"""An earlier change, section 1 criteria 2 and 4, section 6: the identity seam.

Surface under test (written before it exists):

``mcp_server.identity``
    ``class Identity(BaseModel, frozen=True)`` with ``user_id: uuid.UUID``,
    ``tenant_id: uuid.UUID``, ``session_id: uuid.UUID`` and ``claims:
    TokenClaims`` (kept whole so ``rbac.authorize`` still reads the token,
    never an argument — a project rule).
    ``class IdentityRefused(ToolError)`` with ``code == ErrorCode.FORBIDDEN``.
    ``identity_from_header(authorization: str | None, secret: str,
    now: datetime) -> Identity``.

One error type and one code for every cause. A caller must not be able to
tell an absent header from an expired token from a `sub` that is not a UUID:
the refusal table below is exhaustive over the causes named in the plan, and
every row asserts the same code, so a "helpful" distinction fails here.

``mcp_server.pipeline._session_factory(settings)`` is the seam the transport
cases spy on: criterion 2 says no DB session is opened on a refusal, and a
spy that is never called is the only way to prove that.

No pytest-asyncio: each case is a sync function driving one ``asyncio.run``.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from helpdesk.auth.jwt import TokenClaims
from helpdesk.errors import ErrorCode, ToolError
from mcp_fixtures import (  # noqa: F401 - fixtures are resolved by name
    TEST_JWT_SECRET,
    WRONG_JWT_SECRET,
    SeededMcp,
    bearer,
    call_tool,
    error_code,
    mcp_env,
    mcp_pg,
    mcp_redis_base_url,
    mcp_redis_url,
    seeded_mcp_db,
    token_for,
    token_with_claims,
)
from mcp_server import pipeline
from mcp_server.identity import Identity, IdentityRefused, identity_from_header
from mcp_server.registry import REGISTRY

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)

# A token issued more than the 8-hour TTL before NOW has expired at NOW.
LONG_BEFORE_NOW = NOW - timedelta(hours=9)

# Every header this seam must refuse, keyed by the cause. The builder takes the
# seeded user so a row can forge one claim on an otherwise valid token.
HeaderBuilder = Callable[[SeededMcp], str | None]

REFUSAL_CASES: tuple[tuple[str, HeaderBuilder], ...] = (
    ("no Authorization header at all", lambda seeded: None),
    ("empty header value", lambda seeded: ""),
    (
        "non-bearer scheme (Basic)",
        lambda seeded: f"Basic {token_for(seeded.tenant_a, now=NOW)}",
    ),
    ("scheme with no token", lambda seeded: "Bearer"),
    ("bearer with an empty token", lambda seeded: "Bearer "),
    ("bearer with whitespace for a token", lambda seeded: "Bearer    "),
    ("malformed token, not a JWT at all", lambda seeded: "Bearer not-a-jwt"),
    (
        "token signed with a different secret",
        lambda seeded: f"Bearer {token_for(seeded.tenant_a, now=NOW, secret=WRONG_JWT_SECRET)}",
    ),
    (
        "expired token, otherwise valid",
        lambda seeded: f"Bearer {token_for(seeded.tenant_a, now=LONG_BEFORE_NOW)}",
    ),
    (
        "sub claim is not a UUID",
        lambda seeded: (
            "Bearer " + token_with_claims(seeded.tenant_a, {"sub": "not-a-uuid"}, now=NOW)
        ),
    ),
    (
        "tenant_id claim is not a UUID",
        lambda seeded: (
            "Bearer " + token_with_claims(seeded.tenant_a, {"tenant_id": "not-a-uuid"}, now=NOW)
        ),
    ),
    (
        "sid claim is not a UUID",
        lambda seeded: (
            "Bearer " + token_with_claims(seeded.tenant_a, {"sid": "not-a-uuid"}, now=NOW)
        ),
    ),
)

REFUSAL_IDS = [label for label, _ in REFUSAL_CASES]


# --- the seam itself ----------------------------------------------------------


def test_the_refusal_table_has_every_cause_the_seam_names() -> None:
    """The count is stated in `identity.py`'s docstring, so it is asserted
    here rather than left to drift."""
    assert len(REFUSAL_CASES) == 12


def test_identity_refused_carries_exactly_forbidden() -> None:
    """Pinned as a value, not described in a docstring."""
    assert IdentityRefused.code == ErrorCode.FORBIDDEN
    assert issubclass(IdentityRefused, ToolError)


@pytest.mark.parametrize(("label", "build"), REFUSAL_CASES, ids=REFUSAL_IDS)
def test_identity_from_header_refuses(
    seeded_mcp_db: SeededMcp,  # noqa: F811
    label: str,
    build: HeaderBuilder,
) -> None:
    with pytest.raises(IdentityRefused) as refusal:
        identity_from_header(build(seeded_mcp_db), TEST_JWT_SECRET, NOW)

    assert refusal.value.code == ErrorCode.FORBIDDEN, label


def test_identity_from_header_accepts_a_valid_bearer_token(
    seeded_mcp_db: SeededMcp,  # noqa: F811
) -> None:
    user = seeded_mcp_db.tenant_a
    token = token_for(user, now=NOW)

    identity = identity_from_header(f"Bearer {token}", TEST_JWT_SECRET, NOW)

    assert isinstance(identity, Identity)
    assert identity.user_id == user.user_id
    assert identity.tenant_id == user.tenant_id
    assert isinstance(identity.session_id, uuid.UUID)
    assert isinstance(identity.claims, TokenClaims)
    assert identity.claims.sub == str(user.user_id)


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER", "BeArEr"])
def test_the_bearer_scheme_is_case_insensitive(
    seeded_mcp_db: SeededMcp,  # noqa: F811
    scheme: str,
) -> None:
    """RFC 7235 makes the scheme case-insensitive; a client sending `bearer`
    must not be told it lacks permission."""
    token = token_for(seeded_mcp_db.tenant_a, now=NOW)

    identity = identity_from_header(f"{scheme} {token}", TEST_JWT_SECRET, NOW)

    assert identity.user_id == seeded_mcp_db.tenant_a.user_id


def test_a_token_expiring_exactly_now_is_refused(
    seeded_mcp_db: SeededMcp,  # noqa: F811
) -> None:
    """`exp` names the first instant the token is dead, inclusive boundary."""
    issued_at = NOW - timedelta(hours=8)
    token = token_for(seeded_mcp_db.tenant_a, now=issued_at)

    # One microsecond earlier the same token still verifies: the pair proves
    # the boundary is where it is claimed, not merely that expiry happens.
    assert (
        identity_from_header(f"Bearer {token}", TEST_JWT_SECRET, NOW - timedelta(seconds=1)).user_id
        == seeded_mcp_db.tenant_a.user_id
    )
    with pytest.raises(IdentityRefused):
        identity_from_header(f"Bearer {token}", TEST_JWT_SECRET, NOW)


# --- the same table, through the real transport -------------------------------


def _session_factory_spy(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Record every `_session_factory` call so a refusal can prove it opened none."""
    calls: list[object] = []
    original = pipeline._session_factory

    def spy(settings: Any) -> Any:
        calls.append(settings)
        return original(settings)

    monkeypatch.setattr(pipeline, "_session_factory", spy)
    return calls


@pytest.mark.parametrize(("label", "build"), REFUSAL_CASES, ids=REFUSAL_IDS)
@pytest.mark.parametrize("tool_name", sorted(REGISTRY))
def test_transport_refuses_with_forbidden_and_opens_no_session(
    mcp_env: SeededMcp,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    build: HeaderBuilder,
    tool_name: str,
) -> None:
    calls = _session_factory_spy(monkeypatch)
    header = build(mcp_env)
    headers = {} if header is None else {"Authorization": header}

    result = call_tool(headers, tool_name, {})

    assert error_code(result) == "FORBIDDEN", label
    assert calls == [], f"{label}: a refused call must open no DB session"


def test_a_valid_token_is_not_refused_by_the_identity_seam(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The refusal table is only meaningful if the happy header gets through."""
    result = call_tool(bearer(token_for(mcp_env.tenant_a)), "get_me", {})

    assert result.is_error is False, result.content
