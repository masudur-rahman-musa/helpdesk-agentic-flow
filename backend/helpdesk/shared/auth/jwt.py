"""HS256 token issue and check. Pure: no I/O, no settings, time passed in.

Tenant and user always come from a checked token, never from tool
arguments. The algorithm is pinned on decode, so an `alg: none` or RS/ES
header is refused.
"""

from collections.abc import Mapping
from datetime import datetime, timedelta

import jwt as _pyjwt
from pydantic import BaseModel, ValidationError

_ALGORITHM = "HS256"
_TOKEN_TTL = timedelta(hours=8)


class TokenVerificationError(Exception):
    """The token failed signature, algorithm, claim-shape, or expiry checks.

    One error type for every cause: callers must not be able to tell (or
    leak) why a token was refused.
    """


class TokenClaims(BaseModel):
    sub: str
    tenant_id: str
    department_id: str
    principals: list[str]
    sid: str
    exp: int  # Unix seconds
    iat: int  # Unix seconds


def issue_token(claims: Mapping[str, object], secret: str, now: datetime) -> str:
    """Sign the five identity claims plus iat=now and an 8-hour exp."""
    payload = TokenClaims.model_validate(
        {
            **claims,
            "iat": int(now.timestamp()),
            "exp": int((now + _TOKEN_TTL).timestamp()),
        }
    )
    return _pyjwt.encode(payload.model_dump(), secret, algorithm=_ALGORITHM)


def verify_token(token: str, secret: str, now: datetime) -> TokenClaims:
    """Check signature, algorithm, claim shape and expiry; return typed claims."""
    try:
        payload = _pyjwt.decode(
            token,
            secret,
            algorithms=[_ALGORITHM],
            # exp and iat are judged against the injected `now` below, never
            # PyJWT's real clock.
            options={"verify_exp": False, "verify_iat": False},
        )
    except _pyjwt.exceptions.PyJWTError as exc:
        raise TokenVerificationError("token failed verification") from exc
    try:
        parsed = TokenClaims.model_validate(payload)
    except ValidationError as exc:
        raise TokenVerificationError("token failed verification") from exc
    if int(now.timestamp()) >= parsed.exp:
        raise TokenVerificationError("token failed verification")
    return parsed
