"""The MCP tool proxy every authenticated read route acts through.

Split out of `deps.py` by an earlier change so the plain request dependencies
(`agent_api.core.deps`) carry no MCP client import. `build_tool_caller` is
reached through THIS module's global, so the existing
`monkeypatch.setattr(..., "build_tool_caller", …)` sites bind here and the
route's tests still open no socket.

An earlier change replaces `call_tool_route` with `proxy_tool`, which returns the tool
body as a plain mapping instead of building a `JSONResponse`. ToolErrors
propagate to the app-level handler in `router.py`.
"""

from collections.abc import Callable, Mapping
from typing import Any

from fastapi import Request
from pydantic import BaseModel

from helpdesk.agent_api.core.deps import request_bearer, request_claims
from helpdesk.agent_api.services.mcp_client import McpToolCaller, ToolCaller, TracingToolCaller
from helpdesk.shared.auth.jwt import TokenClaims
from helpdesk.shared.core.config import Settings, load_settings


def build_tool_caller(settings: Settings, bearer_token: str) -> ToolCaller:
    """The request's MCP caller, built per request and never held on the app.

    A tool session's lifetime is one turn and the caller
    is what enters and exits it, so this is deliberately not a lifespan
    resource.

    The caller's own bearer is forwarded verbatim: agent-api
    signs nothing, and the MCP server decides what that person may do. Pinned
    for the read routes by
    `test_me_routes.py::test_the_requests_route_answers_the_tools_output_unchanged`,
    which asserts the scripted caller saw the SAME token the request carried.

    Wrapped in the tracing decorator here, which is the only place the concrete
    caller is built: every call then opens its
    `tool:{name}` span under the node that made it, and the MCP client itself
    learns nothing about tracing. Pinned by
    `test_agent_tracing.py::test_the_route_builds_the_tracing_caller_around_the_mcp_one`,
    which is what catches a wrapper that exists and is never wired.
    """
    return TracingToolCaller(McpToolCaller(url=settings.mcp_url, bearer_token=bearer_token))


async def proxy_tool(
    request: Request,
    tool: str,
    arguments: BaseModel,
    extend: Callable[[TokenClaims], Mapping[str, Any]] | None = None,
) -> Mapping[str, Any]:
    """Call one tool under the caller's own bearer; return the body or raise ToolError.

    Replaces `call_tool_route` as the plumbing every proxied route
    shares. The difference from an earlier change onward: instead of catching ToolError
    and building a JSONResponse here, the error propagates to the app-level
    handler in `router.py`, which is the one edge that renders it.

    `build_tool_caller` is reached through THIS module's global, so a test
    stubbing the seam patches `agent_api.services.tool_proxy` and the route
    opens no socket. Pinned by every proxy route test that sets
    `monkeypatch.setattr(tool_proxy, "build_tool_caller", ...)`.

    The caller is already verified: `RequireAuthMiddleware` turns away
    every request with no valid token before any route runs, and puts the raw
    bearer and the verified claims on `request.state`. So this reads them off the
    request, and there is no 401 path here — a request with no usable bearer
    never reaches a route that calls this.

    A tool refusal crosses as its own typed code (ToolError) which the handler
    renders through the one map. An unreachable server raises
    `RemoteInternalError`, which is a `ToolError` too and crosses as INTERNAL 500.

    `extend` adds keys computed from the VERIFIED claims to a
    successful body; a refusal never carries them.
    """
    token = request_bearer(request)
    verified_claims = request_claims(request)

    async with build_tool_caller(load_settings(), token) as tools:
        payload: Mapping[str, Any] = await tools.call_tool(tool, arguments)
    body = dict(payload)
    if extend is not None:
        body.update(extend(verified_claims))
    return body
