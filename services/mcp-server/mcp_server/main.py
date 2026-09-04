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

Deployment constraint, decided rather than inherited:
`host=LOOPBACK_HOST` turns on the SDK's DNS-rebinding protection, which
answers 421 to any `Host` header that is not `127.0.0.1:<port>`,
`localhost:<port>` or `[::1]:<port>`. Behind a service name or an ingress
hostname that refuses every call before identity is reached, so row 30 must
pass `transport_security` with the deployed hostnames. Left as the safe
default here because the alternative -- disabling the protection to make a
container work -- is the wrong way round.

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
from fastapi.responses import JSONResponse
from helpdesk.errors import ToolError
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server
from pydantic import BaseModel
from starlette.requests import Request

from mcp_server import pipeline, readiness, registry

logger = logging.getLogger(__name__)

SERVER_NAME = "helpdesk-mcp"
SERVER_VERSION = "0.1.0"

MCP_PATH = "/mcp"
"""Where the streamable HTTP transport is mounted (the design)."""

LOOPBACK_HOST = "127.0.0.1"
"""The host the transport's DNS-rebinding protection is configured for.

Passed explicitly so the protection is a choice with its consequence written
next to it, not an SDK default nobody decided on. See the module docstring."""


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

    `/healthz` and `/readyz` are registered before the transport is mounted
    so the mount at `/` cannot swallow them.
    """
    mcp_app = build_server().streamable_http_app(
        streamable_http_path=MCP_PATH, stateless_http=True, host=LOOPBACK_HOST
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # The session manager's task group lives for the life of the app;
        # without this the transport answers every request with a 500.
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
        }
        ready = all(checks.values())
        body = ReadinessResponse(status="ready" if ready else "not_ready", checks=checks)
        return JSONResponse(status_code=200 if ready else 503, content=body.model_dump())

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
    asyncio.run(_serve_stdio())
