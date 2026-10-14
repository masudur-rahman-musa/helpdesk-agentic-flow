"""The plain request dependencies every authenticated agent-api route needs.

Extracted from `chat_routes` by an earlier change (decision C) when `me_routes` became
the second router to need them: reading a bearer off a header, turning that
bearer into verified claims, and the typed-refusal map a proxied route answers
with. The tool proxy that acts through those claims lives next to this module
in `services/tool_proxy.py`.

A test that wants to stub a seam patches `agent_api.core.deps` for these plain
dependencies; the tool proxy patches `agent_api.services.tool_proxy`.
"""

from collections.abc import Mapping
from datetime import UTC, datetime

from fastapi.responses import JSONResponse

from helpdesk.shared.auth.jwt import TokenClaims, TokenVerificationError, verify_token
from helpdesk.shared.core.settings import load_settings
from helpdesk.shared.errors import ErrorCode

STATUS_FOR_CODE: Mapping[ErrorCode, int] = {
    ErrorCode.VALIDATION: 400,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.POLICY_BLOCKED: 409,
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

`POLICY_BLOCKED` is **409**. It became reachable from a
proxied route with `/admin/killswitch`: an admin aiming `set_kill_switch` at
`set_kill_switch` is refused by the protected-tool guard, and with no row here
that refusal fell through to a 500 — telling an admin the platform broke when
the platform refused, the one message that would send somebody looking for an
outage. 409 because the caller holds the permission and the action conflicts
with the platform's own state, which is exactly `ProtectedToolError`'s
docstring. Added here rather than special-cased in that router, for the same
reason 23c decision C moved the map at all.

Pinned, as a whole — the five rows and the missing `INTERNAL` one — by
`test_killswitch_routes.py::test_the_shared_status_map_answers_exactly_five_codes_with_no_internal_row`,
and row by row by
`test_approval_routes.py::test_the_shared_status_map_answers_a_matrix_refusal_403_and_a_kill_switch_503`
and `test_me_routes.py::test_the_status_map_dresses_forbidden_as_403_and_not_as_chats_401`.
"""


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
