"""The MCP tool proxy every authenticated read route acts through.

Split out of `deps.py` by an earlier change so the plain request dependencies
(`agent_api.core.deps`) carry no MCP client import. `build_tool_caller` is
reached through THIS module's global, so the existing
`monkeypatch.setattr(..., "build_tool_caller", …)` sites bind here and the
route's tests still open no socket.
"""

from collections.abc import Callable, Mapping
from typing import Any

from fastapi.responses import JSONResponse
from pydantic import BaseModel

from helpdesk.agent_api.core.deps import authenticated, typed_refusal, unauthenticated
from helpdesk.agent_api.services.mcp_client import McpToolCaller, ToolCaller, TracingToolCaller
from helpdesk.shared.auth.jwt import TokenClaims
from helpdesk.shared.core.settings import Settings, load_settings
from helpdesk.shared.errors import ToolError


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


async def call_tool_route(
    authorization: str | None,
    tool: str,
    arguments: BaseModel,
    extend: Callable[[TokenClaims], Mapping[str, Any]] | None = None,
) -> JSONResponse:
    """Verify the caller, call one tool under their own bearer, render it.

    The plumbing every proxied route shares, moved out of `me_routes` by plan
    23c (decision C). `build_tool_caller` is reached through THIS module's
    global, so a test stubbing the seam patches `agent_api.services.tool_proxy`
    and the route opens no socket.

    The token check runs BEFORE the caller is built, so a request with no
    usable bearer opens no MCP session at all — pinned by the 401 cases in
    `test_me_routes.py` and, over all three approval routes, by
    `test_approval_routes.py::test_an_absent_or_malformed_bearer_is_a_typed_401_and_opens_no_session`,
    each of which scripts a caller with no answers so entering it fails the
    case outright rather than showing up as a count.

    Absent, malformed, forged and expired all answer the same literal 401 with
    `FORBIDDEN`: a caller learns they are not authenticated, never which of the
    four it was. That status is written here rather than read from
    `STATUS_FOR_CODE`, whose `FORBIDDEN` row is the matrix's 403.

    A tool refusal crosses as its own typed code
    (`test_approval_routes.py::test_a_tool_refusal_answers_its_own_typed_code`);
    an unreachable server raises `RemoteInternalError`, which is a `ToolError`
    too and falls through the map to 500 carrying the code alone
    (`::test_an_unreachable_mcp_server_is_one_typed_internal_naming_neither_host_nor_path`).

    `extend` adds keys computed from the VERIFIED claims to a
    successful body; a refusal never carries them.
    """
    verified = authenticated(authorization)
    if verified is None:
        return unauthenticated()
    token, verified_claims = verified

    try:
        async with build_tool_caller(load_settings(), token) as tools:
            payload: Mapping[str, Any] = await tools.call_tool(tool, arguments)
    except ToolError as exc:
        return typed_refusal(exc.code)
    body = dict(payload)
    if extend is not None:
        body.update(extend(verified_claims))
    return JSONResponse(status_code=200, content=body)
