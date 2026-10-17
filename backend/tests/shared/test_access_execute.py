"""`execute_grant`, called directly over the seed.

Surface under test (written before it exists):

``helpdesk.access.execute``
    ``SodConflictAtExecutionError(ToolError)`` — `code = SOD_CONFLICT`.
    ``PolicyBlockedAtExecutionError(ToolError)`` — `code = POLICY_BLOCKED`.
    ``GRANT_EXECUTED_EVENT_TYPE = "grant.executed"``.
    ``async def execute_grant(session, directory, *, request_id,
    requester_user_id, target_user_id, role_id, requested_expiry_days=None,
    granted_by_user_id=None, actor_ref=None, session_id=None, now) -> Grant``
    — rebuilds the access snapshot, re-runs the FULL `policy.engine.classify`
    on it (not `policy/sod.py` alone), then: any `sod_conflicts`
    raises `SodConflictAtExecutionError` and writes no grant; a fresh
    `Decision.BLOCKED` raises `PolicyBlockedAtExecutionError` and writes no
    grant; otherwise it writes ONE `grants` row through the directory port's
    mutation half (`apply_grant`) with `granted_at = now`,
    `expires_at = now + effective_expiry_days`, `status = "active"` and the
    `request_id` it was given, appends ONE `grant.executed` audit event in the
    SAME transaction, and returns the `Grant`.

The seeded database is `test_access_snapshot.py`'s
session-scoped `access_db_url` fixture, imported rather than rebuilt (argon2
seeding is the slow part of this suite). Per-test isolation inside it is a
namespace: each case owns a Production user index nothing else touches, and
row counts are read as before/after deltas for one (user, role) pair.
Identities are recomputed from `scripts.seed.generate_tenant` — deterministic
uuid5 ids, never hardcoded UUIDs.

No pytest-asyncio: each case is a sync function driving one `asyncio.run`.
"""

import asyncio
import contextlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from helpdesk.shared.access.execute import (
    GRANT_EXECUTED_EVENT_TYPE,
    PolicyBlockedAtExecutionError,
    SodConflictAtExecutionError,
    execute_grant,
)
from helpdesk.shared.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.shared.core.errors import ErrorCode
from helpdesk.shared.crud import Repository
from helpdesk.shared.models import AccessRequest, Grant, RequestStatus
from helpdesk.shared.policy.engine import classify
from helpdesk.shared.policy.types import Classification, Decision
from helpdesk.shared.access.directory import SimulatedDirectory
from helpdesk.shared.access.snapshot import build_request_snapshot

# Imported, not redefined: one seeded database for the whole session, shared
# with `test_access_snapshot.py`.
from tests.shared.access.test_access_snapshot import (
    NOW,
    ROWS,
    access_db_url,  # noqa: F401
)

# `now` is the fixed clock every case passes in, so an expiry assertion cannot
# depend on when the suite runs. `NOW` comes from the snapshot module so both
# files classify against the same instant.
EXECUTED_AT = NOW

# Production users this module owns. Indexes chosen to avoid the seed's
# planted anomalies (tenant_index % 20 == 7 is the stale grant, % 15 == 4 the
# out-of-template one; Production starts at tenant_index 46, so indexes 1 and
# 3 are the planted ones and are left to `test_access_snapshot.py`).
SOD_TARGET = ROWS.users_by_department["Production"][10]
LEAVER_TARGET = ROWS.users_by_department["Production"][11]
EXPIRY_TARGET = ROWS.users_by_department["Production"][12]
ROLLBACK_TARGET = ROWS.users_by_department["Production"][13]
REQUESTER = ROWS.users_by_department["Production"][14]

# The SoD pair the re-check must find: `payments.initiator` and
# `payments.approver` (the seeded SoD-STD rule). Production's template holds
# neither, so the conflict exists only because this module creates it.
REQUESTED_ROLE = ROWS.role_ids[("payments", "approver")]
CONFLICTING_ROLE = ROWS.role_ids[("payments", "initiator")]

# Low sensitivity, read, outside Production's template and named by no SoD
# rule: an `elevated` request whose only possible refusal is the leaver check.
LEAVER_ROLE = ROWS.role_ids[("grafana", "viewer")]

# `hris.viewer` is a read role with `default_expiry_days = 365` on a system
# seeded `critical`, so the request classifies `critical` and the class cap
# (30 days) is strictly below the role default. That makes the cap, and not
# the role's own expiry or the requested value, the number under test.
CAPPED_ROLE = ROWS.role_ids[("hris", "viewer")]
CRITICAL_CLASS_CAP_DAYS = 30
ROLE_DEFAULT_EXPIRY_DAYS = 365
REQUESTED_EXPIRY_DAYS = 365


@dataclass(frozen=True)
class GrantFacts:
    """What one written `grants` row says, read inside its own session.

    A detached ORM object would refresh against a closed session, so every
    value the assertions need is copied out before the transaction ends.
    """

    grant_id: uuid.UUID
    user_id: uuid.UUID
    role_id: uuid.UUID
    request_id: uuid.UUID | None
    granted_by_user_id: uuid.UUID | None
    granted_at: datetime
    expires_at: datetime | None
    status: str


def _facts(grant: Grant) -> GrantFacts:
    return GrantFacts(
        grant_id=grant.id,
        user_id=grant.user_id,
        role_id=grant.role_id,
        request_id=grant.request_id,
        granted_by_user_id=grant.granted_by_user_id,
        granted_at=grant.granted_at,
        expires_at=grant.expires_at,
        status=grant.status,
    )


# --- driving the surface -------------------------------------------------------


def _execute(
    url: str,
    *,
    request_id: uuid.UUID | None,
    target_user_id: uuid.UUID,
    role_id: uuid.UUID,
    requested_expiry_days: int | None = None,
    now: datetime = EXECUTED_AT,
) -> GrantFacts:
    """One `execute_grant` in one committed transaction."""

    async def scenario() -> GrantFacts:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                grant = await execute_grant(
                    session,
                    SimulatedDirectory(session),
                    request_id=request_id,
                    requester_user_id=REQUESTER.id,
                    target_user_id=target_user_id,
                    role_id=role_id,
                    requested_expiry_days=requested_expiry_days,
                    granted_by_user_id=REQUESTER.id,
                    actor_ref=str(REQUESTER.id),
                    session_id=None,
                    now=now,
                )
                return _facts(grant)
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


class _Boom(Exception):
    """Raised by the caller AFTER `execute_grant` returned, to roll it back."""


def _execute_then_raise(url: str, *, target_user_id: uuid.UUID, role_id: uuid.UUID) -> uuid.UUID:
    """Execute a grant, then fail the enclosing transaction; return the id."""
    captured: list[uuid.UUID] = []

    async def scenario() -> None:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                grant = await execute_grant(
                    session,
                    SimulatedDirectory(session),
                    request_id=None,
                    requester_user_id=REQUESTER.id,
                    target_user_id=target_user_id,
                    role_id=role_id,
                    granted_by_user_id=REQUESTER.id,
                    actor_ref=str(REQUESTER.id),
                    session_id=None,
                    now=EXECUTED_AT,
                )
                captured.append(grant.id)
                raise _Boom("the caller failed after the grant was written")
        finally:
            await engine.dispose()

    with contextlib.suppress(_Boom):
        asyncio.run(scenario())
    assert len(captured) == 1, "execute_grant must have returned a flushed grant"
    return captured[0]


def _classify(
    url: str,
    *,
    target_user_id: uuid.UUID,
    role_id: uuid.UUID,
    requested_expiry_days: int | None = None,
) -> Classification:
    """The same classification the executor must redo for itself."""

    async def scenario() -> Classification:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                snapshot = await build_request_snapshot(
                    session,
                    SimulatedDirectory(session),
                    requester_user_id=REQUESTER.id,
                    target_user_id=target_user_id,
                    role_id=role_id,
                    requested_expiry_days=requested_expiry_days,
                    now=EXECUTED_AT,
                )
                return classify(snapshot)
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


# --- probes around the surface, never through it -------------------------------


def _grant_count(url: str, user_id: uuid.UUID, role_id: uuid.UUID) -> int:
    async def scenario() -> int:
        engine = create_engine(url)
        try:
            async with engine.connect() as conn:
                total = (
                    await conn.execute(
                        select(func.count())
                        .select_from(Grant)
                        .where(Grant.user_id == user_id, Grant.role_id == role_id)
                    )
                ).scalar_one()
                return int(total)
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _grant_exists(url: str, grant_id: uuid.UUID) -> bool:
    async def scenario() -> bool:
        engine = create_engine(url)
        try:
            async with engine.connect() as conn:
                row = (
                    await conn.execute(select(Grant.id).where(Grant.id == grant_id))
                ).one_or_none()
                return row is not None
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _event_count(url: str, event_type: str) -> int:
    async def scenario() -> int:
        engine = create_engine(url)
        try:
            async with engine.connect() as conn:
                total = (
                    await conn.execute(
                        text(
                            "SELECT count(*) FROM audit_events "
                            "WHERE tenant_id = :tenant AND event_type = :event_type"
                        ),
                        {"tenant": ROWS.tenant_id, "event_type": event_type},
                    )
                ).scalar_one()
                return int(total)
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _insert_grant(url: str, *, user_id: uuid.UUID, role_id: uuid.UUID) -> None:
    """A live grant written around the executor, between classify and execute."""

    async def scenario() -> None:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                await Repository(session, Grant).create(
                    user_id=user_id,
                    role_id=role_id,
                    request_id=None,
                    granted_by_user_id=None,
                    granted_at=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
                    expires_at=None,
                    revoked_at=None,
                    revoke_reason=None,
                    status="active",
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def _insert_request(url: str, *, target_user_id: uuid.UUID, role_id: uuid.UUID) -> uuid.UUID:
    """One committed `access_requests` row for the grant to point at."""

    async def scenario() -> uuid.UUID:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                row = await Repository(session, AccessRequest).create(
                    requester_user_id=REQUESTER.id,
                    target_user_id=target_user_id,
                    role_id=role_id,
                    justification=f"fake execute_grant request {uuid.uuid4().hex}",
                    requested_expiry_days=REQUESTED_EXPIRY_DAYS,
                    risk_class="critical",
                    decision="approval",
                    decision_reasons=[],
                    citations=[],
                    status=RequestStatus.APPROVED.value,
                    approver_user_ids=[],
                    approvals=[],
                    session_id=None,
                    trace_id=None,
                    idempotency_key=None,
                )
                return row.id
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _set_user_status(url: str, user_id: uuid.UUID, status: str) -> None:
    async def scenario() -> None:
        engine = create_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE users SET status = :status WHERE id = :id"),
                    {"status": status, "id": user_id},
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


# --- the SoD re-check -------------------------------


def test_a_conflict_created_after_classification_refuses_and_writes_no_grant(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches an executor that trusts the classification it was handed (or
    the snapshot built moments earlier) instead of rebuilding and re-checking
    at execution time.

    The request classifies clean first — asserted, so the premise is proven,
    not assumed — and the conflicting grant is written afterwards, so the
    conflict exists ONLY in the state a re-check would see. The fresh
    decision is `approval`, never `blocked` on its own, so the neighbouring
    blocked-decision guard cannot produce this refusal: only the SoD half
    can. Zero new `grants` rows is the second half of the promise.
    """
    before = _classify(access_db_url, target_user_id=SOD_TARGET.id, role_id=REQUESTED_ROLE)
    assert before.sod_conflicts == (), "premise: the request must classify clean first"
    assert before.decision is not Decision.BLOCKED, before.reasons
    grants_before = _grant_count(access_db_url, SOD_TARGET.id, REQUESTED_ROLE)

    _insert_grant(access_db_url, user_id=SOD_TARGET.id, role_id=CONFLICTING_ROLE)

    after = _classify(access_db_url, target_user_id=SOD_TARGET.id, role_id=REQUESTED_ROLE)
    assert len(after.sod_conflicts) == 1, "premise: the planted grant completes the pair"

    with pytest.raises(SodConflictAtExecutionError):
        _execute(
            access_db_url,
            request_id=None,
            target_user_id=SOD_TARGET.id,
            role_id=REQUESTED_ROLE,
        )

    assert _grant_count(access_db_url, SOD_TARGET.id, REQUESTED_ROLE) == grants_before


def test_the_execution_errors_carry_their_spec_error_codes() -> None:
    """Pins both codes exactly.

    `test_errors.py`'s `TYPED_ERRORS` table is the repo's usual home for this
    pin, and that file is not edited here, so this assertion is the
    only pin these two errors get. An executor raising `SOD_CONFLICT` for a
    policy block (or `INTERNAL` for either, which is what inheriting
    `ToolError` without setting `code` produces) crosses the MCP boundary as
    the wrong error answer, and every other test here would still pass.
    """
    assert SodConflictAtExecutionError.code is ErrorCode.SOD_CONFLICT
    assert PolicyBlockedAtExecutionError.code is ErrorCode.POLICY_BLOCKED


def test_a_target_who_became_a_leaver_is_refused_and_writes_no_grant(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches an executor that re-runs `policy/sod.py` alone instead of a
    full fresh `classify`: the target turns `leaver` between the
    two moments and the role is named by no SoD rule, so the conflict half of
    the re-check finds nothing and only a re-run of the whole engine refuses.
    Also catches a refusal that still writes the row before raising.
    """
    before = _classify(access_db_url, target_user_id=LEAVER_TARGET.id, role_id=LEAVER_ROLE)
    assert before.decision is not Decision.BLOCKED, "premise: it must classify clean first"
    assert before.sod_conflicts == (), "premise: no SoD rule names this role"
    grants_before = _grant_count(access_db_url, LEAVER_TARGET.id, LEAVER_ROLE)

    _set_user_status(access_db_url, LEAVER_TARGET.id, "leaver")
    try:
        with pytest.raises(PolicyBlockedAtExecutionError):
            _execute(
                access_db_url,
                request_id=None,
                target_user_id=LEAVER_TARGET.id,
                role_id=LEAVER_ROLE,
            )
    finally:
        _set_user_status(access_db_url, LEAVER_TARGET.id, "active")

    assert _grant_count(access_db_url, LEAVER_TARGET.id, LEAVER_ROLE) == grants_before


# --- what a written grant says -------------------------------------------------


def test_the_grant_expires_exactly_one_class_cap_after_granted_at(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches an expiry taken from the wrong number or the wrong clock.

    The role's own default is 365 days and the caller asks for 365, but the
    request classifies `critical`, whose cap is 30 — so an executor using the
    role default, the requested value, or its own `datetime.now()` lands on a
    different instant than `now + 30 days`. The day count is asserted exactly,
    not as a band. Also catches a grant that loses the
    `request_id` it was given (nothing would tie the row to the request that
    authorised it) or is written with a status other than `active`, and an
    executor that skips or duplicates its `grant.executed` event.
    """
    classification = _classify(
        access_db_url,
        target_user_id=EXPIRY_TARGET.id,
        role_id=CAPPED_ROLE,
        requested_expiry_days=REQUESTED_EXPIRY_DAYS,
    )
    assert classification.decision is not Decision.BLOCKED, classification.reasons
    assert classification.effective_expiry_days == CRITICAL_CLASS_CAP_DAYS, (
        "premise: the class cap, not the role default, is the binding number"
    )
    assert ROLE_DEFAULT_EXPIRY_DAYS != CRITICAL_CLASS_CAP_DAYS

    request_id = _insert_request(
        access_db_url, target_user_id=EXPIRY_TARGET.id, role_id=CAPPED_ROLE
    )
    grants_before = _grant_count(access_db_url, EXPIRY_TARGET.id, CAPPED_ROLE)
    events_before = _event_count(access_db_url, GRANT_EXECUTED_EVENT_TYPE)

    grant = _execute(
        access_db_url,
        request_id=request_id,
        target_user_id=EXPIRY_TARGET.id,
        role_id=CAPPED_ROLE,
        requested_expiry_days=REQUESTED_EXPIRY_DAYS,
    )

    assert grant.granted_at == EXECUTED_AT
    assert grant.expires_at == EXECUTED_AT + timedelta(days=CRITICAL_CLASS_CAP_DAYS)
    assert grant.request_id == request_id
    assert grant.user_id == EXPIRY_TARGET.id
    assert grant.role_id == CAPPED_ROLE
    assert grant.granted_by_user_id == REQUESTER.id
    assert grant.status == "active"
    assert _grant_count(access_db_url, EXPIRY_TARGET.id, CAPPED_ROLE) == grants_before + 1
    assert _event_count(access_db_url, GRANT_EXECUTED_EVENT_TYPE) == events_before + 1


def test_the_grant_and_its_event_roll_back_together(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches an executor that commits (or opens its own session for) the
    grant or the audit event: the caller fails AFTER `execute_grant` returned,
    so a grant written in its own transaction survives a rollback that must
    take it, and a `grant.executed` event written outside the caller's
    transaction becomes a record of something that never happened.

    The event is counted rather than assumed absent everywhere: the
    happy-path case above proves one is normally appended, so an executor
    that never writes the event at all fails there, not silently here.
    """
    grants_before = _grant_count(access_db_url, ROLLBACK_TARGET.id, LEAVER_ROLE)
    events_before = _event_count(access_db_url, GRANT_EXECUTED_EVENT_TYPE)

    grant_id = _execute_then_raise(
        access_db_url, target_user_id=ROLLBACK_TARGET.id, role_id=LEAVER_ROLE
    )

    assert _grant_exists(access_db_url, grant_id) is False
    assert _grant_count(access_db_url, ROLLBACK_TARGET.id, LEAVER_ROLE) == grants_before
    assert _event_count(access_db_url, GRANT_EXECUTED_EVENT_TYPE) == events_before
