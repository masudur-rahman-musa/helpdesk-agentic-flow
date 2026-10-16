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

from fastapi import Request
from fastapi.responses import JSONResponse

from helpdesk.shared.auth.jwt import TokenClaims, TokenVerificationError, verify_token
from helpdesk.shared.core.errors import ErrorCode

STATUS_FOR_CODE: Mapping[ErrorCode, int] = {
    ErrorCode.VALIDATION: 400,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.SOD_CONFLICT: 409,
    ErrorCode.POLICY_BLOCKED: 409,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.TOOL_DISABLED: 503,
    ErrorCode.INTERNAL: 500,
}
"""The one HTTP status each of the eight typed codes crosses the wire as.

The single map the whole agent-api turns a code into a status with (an earlier change,
a design note). Before it there were two that disagreed: the proxied routes read
`FORBIDDEN` as 403 and `POLICY_BLOCKED` as 409 with no row for `INTERNAL`, and
the chat routes read the same `FORBIDDEN` as 401 and `POLICY_BLOCKED` as 429
with only four rows. One code now crosses the wire as one status, and every
code has a row so nothing falls through to 500 by accident. The `.get(code,
500)` in `typed_refusal` is kept as a backstop for a code added to the enum
without a row, but every one of today's eight is listed.

Two answers stay explicit and are NOT a lookup in this map:
- an unauthenticated caller is `RequireAuthMiddleware`'s literal 401,
  so a caller learns they are not authenticated and never which of absent,
  malformed, forged or expired it was. `FORBIDDEN` here is the matrix's 403.
- the rate limit is a literal 429 where it is caught in `api/routes/chat.py`.
  `POLICY_BLOCKED` in this map is the proxied conflict's 409, a different fact.

The rows, one by one:
- `FORBIDDEN` is 403: on a proxied route it can only have come from the RBAC
  matrix, which is a permission decision.
- `SOD_CONFLICT` and `IDEMPOTENCY_CONFLICT` are 409: the caller holds the
  permission and the action conflicts with the platform's own state.
- `POLICY_BLOCKED` is 409: the protected-tool guard
  refusing an admin aiming `set_kill_switch` at `set_kill_switch` is a conflict
  with platform state, exactly `ProtectedToolError`'s docstring, not an outage.
- `TOOL_DISABLED` is 503: an admin tripping the kill switch is "this call is
  unavailable right now", not a permission decision.
- `INTERNAL` is 500: a failure the caller cannot act on, carrying the code and
  nothing of the client's own message (an MCP host and path are internal).

Pinned as a WHOLE, every row and the count, by
`test_error_map.py::test_the_one_map_has_a_row_for_every_code`, and row by row
by
`test_approval_routes.py::test_the_shared_status_map_answers_a_matrix_refusal_403_and_a_kill_switch_503`
and `test_me_routes.py::test_the_one_shared_map_dresses_forbidden_as_403`.
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


def request_bearer(request: Request) -> str:
    """The verified caller's own raw bearer, forwarded to the MCP server.

    Put on `request.state` by `RequireAuthMiddleware` after a single verify, so a
    route never re-reads the header. The middleware turns away every request with
    no valid token before the route runs, so this is always present on a route
    the middleware let through.
    """
    token: str = request.state.auth_bearer
    return token


def request_claims(request: Request) -> TokenClaims:
    """The verified caller's claims, the one thing a route may derive identity from.

    Put on `request.state` by `RequireAuthMiddleware` beside the raw bearer, for
    the same reason and with the same guarantee: present on every route the
    middleware let through, never a tool argument.
    """
    verified: TokenClaims = request.state.auth_claims
    return verified
