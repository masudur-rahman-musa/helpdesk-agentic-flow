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

from datetime import UTC, datetime

from helpdesk.auth.jwt import TokenClaims, TokenVerificationError, verify_token
from helpdesk.core.settings import Settings

from agent_api.mcp_client import McpToolCaller, ToolCaller, TracingToolCaller


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
