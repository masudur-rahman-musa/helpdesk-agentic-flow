"""Who is calling, derived from the JWT and from nothing else.

a project rule: tenant and user identity come from the verified token, never
from a tool argument. This module is the only place the MCP server turns an
`Authorization` header into an identity, so there is no second path an
argument could travel.

One error type and one code for every cause (`IdentityRefused`, `FORBIDDEN`).
A caller must not be able to tell an absent header from an expired token from
a `sub` that is not a UUID: the twelve-row refusal table in
`test_mcp_identity.py::test_identity_from_header_refuses` asserts the same
code for every cause, so a "helpful" distinction fails there.

`TokenClaims` types `sub`, `tenant_id` and `sid` as `str`, which is the honest
wire shape, so the parse into `uuid.UUID` happens here rather than in the
model. Row 19 needs the same parse and calls this
function; it is not repeated anywhere.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from helpdesk.shared.auth.jwt import TokenClaims, TokenVerificationError, verify_token
from helpdesk.shared.core.errors import ErrorCode, ToolError

_BEARER_SCHEME = "bearer"


class IdentityRefused(ToolError):
    """The caller did not prove who they are, for any reason at all.

    `FORBIDDEN` and one type for every cause, pinned by
    `test_identity_refused_carries_exactly_forbidden` and the refusal table.
    """

    code = ErrorCode.FORBIDDEN


class Identity(BaseModel):
    """A verified caller: the parsed ids plus the claims they came from.

    Frozen: nothing downstream may edit who is calling. The whole
    `TokenClaims` is kept because `rbac.authorize` takes claims, not a
    principal list — the matrix must keep reading the token itself.
    """

    model_config = ConfigDict(frozen=True)

    user_id: uuid.UUID
    tenant_id: uuid.UUID
    session_id: uuid.UUID
    claims: TokenClaims


def identity_from_header(authorization: str | None, secret: str, now: datetime) -> Identity:
    """Verify a bearer token and parse its identity claims, or refuse.

    `now` is injected rather than read here so expiry is judged against one
    clock the caller controls, the same way `helpdesk.auth.jwt` does it.
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
    """The token out of `Authorization: Bearer <token>`, or a refusal.

    The scheme is compared case-insensitively (RFC 7235 makes it so); a
    client sending `bearer` must not be told it lacks permission.
    """
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
