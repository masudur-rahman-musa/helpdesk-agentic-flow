"""Who is calling, taken from the signed token and from nothing else.

This is the only place the MCP server turns an `Authorization` header into an
identity, so no tool argument can name a user or tenant. Every failure gives
the same `FORBIDDEN`, so a caller cannot tell a missing header from an
expired token or a bad claim.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from helpdesk.shared.auth.jwt import TokenClaims, TokenVerificationError, verify_token
from helpdesk.shared.core.errors import ErrorCode, ToolError

_BEARER_SCHEME = "bearer"


class IdentityRefused(ToolError):
    """The caller did not prove who they are, for any reason."""

    code = ErrorCode.FORBIDDEN


class Identity(BaseModel):
    """A verified caller: the parsed ids plus the claims they came from.

    Frozen, so nothing downstream can change who is calling. The claims are kept
    because the access check reads them.
    """

    model_config = ConfigDict(frozen=True)

    user_id: uuid.UUID
    tenant_id: uuid.UUID
    session_id: uuid.UUID
    claims: TokenClaims


def identity_from_header(authorization: str | None, secret: str, now: datetime) -> Identity:
    """Check a bearer token and parse its identity claims, or refuse.

    `now` is passed in so the caller controls the clock expiry is judged by.
    """
    token = _bearer_token(authorization)
    try:
        claims = verify_token(token, secret, now)
    except TokenVerificationError as exc:
        raise IdentityRefused("token failed verification") from exc
    return Identity(
        user_id=_uuid_claim(claims.sub),
        tenant_id=_uuid_claim(claims.tenant_id),
        session_id=_uuid_claim(claims.sid),
        claims=claims,
    )


def _bearer_token(authorization: str | None) -> str:
    """The token out of `Authorization: Bearer <token>`, or a refusal. The scheme ignores case."""
    if authorization is None:
        raise IdentityRefused("no bearer token")
    scheme, _, rest = authorization.partition(" ")
    token = rest.strip()
    if scheme.lower() != _BEARER_SCHEME or not token:
        raise IdentityRefused("no bearer token")
    return token


def _uuid_claim(value: str) -> uuid.UUID:
    """One identity claim as a UUID, or the same single refusal as any other cause."""
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise IdentityRefused("token claim is not a uuid") from exc
