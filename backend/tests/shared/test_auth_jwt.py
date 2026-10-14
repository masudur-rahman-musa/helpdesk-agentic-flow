"""Tests for helpdesk.auth.jwt.

Expected implementation surface (the implementer must provide exactly this):

``helpdesk.auth.jwt`` (pure: no I/O, no settings access, time injected)
    ``class TokenClaims(pydantic.BaseModel)``
        sub: str            -- user id (UUID string)
        tenant_id: str      -- UUID string
        department_id: str  -- UUID string
        principals: list[str]
        sid: str            -- session id (UUID string)
        exp: int            -- Unix seconds
        iat: int            -- Unix seconds
    ``def issue_token(claims, secret, now) -> str``
        ``claims`` carries the five identity claims (sub, tenant_id,
        department_id, principals, sid); ``iat`` is ``now`` and ``exp`` is
        ``now`` + 8 hours (the design). HS256 only.
    ``def verify_token(token, secret, now) -> TokenClaims``
        Validates signature, algorithm (HS256 pinned on decode) and expiry
        against the injected ``now``; raises ``TokenVerificationError`` on any
        failure (typed error, no free-text branches to distinguish causes).
    ``class TokenVerificationError(Exception)``
"""

import base64
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import pytest

from helpdesk.shared.auth.jwt import TokenClaims, TokenVerificationError, issue_token, verify_token

SECRET = "unit-test-jwt-secret-0123456789abcdef"  # obviously fake, tests only
OTHER_SECRET = "unit-test-other-secret-0123456789abc"
NOW = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)
EXPIRY = timedelta(hours=8)

CLAIMS: dict[str, object] = {
    "sub": "0b1f8a52-1111-4aaa-8aaa-000000000001",
    "tenant_id": "0b1f8a52-2222-4bbb-8bbb-000000000002",
    "department_id": "0b1f8a52-3333-4ccc-8ccc-000000000003",
    "principals": ["employee", "manager"],
    "sid": "0b1f8a52-4444-4ddd-8ddd-000000000004",
}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _segment(obj: Mapping[str, object]) -> str:
    return _b64url(json.dumps(obj).encode())


def _alg_none_token() -> str:
    """A well-formed unsigned token: header alg=none, valid-looking payload."""
    header = {"alg": "none", "typ": "JWT"}
    payload = {
        **CLAIMS,
        "iat": int(NOW.timestamp()),
        "exp": int((NOW + EXPIRY).timestamp()),
    }
    return f"{_segment(header)}.{_segment(payload)}."


def _tamper_signature(token: str) -> str:
    head, payload, sig = token.split(".")
    flipped = ("A" if sig[0] != "A" else "B") + sig[1:]
    return f"{head}.{payload}.{flipped}"


def _tamper_payload(token: str) -> str:
    """Swap in an attacker-chosen payload while keeping the real signature."""
    head, _payload, sig = token.split(".")
    forged = {
        **CLAIMS,
        "principals": ["platform_admin"],
        "iat": int(NOW.timestamp()),
        "exp": int((NOW + EXPIRY).timestamp()),
    }
    return f"{head}.{_segment(forged)}.{sig}"


def test_well_signed_token_with_missing_claims_rejected() -> None:
    """A correctly signed token whose payload lacks the identity claims fails."""
    import jwt as pyjwt

    token = pyjwt.encode(
        {"sub": "x", "iat": int(NOW.timestamp()), "exp": int((NOW + EXPIRY).timestamp())},
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(TokenVerificationError):
        verify_token(token, SECRET, NOW)


def test_round_trip_preserves_all_claims() -> None:
    token = issue_token(CLAIMS, SECRET, NOW)

    out = verify_token(token, SECRET, NOW + timedelta(hours=1))

    assert isinstance(out, TokenClaims)
    assert out.sub == CLAIMS["sub"]
    assert out.tenant_id == CLAIMS["tenant_id"]
    assert out.department_id == CLAIMS["department_id"]
    assert out.principals == CLAIMS["principals"]
    assert out.sid == CLAIMS["sid"]
    assert out.iat == int(NOW.timestamp())
    assert out.exp == int((NOW + EXPIRY).timestamp())


def test_expiry_is_exactly_eight_hours() -> None:
    token = issue_token(CLAIMS, SECRET, NOW)
    out = verify_token(token, SECRET, NOW)
    assert out.exp - out.iat == int(EXPIRY.total_seconds())


def test_token_still_valid_just_before_expiry() -> None:
    token = issue_token(CLAIMS, SECRET, NOW)
    out = verify_token(token, SECRET, NOW + EXPIRY - timedelta(seconds=1))
    assert out.sub == CLAIMS["sub"]


def test_expired_token_rejected() -> None:
    token = issue_token(CLAIMS, SECRET, NOW)
    with pytest.raises(TokenVerificationError):
        verify_token(token, SECRET, NOW + EXPIRY + timedelta(seconds=1))


def test_wrong_secret_rejected() -> None:
    token = issue_token(CLAIMS, SECRET, NOW)
    with pytest.raises(TokenVerificationError):
        verify_token(token, OTHER_SECRET, NOW)


@pytest.mark.parametrize(
    ("label", "make_token"),
    [
        ("tampered signature", lambda: _tamper_signature(issue_token(CLAIMS, SECRET, NOW))),
        (
            "tampered payload, real signature",
            lambda: _tamper_payload(issue_token(CLAIMS, SECRET, NOW)),
        ),
        ("alg none confusion", _alg_none_token),
        ("not a jwt at all", lambda: "not-a-jwt"),
        ("empty token", lambda: ""),
    ],
)
def test_invalid_tokens_rejected_with_typed_error(label: str, make_token: object) -> None:
    assert callable(make_token)
    token = make_token()
    assert isinstance(token, str)
    with pytest.raises(TokenVerificationError):
        verify_token(token, SECRET, NOW)
