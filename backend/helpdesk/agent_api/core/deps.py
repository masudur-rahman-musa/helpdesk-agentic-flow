"""Request helpers every signed-in agent-api route needs.

The token and claims set by the auth middleware, and the one map from typed
error codes to HTTP statuses.
"""

from collections.abc import Mapping
from datetime import UTC, datetime

from fastapi import Request
from fastapi.responses import JSONResponse

from helpdesk.shared.auth.jwt import TokenClaims, TokenVerificationError, verify_token
from helpdesk.shared.core.errors import ErrorCode, ToolError

STATUS_FOR_CODE: Mapping[ErrorCode, int] = {
    ErrorCode.VALIDATION: 400,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.SOD_CONFLICT: 409,
    ErrorCode.POLICY_BLOCKED: 409,
    ErrorCode.REQUEST_NOT_PENDING: 409,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.TOOL_DISABLED: 503,
    ErrorCode.INTERNAL: 500,
}
"""The HTTP status each typed error code is sent as.

A missing or bad token is the middleware's own 401, not `FORBIDDEN`'s 403.
"""


class InvalidBodyError(ToolError):
    """A request body a route reads by hand and refuses, as a typed 400."""

    code = ErrorCode.VALIDATION


def bearer(authorization: str | None) -> str | None:
    """The token out of an `Authorization: Bearer <token>` header, or None."""
    if authorization is None:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def claims(token: str | None, secret: str) -> TokenClaims | None:
    """The verified claims, or None, with no hint of why it failed."""
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
    """The single 401 for any token that does not verify."""
    return JSONResponse(status_code=401, content={"code": ErrorCode.FORBIDDEN.value})


def request_bearer(request: Request) -> str:
    """The caller's raw token, set by the auth middleware, to forward to the MCP server."""
    token: str = request.state.auth_bearer
    return token


def request_claims(request: Request) -> TokenClaims:
    """The caller's verified claims: the only place a route may take identity from."""
    verified: TokenClaims = request.state.auth_claims
    return verified
