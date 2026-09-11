"""The three things every authenticated agent-api route needs.

Extracted from `chat_routes` by an earlier change (decision C) when `me_routes` became
the second router to need them: reading a bearer off a header, turning that
bearer into verified claims, and building the MCP caller a request acts
through. One seam rather than two copies, so "the caller's own bearer is
forwarded verbatim" is one statement of one fact.

`chat_routes` keeps all three as module globals and calls them through the
module attribute, so the existing `monkeypatch.setattr(chat_routes,
"build_tool_caller", …)` sites still bind and the route's tests still open no
socket. A test that wants to stub this module's own seam patches
`agent_api.deps.build_tool_caller`; a test of the chat routes patches theirs.
"""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from fastapi.responses import JSONResponse
from helpdesk.auth.jwt import TokenClaims, TokenVerificationError, verify_token
from helpdesk.core.settings import Settings, load_settings
from helpdesk.errors import ErrorCode, ToolError
from pydantic import BaseModel

from agent_api.mcp_client import McpToolCaller, ToolCaller, TracingToolCaller

STATUS_FOR_CODE: Mapping[ErrorCode, int] = {
    ErrorCode.VALIDATION: 400,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.TOOL_DISABLED: 503,
}
"""The HTTP status each typed code a proxied tool route can answer with.

Moved here from `me_routes` by an earlier change (decision C) when `approval_routes`
became the second router to need it, and 23e, 23f and 24 will each be the
next: a typed-refusal map that forks twice is a map that answers one code two
ways.

`FORBIDDEN` is **403** and not the 401 `chat_routes.STATUS_FOR_CODE` carries,
because on these routers the two facts are different ones: a caller who did
not prove who they are gets a literal 401 in `call_tool_route`, and a
FORBIDDEN can then only have come from the RBAC matrix, which is a 403.

`INTERNAL` has no row on purpose: an unreachable MCP server is a failure the
caller cannot act on, so it falls through to 500 carrying the code and nothing
of the client's own message (the host and the path are internal detail).
`TOOL_DISABLED` is 503 because an admin tripping the kill switch is exactly
"this call is unavailable right now", not a permission decision.

Pinned, as a whole — the 403, the 503, the 400 and the missing `INTERNAL` row
— by
`test_approval_routes.py::test_the_shared_status_map_answers_a_matrix_refusal_403_and_a_kill_switch_503`
and `test_me_routes.py::test_the_status_map_dresses_forbidden_as_403_and_not_as_chats_401`.
"""


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


def bearer(authorization: str | None) -> str | None:
    """The token out of an `Authorization: Bearer <token>` header, or None.

    Anything else — no header, another scheme, a scheme with nothing after it —
    is None, which every caller turns into the same typed refusal. The scheme
    comparison is case-insensitive because RFC 7235 makes it so; the token is
    not touched.
    """
    if authorization is None:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def claims(token: str | None, secret: str) -> TokenClaims | None:
    """The token's VERIFIED claims, or None for anything that does not verify.

    One answer for absent, malformed, forged and expired: a caller learns that
    they are not authenticated, never which of the four it was. "Verified"
    is the load-bearing word — `verify_token` checks the signature and the
    expiry against the instant read here, so a token whose claims parse and
    whose `exp` has passed is None. Pinned in all four shapes by
    `test_me_routes.py`'s three 401 cases, each of which also asserts the
    scripted caller recorded no call at all.
    """
    if token is None:
        return None
    try:
        return verify_token(token, secret, datetime.now(UTC))
    except TokenVerificationError:
        return None


def typed_refusal(code: ErrorCode) -> JSONResponse:
    """One typed body, dressed in this code's status. Never free text."""
    return JSONResponse(status_code=STATUS_FOR_CODE.get(code, 500), content={"code": code.value})


def unauthenticated() -> JSONResponse:
    """The one answer for absent, malformed, forged and expired.

    A literal 401 rather than a row of `STATUS_FOR_CODE`, whose `FORBIDDEN` is
    the matrix's 403: a caller learns they are not authenticated, never which
    of the four it was. One function so the two callers below cannot drift
    into two bodies for one fact.
    """
    return JSONResponse(status_code=401, content={"code": ErrorCode.FORBIDDEN.value})


def authenticated(authorization: str | None) -> tuple[str, TokenClaims] | None:
    """The caller's own bearer and its VERIFIED claims, or None.

    The token half is what a route forwards; the claims half is
    what a route may derive an identity from — never a tool argument. Both
    come back together so no caller can hold one without the other.
    """
    token = bearer(authorization)
    verified = claims(token, load_settings().jwt_secret)
    if token is None or verified is None:
        return None
    return token, verified


async def call_tool_route(
    authorization: str | None, tool: str, arguments: BaseModel
) -> JSONResponse:
    """Verify the caller, call one tool under their own bearer, render it.

    The plumbing every proxied route shares, moved out of `me_routes` by plan
    23c (decision C). `build_tool_caller` is reached through THIS module's
    global, so a test stubbing the seam patches `agent_api.deps` and the route
    opens no socket.

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
    """
    verified = authenticated(authorization)
    if verified is None:
        return unauthenticated()
    token, _claims = verified

    try:
        async with build_tool_caller(load_settings(), token) as tools:
            payload: Mapping[str, Any] = await tools.call_tool(tool, arguments)
    except ToolError as exc:
        return typed_refusal(exc.code)
    return JSONResponse(status_code=200, content=dict(payload))
