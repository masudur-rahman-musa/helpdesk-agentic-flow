"""The MCP server: the streamable HTTP transport, health checks, and stdio.

The low-level SDK server is used so the kill switch and access checks run
before argument checks. The DNS-rebinding host list is configured, never
guessed, so calls by compose service name work.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import mcp.types as types
from fastapi import FastAPI
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request

from helpdesk.mcp_server.tools import dispatch, registry
from helpdesk.mcp_server.tools.context import ToolClients
from helpdesk.shared.core import health as _health
from helpdesk.shared.core.config import load_settings, mcp_allowed_hosts_from_env
from helpdesk.shared.core.db import shared_engine
from helpdesk.shared.core.errors import ToolError
from helpdesk.shared.core.observability.logging import configure_logging
from helpdesk.shared.core.redis import get_redis

logger = logging.getLogger(__name__)

SERVER_NAME = "helpdesk-mcp"
SERVER_VERSION = "0.1.0"

MCP_PATH = "/mcp"
"""Where the streamable HTTP transport is mounted."""


def transport_security() -> TransportSecuritySettings:
    """DNS-rebinding settings, always on. No origins, since no browser calls `/mcp`."""
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=mcp_allowed_hosts_from_env(),
    )


async def _list_tools(
    ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
) -> types.ListToolsResult:
    """Exactly the registry, with each model's own JSON schema, so the two cannot drift."""
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
    """Look up the tool name first, so an unknown one never reaches the kill switch.

    That case is logged, not audited: there is no tool to name.
    """
    try:
        spec = registry.lookup(params.name)
    except ToolError as exc:
        logger.warning("unregistered tool name requested: %r", params.name)
        return dispatch.error_result(exc.code)
    request = ctx.request if isinstance(ctx.request, Request) else None
    return await dispatch.dispatch(spec, request, params.arguments or {})


def build_server() -> Server[Any]:
    """A fresh lowlevel MCP server wired to the registry and the pipeline."""
    return Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        on_list_tools=_list_tools,
        on_call_tool=_call_tool,
    )


def create_app() -> FastAPI:
    """The HTTP app: health, readiness, metrics, and the MCP transport."""
    mcp_app = build_server().streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        transport_security=transport_security(),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        configure_logging()
        # Open the tool clients once, on the mounted app's state, which tool
        # calls see as `request.app`. Closed at shutdown so no pool leaks.
        tool_clients = ToolClients.open_clients(load_settings())
        mcp_app.state.tool_clients = tool_clients
        # The session manager's task group lives for the life of the app;
        # without this the transport answers every request with a 500.
        async with mcp_app.router.lifespan_context(mcp_app):
            try:
                yield
            finally:
                await tool_clients.aclose()

    app = FastAPI(title="mcp-server", lifespan=lifespan)

    # Health routes go first: the mount at "/" would otherwise catch them.
    _health.register_health(app, _build_mcp_checks)

    app.mount("/", mcp_app)
    return app


def _build_mcp_checks(state: object) -> dict[str, _health._CheckFn]:
    """The three readiness checks, using the shared per-loop clients."""
    from helpdesk.mcp_server.core import rag_clients  # avoid circular import at module level

    settings = load_settings()
    return {
        "postgres": lambda: _health.ping_postgres(shared_engine(settings.database_url)),
        "redis": lambda: _health.ping_redis(get_redis(settings)),
        "qdrant": lambda: _health.ping_qdrant(rag_clients.qdrant(settings)),
    }


app = create_app()


async def _serve_stdio() -> None:
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def run_stdio() -> None:
    """Serve the tools over stdio for local debugging. Calls are refused; no token."""
    configure_logging()
    asyncio.run(_serve_stdio())
