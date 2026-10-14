"""The MCP server service: the streamable HTTP transport, health, and stdio.

The SDK's lowlevel `Server` is used rather than its decorator-based wrapper
so `tools/list` is built from the registry and `tools/call` goes straight to
`pipeline.dispatch`. The wrapper would validate arguments against the tool's
schema before any handler ran, which would put input validation ahead of the
kill switch and RBAC and would answer with pydantic's own text — both of the
things an earlier change's criteria 5 and 6 forbid.

`create_app()` builds a FRESH server and app each call. The SDK's
`StreamableHTTPSessionManager` refuses a second `run()` on one instance, so
one app per process (module-level `app`, for uvicorn) and one per test.

DNS-rebinding protection is on and its allow-list is CONFIGURED, never
inferred. `transport_security` is
passed explicitly, so the SDK reads no `host=` argument and cannot fall back to
the loopback names alone: that inference is what answered 421 to every call
agent-api made inside compose, where the server is reached by its service name.
The patterns come from `MCP_ALLOWED_HOSTS`, defaulting to
`helpdesk.core.settings.DEFAULT_MCP_ALLOWED_HOSTS` -- the three loopback names
plus this repository's compose service name -- and a deployment reached by any
other name sets the variable to its own list. The protection itself is never
disabled and the list is never empty: an unlisted host is still answered 421,
pinned by
`test_mcp_transport.py::test_an_unlisted_host_is_refused_with_421`, and the
explicit pass by `::test_the_transport_receives_explicit_security_settings`.

`tools/list` is deliberately unauthenticated (decision I): it advertises
names and schemas, and every call still passes the whole pipeline. Row 12
registers tools whose names are worth less freely, and gates this then.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import mcp.types as types
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel
from starlette.requests import Request

from helpdesk.mcp_server import pipeline, readiness, registry
from helpdesk.shared.core.settings import mcp_allowed_hosts_from_env
from helpdesk.shared.errors import ToolError
from helpdesk.shared.obs.logging import configure_logging
from helpdesk.shared.obs.metrics import render as render_metrics

logger = logging.getLogger(__name__)

SERVER_NAME = "helpdesk-mcp"
SERVER_VERSION = "0.1.0"

MCP_PATH = "/mcp"
"""Where the streamable HTTP transport is mounted (the design)."""


def transport_security() -> TransportSecuritySettings:
    """The transport's DNS-rebinding settings, built from configuration.

    Explicit rather than inferred from a `host=` argument:
    the SDK reads `host` only when `transport_security` is None, so passing both
    would leave a dead argument reading as if it still decided something. The
    protection stays ON -- there is no flag and no empty value that turns it off
    (decision D) -- and only the allow-list is configurable.

    `allowed_origins` is deliberately left empty (decision C): nothing in a
    browser calls `/mcp`, agent-api being a server-side httpx client that sends
    no `Origin` header, and the middleware accepts an absent one. A configured
    origin list would be a value nobody reads. Pinned by
    `test_mcp_transport.py::test_the_transport_receives_explicit_security_settings`.
    """
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=mcp_allowed_hosts_from_env(),
    )


class HealthResponse(BaseModel):
    status: str


class ReadinessResponse(BaseModel):
    status: str
    checks: dict[str, bool]


async def _list_tools(
    ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
) -> types.ListToolsResult:
    """Exactly the registry, with each model's own JSON schema.

    Built from the registry rather than from a second list, so a schema
    cannot drift from the model that validates it.
    """
    return types.ListToolsResult(
        tools=[
            types.Tool(
                name=spec.name,
                description=spec.description,
                input_schema=spec.input_model.model_json_schema(),
                output_schema=spec.output_model.model_json_schema(),
            )
            for spec in registry.REGISTRY.values()
        ]
    )


async def _call_tool(
    ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
) -> types.CallToolResult:
    """The wire name is resolved before anything else can use it.

    Criterion 8: an unregistered or malformed name must not reach the kill
    switch, because Redis has no shape or length limit and would happily
    store a key nothing ever reads.

    This is the one path that writes no audit event: there is no spec to
    name, no declared event type and no subject, so an audit row could say
    nothing beyond "a client sent a name we do not have". It is logged
    instead, and carried in STATUS as owed to row 10b.
    """
    try:
        spec = registry.lookup(params.name)
    except ToolError as exc:
        logger.warning("unregistered tool name requested: %r", params.name)
        return pipeline.error_result(exc.code)
    request = ctx.request if isinstance(ctx.request, Request) else None
    return await pipeline.dispatch(spec, request, params.arguments or {})


def build_server() -> Server[Any]:
    """A fresh lowlevel MCP server wired to the registry and the pipeline."""
    return Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        on_list_tools=_list_tools,
        on_call_tool=_call_tool,
    )


def create_app() -> FastAPI:
    """The HTTP application: health, readiness, and the MCP transport.

    `/healthz`, `/readyz` and `/metrics` are registered before the transport
    is mounted so the mount at `/` cannot swallow them — a mount matches every
    path under it, so a scrape registered after it would reach the streamable
    HTTP transport and be answered 406. Pinned by
    `test_mcp_metrics.py::test_the_metrics_route_answers_before_the_mcp_mount`.
    """
    mcp_app = build_server().streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        transport_security=transport_security(),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # The session manager's task group lives for the life of the app;
        # without this the transport answers every request with a 500.
        configure_logging()
        async with mcp_app.router.lifespan_context(mcp_app):
            yield

    app = FastAPI(title="mcp-server", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get("/readyz", response_model=ReadinessResponse)
    async def readyz() -> JSONResponse:
        # Called through the module attribute so tests can fake one check.
        checks = {
            "postgres": await readiness.check_postgres(),
            "redis": await readiness.check_redis(),
            "qdrant": await readiness.check_qdrant(),
        }
        ready = all(checks.values())
        body = ReadinessResponse(status="ready" if ready else "not_ready", checks=checks)
        return JSONResponse(status_code=200 if ready else 503, content=body.model_dump())

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> PlainTextResponse:
        """The Prometheus scrape, unauthenticated."""
        body, content_type = render_metrics()
        return PlainTextResponse(content=body, media_type=content_type)

    app.mount("/", mcp_app)
    return app


app = create_app()


async def _serve_stdio() -> None:
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def run_stdio() -> None:
    """Serve the same tools over stdio, for local debugging (the design).

    There is no `Authorization` header on stdio, so every tool call is
    refused `FORBIDDEN`; `tools/list` still works, which is what makes this
    useful for checking what the server advertises.
    """
    configure_logging()
    asyncio.run(_serve_stdio())
