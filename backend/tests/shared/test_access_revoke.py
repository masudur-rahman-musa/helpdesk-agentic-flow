"""An earlier change, sections 1 and 6: `revoke_role_access`, called directly over the seed.

Surface under test (written before it exists):

``helpdesk.access.revoke``
    ``GrantNotActiveError(ToolError)`` — `code = POLICY_BLOCKED` (decision C).
    ``GRANT_REVOKED_EVENT_TYPE = "grant.revoked"`` (the design).
    ``REVOKED_GRANT_STATUS = "revoked"`` — the `grants.status` a revoked row
    carries.
    ``async def revoke_role_access(session, directory, *, user_id, role_id,
    reason, revoked_by_user_id, actor_ref, session_id, now) -> list[Grant]``
    — locks the LIVE set for `(user_id, role_id)`
    (`core.repository.lock_live_grants_for_role`, `SELECT … FOR UPDATE`),
    raises `GrantNotActiveError` when that set is empty, and otherwise revokes
    EVERY row in it through the directory port's `revoke_grant` (decision A
    and F), appending one `grant.revoked` audit event per row, all in the
    caller's transaction.

Decision A is the whole point of this module: an earlier change leaves two live rows
for one `(user, role)` reachable (two requests under two idempotency keys),
so a revocation that touches only one row reports success while access
survives.

a project rule: the seeded database is `test_access_snapshot.py`'s
session-scoped `access_db_url` fixture, imported rather than rebuilt (argon2
seeding is the slow part of this suite). Per-test isolation inside it is a
namespace: each case owns a Production user index nothing else revokes for,
and every probe is scoped to one `(user, role)` pair. Identities are
recomputed from `scripts.seed.generate_tenant` — deterministic uuid5 ids,
never hardcoded UUIDs.

No pytest-asyncio: each case is a sync function driving one `asyncio.run`.
"""

import asyncio
import contextlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select, text

from helpdesk.shared.access.revoke import (
    GRANT_REVOKED_EVENT_TYPE,
    REVOKED_GRANT_STATUS,
    GrantNotActiveError,
    revoke_grant_row,
    revoke_role_access,
)
from helpdesk.shared.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.shared.core.errors import ErrorCode
from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.grants import (
    ACTIVE_GRANT_STATUS,
    is_live_grant,
    lock_live_grants_for_role,
)
from helpdesk.shared.models import AccessRequest, Grant, RequestStatus
from helpdesk.shared.access.directory import SimulatedDirectory

# Imported, not redefined: one seeded database for the whole session, shared
# with `test_access_snapshot.py`, `test_access_execute.py` and
# `test_access_approvals.py`.
from tests.shared.access.test_access_snapshot import (
    NOW,
    ROWS,
    access_db_url,  # noqa: F401
)

# The fixed clock every case revokes at, so `revoked_at` is never "whenever
# the suite ran". `NOW` comes from the snapshot module, so every file in this
# suite answers for the same instant.
REVOKED_AT = NOW

GRANTED_AT = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
# Strictly before `REVOKED_AT`: an expired row is not live and must be left
# exactly as it is.
PAST_EXPIRY = datetime(2026, 2, 1, 9, 0, tzinfo=UTC)
# When the historical revocation below happened, and what it said. A second
# revocation that rewrote it would erase the record of the first.
HISTORIC_REVOKED_AT = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
HISTORIC_REASON = "fake historical revocation, must not be rewritten"

REASON = "fake revocation reason for the test suite"

# Production users this module owns. Indexes 0-3 belong to
# `test_access_snapshot.py`, 10-14 to `test_access_execute.py` and 20-30 to
# `test_access_approvals.py`, so nothing here changes a row another module
# reads. 33 is skipped: the seed plants an out-of-template grant on every
# Production index where `(46 + index) % 15 == 4`.
SIMPLE_TARGET = ROWS.users_by_department["Production"][34]
DOUBLE_TARGET = ROWS.users_by_department["Production"][35]
HISTORY_TARGET = ROWS.users_by_department["Production"][36]
INACTIVE_TARGET = ROWS.users_by_department["Production"][37]
EVENT_TARGET = ROWS.users_by_department["Production"][38]
ROLLBACK_TARGET = ROWS.users_by_department["Production"][39]
DIRECTORY_TARGET = ROWS.users_by_department["Production"][40]
REVOKER = ROWS.users_by_department["Production"][42]
# Added at review round 1, for the two cases below that pin claims the first
# eight made without one.
PREDICATE_TARGET = ROWS.users_by_department["Production"][43]
ORDER_TARGET = ROWS.users_by_department["Production"][44]
# An earlier change: the row `revoke_role_access` cannot reach at all (decision A).
LAPSED_TARGET = ROWS.users_by_department["Production"][45]

# Distinct instants for the ordering case, both before `REVOKED_AT`.
OLDER_GRANTED_AT = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
NEWER_GRANTED_AT = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)

# Outside Production's template, so the only grants any target holds on it are
# the ones a case plants (asserted as a premise before every plant).
REVOKED_ROLE = ROWS.role_ids[("grafana", "viewer")]
# A second role, held live by the same user in the "leave the neighbours
# alone" case: revoking one role must not revoke the other.
NEIGHBOUR_ROLE = ROWS.role_ids[("plm", "engineer")]

EXPECTED_PAYLOAD_KEYS = {"user_id", "role_id", "request_id", "reason", "revoked_count"}


@dataclass(frozen=True)
class GrantFacts:
    """What one `grants` row says, copied out before its session closes.

    A detached ORM object would refresh against a closed session, so every
    value the assertions need is read inside the transaction that owns it.
    """

    grant_id: uuid.UUID
    user_id: uuid.UUID
    role_id: uuid.UUID
    request_id: uuid.UUID | None
    revoked_at: datetime | None
    revoke_reason: str | None
    status: str


def _facts(grant: Grant) -> GrantFacts:
    return GrantFacts(
        grant_id=grant.id,
        user_id=grant.user_id,
        role_id=grant.role_id,
        request_id=grant.request_id,
        revoked_at=grant.revoked_at,
        revoke_reason=grant.revoke_reason,
        status=grant.status,
    )


# --- driving the surface -------------------------------------------------------


def _revoke(
    url: str,
    *,
    user_id: uuid.UUID,
    role_id: uuid.UUID,
    reason: str = REASON,
    now: datetime = REVOKED_AT,
) -> list[GrantFacts]:
    """One `revoke_role_access` in one committed transaction."""

    async def scenario() -> list[GrantFacts]:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                grants = await revoke_role_access(
                    session,
                    SimulatedDirectory(session),
                    user_id=user_id,
                    role_id=role_id,
                    reason=reason,
                    revoked_by_user_id=REVOKER.id,
                    actor_ref=str(REVOKER.id),
                    session_id=None,
                    now=now,
                )
                return [_facts(grant) for grant in grants]
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


class _Boom(Exception):
    """Raised by the caller AFTER `revoke_role_access` returned, to roll it back."""


def _revoke_then_raise(url: str, *, user_id: uuid.UUID, role_id: uuid.UUID) -> int:
    """Revoke, then fail the enclosing transaction; return how many rows it revoked."""
    captured: list[int] = []

    async def scenario() -> None:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                grants = await revoke_role_access(
                    session,
                    SimulatedDirectory(session),
                    user_id=user_id,
                    role_id=role_id,
                    reason=REASON,
                    revoked_by_user_id=REVOKER.id,
                    actor_ref=str(REVOKER.id),
                    session_id=None,
                    now=REVOKED_AT,
                )
                captured.append(len(grants))
                raise _Boom("the caller failed after the revocation was written")
        finally:
            await engine.dispose()

    with contextlib.suppress(_Boom):
        asyncio.run(scenario())
    assert captured == [1], "revoke_role_access must have revoked exactly one flushed row"
    return captured[0]


def _account_role_ids(url: str, user_id: uuid.UUID) -> frozenset[uuid.UUID]:
    """What the DIRECTORY says this user holds, as of `REVOKED_AT`."""

    async def scenario() -> frozenset[uuid.UUID]:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                return await SimulatedDirectory(session).list_account_role_ids(user_id, REVOKED_AT)
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


# --- probes around the surface, never through it -------------------------------


def _plant_grant(
    url: str,
    *,
    user_id: uuid.UUID,
    role_id: uuid.UUID,
    request_id: uuid.UUID | None = None,
    granted_at: datetime = GRANTED_AT,
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
    revoke_reason: str | None = None,
    status: str = ACTIVE_GRANT_STATUS,
) -> uuid.UUID:
    """One committed `grants` row written around the revoker."""

    async def scenario() -> uuid.UUID:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                row = await Repository(session, Grant).create(
                    user_id=user_id,
                    role_id=role_id,
                    request_id=request_id,
                    granted_by_user_id=None,
                    granted_at=granted_at,
                    expires_at=expires_at,
                    revoked_at=revoked_at,
                    revoke_reason=revoke_reason,
                    status=status,
                )
                return row.id
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _plant_request(url: str, *, target_user_id: uuid.UUID, role_id: uuid.UUID) -> uuid.UUID:
    """One committed `access_requests` row for a planted grant to point at."""

    async def scenario() -> uuid.UUID:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                row = await Repository(session, AccessRequest).create(
                    requester_user_id=REVOKER.id,
                    target_user_id=target_user_id,
                    role_id=role_id,
                    justification=f"fake revoke fixture request {uuid.uuid4().hex}",
                    requested_expiry_days=None,
                    risk_class="standard",
                    decision="auto",
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


def _rows_for(url: str, user_id: uuid.UUID, role_id: uuid.UUID) -> list[GrantFacts]:
    """Every `grants` row for one (user, role) pair, oldest id order aside."""

    async def scenario() -> list[GrantFacts]:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                result = await session.execute(
                    select(Grant).where(Grant.user_id == user_id, Grant.role_id == role_id)
                )
                return [_facts(grant) for grant in result.scalars()]
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _row(url: str, grant_id: uuid.UUID) -> GrantFacts:
    async def scenario() -> GrantFacts:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                result = await session.execute(select(Grant).where(Grant.id == grant_id))
                return _facts(result.scalars().one())
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _revoked_events(url: str) -> list[tuple[str, str, dict[str, Any]]]:
    """Every `grant.revoked` event in this tenant, in `seq` order."""

    async def scenario() -> list[tuple[str, str, dict[str, Any]]]:
        engine = create_engine(url)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(
                    text(
                        "SELECT subject_type, subject_id, payload FROM audit_events "
                        "WHERE tenant_id = :tenant AND event_type = :event_type ORDER BY seq"
                    ),
                    {"tenant": ROWS.tenant_id, "event_type": GRANT_REVOKED_EVENT_TYPE},
                )
                return [(str(row[0]), str(row[1]), dict(row[2])) for row in result]
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _premise_no_grants(url: str, user_id: uuid.UUID, role_id: uuid.UUID) -> None:
    """This module owns the (user, role) pair it is about to plant on."""
    assert _rows_for(url, user_id, role_id) == [], (
        "premise: no other seeded or planted row exists for this (user, role) pair"
    )


# --- what a revocation writes (criteria 2 and 3) -------------------------------


def test_revoking_marks_the_row_revoked_with_the_reason_and_timestamp(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches a revocation that leaves `status` `active`.

    Three independent columns say a grant is gone (`status`, `revoked_at`,
    `revoke_reason`) and `list_active_grants_for_user` reads two of them, so
    an implementation writing only `revoked_at` looks correct to the reader
    while the row still says `active` to anything filtering on status alone.
    Every one is asserted as an exact value: `revoked_at` against the `now`
    the caller passed (a revoker reading its own clock lands elsewhere), the
    reason byte for byte, and the status against `REVOKED_GRANT_STATUS`,
    itself pinned to the exact string `"revoked"` here
    because every other case in this file reads the constant and would pass
    whatever it says.
    """
    assert REVOKED_GRANT_STATUS == "revoked"
    assert REVOKED_GRANT_STATUS != ACTIVE_GRANT_STATUS
    _premise_no_grants(access_db_url, SIMPLE_TARGET.id, REVOKED_ROLE)
    grant_id = _plant_grant(access_db_url, user_id=SIMPLE_TARGET.id, role_id=REVOKED_ROLE)

    revoked = _revoke(access_db_url, user_id=SIMPLE_TARGET.id, role_id=REVOKED_ROLE)

    assert [facts.grant_id for facts in revoked] == [grant_id]
    row = _row(access_db_url, grant_id)
    assert row.status == REVOKED_GRANT_STATUS
    assert row.revoked_at == REVOKED_AT
    assert row.revoke_reason == REASON


def test_a_second_live_row_for_the_same_role_is_revoked_too(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches decision A regressing to one row.

    An earlier change leaves two live rows for one `(user, role)` reachable — two
    requests under two idempotency keys both execute — so a revoker that
    takes the first row it finds reports success, writes an audit event
    saying access ended, and leaves the user holding the role. Both ids are
    asserted in the returned set, so a caller is never misled about the scope
    of what happened, and both rows are re-read to prove the second was
    written as well as reported.
    """
    _premise_no_grants(access_db_url, DOUBLE_TARGET.id, REVOKED_ROLE)
    first = _plant_grant(access_db_url, user_id=DOUBLE_TARGET.id, role_id=REVOKED_ROLE)
    second = _plant_grant(access_db_url, user_id=DOUBLE_TARGET.id, role_id=REVOKED_ROLE)
    assert first != second

    revoked = _revoke(access_db_url, user_id=DOUBLE_TARGET.id, role_id=REVOKED_ROLE)

    assert {facts.grant_id for facts in revoked} == {first, second}
    for grant_id in (first, second):
        row = _row(access_db_url, grant_id)
        assert row.status == REVOKED_GRANT_STATUS, grant_id
        assert row.revoked_at == REVOKED_AT
        assert row.revoke_reason == REASON


def test_an_expired_and_an_already_revoked_row_are_left_alone(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches a revocation written as an unfiltered UPDATE over the pair.

    Decision A says "every LIVE row", not "every row". A statement without
    the liveness conjuncts rewrites a historical revocation's reason and
    timestamp — erasing who ended that access and why, which is the one thing
    the column exists to keep — and stamps a fresh `revoked_at` on a grant
    that simply expired, turning an expiry into a revocation in the audit
    record. A neighbouring role held live by the same user is asserted
    untouched too, so a revoker keyed on the user alone fails here.
    """
    _premise_no_grants(access_db_url, HISTORY_TARGET.id, REVOKED_ROLE)
    _premise_no_grants(access_db_url, HISTORY_TARGET.id, NEIGHBOUR_ROLE)
    live = _plant_grant(access_db_url, user_id=HISTORY_TARGET.id, role_id=REVOKED_ROLE)
    expired = _plant_grant(
        access_db_url,
        user_id=HISTORY_TARGET.id,
        role_id=REVOKED_ROLE,
        expires_at=PAST_EXPIRY,
    )
    already = _plant_grant(
        access_db_url,
        user_id=HISTORY_TARGET.id,
        role_id=REVOKED_ROLE,
        revoked_at=HISTORIC_REVOKED_AT,
        revoke_reason=HISTORIC_REASON,
        status=REVOKED_GRANT_STATUS,
    )
    neighbour = _plant_grant(access_db_url, user_id=HISTORY_TARGET.id, role_id=NEIGHBOUR_ROLE)

    revoked = _revoke(access_db_url, user_id=HISTORY_TARGET.id, role_id=REVOKED_ROLE)

    assert [facts.grant_id for facts in revoked] == [live]

    expired_row = _row(access_db_url, expired)
    assert expired_row.revoked_at is None, "an expiry is not a revocation"
    assert expired_row.revoke_reason is None
    assert expired_row.status == ACTIVE_GRANT_STATUS

    already_row = _row(access_db_url, already)
    assert already_row.revoked_at == HISTORIC_REVOKED_AT
    assert already_row.revoke_reason == HISTORIC_REASON

    neighbour_row = _row(access_db_url, neighbour)
    assert neighbour_row.status == ACTIVE_GRANT_STATUS
    assert neighbour_row.revoked_at is None


# --- the refusal (decision C) --------------------------------------------------


def test_revoking_an_inactive_grant_raises_policy_blocked(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches an empty live set treated as a successful no-op.

    A revoker that returns `[]` instead of raising tells the caller access was
    removed when nothing was, and writes a `grant.revoked` count of zero into
    the audit trail. The pair holds one expired and one already-revoked row —
    so the rows exist and only the LIVENESS test can produce this refusal —
    and both are asserted untouched, which is the second half of the promise:
    a refusal writes nothing at all.
    """
    _premise_no_grants(access_db_url, INACTIVE_TARGET.id, REVOKED_ROLE)
    expired = _plant_grant(
        access_db_url,
        user_id=INACTIVE_TARGET.id,
        role_id=REVOKED_ROLE,
        expires_at=PAST_EXPIRY,
    )
    already = _plant_grant(
        access_db_url,
        user_id=INACTIVE_TARGET.id,
        role_id=REVOKED_ROLE,
        revoked_at=HISTORIC_REVOKED_AT,
        revoke_reason=HISTORIC_REASON,
        status=REVOKED_GRANT_STATUS,
    )
    events_before = len(_revoked_events(access_db_url))

    with pytest.raises(GrantNotActiveError):
        _revoke(access_db_url, user_id=INACTIVE_TARGET.id, role_id=REVOKED_ROLE)

    assert _row(access_db_url, expired).revoked_at is None
    assert _row(access_db_url, already).revoke_reason == HISTORIC_REASON
    assert len(_revoked_events(access_db_url)) == events_before


def test_the_revoke_errors_carry_their_spec_error_codes() -> None:
    """Pins the code exactly, as the enum member and as the
    string that crosses the MCP boundary.

    Decision C reserves `NOT_FOUND` for "missing, or another tenant's", which
    is what keeps a grant id from being an existence oracle. A
    `GrantNotActiveError` carrying `NOT_FOUND` would tell a caller who
    legitimately reached the grant that it never existed, and one that
    inherits `ToolError` without setting `code` at all answers `INTERNAL` —
    an already-revoked grant filed as an outage. Every other case here passes
    either way.
    """
    assert GrantNotActiveError.code is ErrorCode.POLICY_BLOCKED
    assert GrantNotActiveError.code.value == "POLICY_BLOCKED"


# --- the audit record (criterion 4, a project rule) ---------------------------


def test_one_event_per_revoked_row_names_the_grant_and_the_reason(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches an event whose subject is the user, the role or the tool
    rather than the GRANT, and a summary event written once for a set of two.

    An auditor asking "what happened to grant X" queries `subject_id`; one
    event per call, or an event subject naming the user, answers nothing for
    every revocation in the tenant while a count-only assertion still passes. The payload keys are asserted as an exact set, so a
    dropped `reason` (the only record of WHY the access ended) or a
    `revoked_count` that reports 1 for a set of 2 fails here. The event type
    itself is pinned to the exact the design string: every
    query in this file reads the constant, so a renamed one would leave the
    file green and the audit log unqueryable. `request_id` is
    read per row — one planted grant points at a request and the other at
    nothing — so an event copying one row's request onto both is caught.
    """
    assert GRANT_REVOKED_EVENT_TYPE == "grant.revoked"
    _premise_no_grants(access_db_url, EVENT_TARGET.id, REVOKED_ROLE)
    request_id = _plant_request(access_db_url, target_user_id=EVENT_TARGET.id, role_id=REVOKED_ROLE)
    with_request = _plant_grant(
        access_db_url,
        user_id=EVENT_TARGET.id,
        role_id=REVOKED_ROLE,
        request_id=request_id,
    )
    without_request = _plant_grant(access_db_url, user_id=EVENT_TARGET.id, role_id=REVOKED_ROLE)
    events_before = len(_revoked_events(access_db_url))

    revoked = _revoke(access_db_url, user_id=EVENT_TARGET.id, role_id=REVOKED_ROLE)
    assert {facts.grant_id for facts in revoked} == {with_request, without_request}

    events = _revoked_events(access_db_url)
    assert len(events) == events_before + 2, "one event per revoked row (decision D)"
    written = events[events_before:]
    assert {subject_type for subject_type, _, _ in written} == {"grant"}
    assert {subject_id for _, subject_id, _ in written} == {
        str(with_request),
        str(without_request),
    }
    by_subject = {subject_id: payload for _, subject_id, payload in written}
    for subject_id, payload in by_subject.items():
        assert set(payload) == EXPECTED_PAYLOAD_KEYS, subject_id
        assert payload["user_id"] == str(EVENT_TARGET.id)
        assert payload["role_id"] == str(REVOKED_ROLE)
        assert payload["reason"] == REASON
        assert payload["revoked_count"] == 2
    assert by_subject[str(with_request)]["request_id"] == str(request_id)
    assert by_subject[str(without_request)]["request_id"] is None


def test_the_revocations_and_their_events_roll_back_together(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches a revoker that commits (or opens its own session for) the row
    or the audit event: the caller fails AFTER `revoke_role_access` returned,
    so a revocation committed in its own transaction survives a rollback that
    must take it, and a `grant.revoked` event written outside the caller's
    transaction becomes a record of access that is still live.

    The event is counted rather than assumed absent everywhere: the case
    above proves one is normally appended per row, so a revoker that never
    writes the event at all fails there, not silently here.
    """
    _premise_no_grants(access_db_url, ROLLBACK_TARGET.id, REVOKED_ROLE)
    grant_id = _plant_grant(access_db_url, user_id=ROLLBACK_TARGET.id, role_id=REVOKED_ROLE)
    events_before = len(_revoked_events(access_db_url))

    _revoke_then_raise(access_db_url, user_id=ROLLBACK_TARGET.id, role_id=REVOKED_ROLE)

    row = _row(access_db_url, grant_id)
    assert row.status == ACTIVE_GRANT_STATUS
    assert row.revoked_at is None
    assert row.revoke_reason is None
    assert len(_revoked_events(access_db_url)) == events_before


# --- the two predicates, and the order (review round 1) -------------------------


def _locked_and_predicate_ids(
    url: str, user_id: uuid.UUID, role_id: uuid.UUID
) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    """What the locked SQL read returns, and what the Python predicate keeps,
    over the same rows in one transaction."""

    async def scenario() -> tuple[list[uuid.UUID], list[uuid.UUID]]:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                locked = await lock_live_grants_for_role(session, user_id, role_id, REVOKED_AT)
                everything = await session.execute(
                    select(Grant).where(Grant.user_id == user_id, Grant.role_id == role_id)
                )
                by_predicate = [
                    grant.id for grant in everything.scalars() if is_live_grant(grant, REVOKED_AT)
                ]
                return [grant.id for grant in locked], by_predicate
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def test_the_python_liveness_predicate_agrees_with_the_locked_read(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches the two halves of "live" drifting apart.

    `revoke_access` answers for the NAMED grant with `is_live_grant` and then
    locks the set with SQL, so a disagreement is a caller told
    `POLICY_BLOCKED` for a grant every read tool still shows as live, or a
    grant admitted by the tool and then reported missing by the lock. Four
    states are planted — live, expired, `revoked_at` set, status `revoked` — so
    each conjunct has a row only it excludes, and the two answers are compared
    as sets rather than assumed.
    """
    _premise_no_grants(access_db_url, PREDICATE_TARGET.id, REVOKED_ROLE)
    live = _plant_grant(access_db_url, user_id=PREDICATE_TARGET.id, role_id=REVOKED_ROLE)
    _plant_grant(
        access_db_url,
        user_id=PREDICATE_TARGET.id,
        role_id=REVOKED_ROLE,
        expires_at=PAST_EXPIRY,
    )
    _plant_grant(
        access_db_url,
        user_id=PREDICATE_TARGET.id,
        role_id=REVOKED_ROLE,
        revoked_at=HISTORIC_REVOKED_AT,
        revoke_reason=HISTORIC_REASON,
    )
    _plant_grant(
        access_db_url,
        user_id=PREDICATE_TARGET.id,
        role_id=REVOKED_ROLE,
        status=REVOKED_GRANT_STATUS,
    )

    locked, by_predicate = _locked_and_predicate_ids(
        access_db_url, PREDICATE_TARGET.id, REVOKED_ROLE
    )

    assert set(locked) == set(by_predicate)
    assert set(locked) == {live}, "exactly one of the four states is live"


def test_the_revoked_set_comes_back_oldest_first(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches the `order_by` being dropped from the locked read.

    Without it Postgres returns the rows in whatever order the plan produces,
    so the `grant.revoked` sequence for one revocation becomes run-dependent
    and an auditor reading the chain cannot tell a stable record from a
    reordered one. The two rows are planted NEWEST FIRST, so an unordered read
    that happens to return insertion order fails.
    """
    _premise_no_grants(access_db_url, ORDER_TARGET.id, REVOKED_ROLE)
    newer = _plant_grant(
        access_db_url,
        user_id=ORDER_TARGET.id,
        role_id=REVOKED_ROLE,
        granted_at=NEWER_GRANTED_AT,
    )
    older = _plant_grant(
        access_db_url,
        user_id=ORDER_TARGET.id,
        role_id=REVOKED_ROLE,
        granted_at=OLDER_GRANTED_AT,
    )

    revoked = _revoke(access_db_url, user_id=ORDER_TARGET.id, role_id=REVOKED_ROLE)

    assert [facts.grant_id for facts in revoked] == [older, newer]


# --- the directory's own answer (criterion 3) -----------------------------------


def test_the_role_is_gone_from_the_directorys_account_roles(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches a revocation that satisfies every column assertion above and
    still leaves the user HOLDING the role, which is the only thing that
    matters to the person whose access it was.

    Two live rows are planted, so a revoker that stops after the first leaves
    the role in `list_account_role_ids` — the same query
    `list_active_grants_for_user` (and therefore `list_my_access`) answers
    from — while the row it did revoke reads perfectly. A live neighbouring
    role is asserted still present, so a directory answer of "nothing at all"
    cannot pass this case.
    """
    _premise_no_grants(access_db_url, DIRECTORY_TARGET.id, REVOKED_ROLE)
    _premise_no_grants(access_db_url, DIRECTORY_TARGET.id, NEIGHBOUR_ROLE)
    _plant_grant(access_db_url, user_id=DIRECTORY_TARGET.id, role_id=REVOKED_ROLE)
    _plant_grant(access_db_url, user_id=DIRECTORY_TARGET.id, role_id=REVOKED_ROLE)
    _plant_grant(access_db_url, user_id=DIRECTORY_TARGET.id, role_id=NEIGHBOUR_ROLE)
    assert REVOKED_ROLE in _account_role_ids(access_db_url, DIRECTORY_TARGET.id), (
        "premise: the directory reports the role before it is revoked"
    )

    _revoke(access_db_url, user_id=DIRECTORY_TARGET.id, role_id=REVOKED_ROLE)

    held = _account_role_ids(access_db_url, DIRECTORY_TARGET.id)
    assert REVOKED_ROLE not in held
    assert NEIGHBOUR_ROLE in held, "only the revoked role may disappear"


# --- an earlier change: the one row unit (`revoke_grant_row`, decision A) -----------------


def test_revoke_grant_row_revokes_a_lapsed_but_active_row_the_live_set_cannot_reach(
    access_db_url: str,  # noqa: F811
) -> None:
    """An earlier change decision A: the case that forced the extraction, and the only
    case in this file that `revoke_role_access` answers differently.

    The row is `status = 'active'` with an expiry already in the past, so
    `lock_live_grants_for_role` returns nothing for its (user, role) and
    `revoke_role_access` refuses it `POLICY_BLOCKED` — asserted here as the
    premise, because that refusal IS why a second entry point exists. A
    review item is exactly such a row (12c flags it `stale`), so an
    implementation of `revoke_grant_row` that re-applied the liveness filter,
    or that delegated back to the live set, would leave the most reviewable
    grant in the tenant unrevocable while every other case here still passed.

    Everything the set unit writes is asserted for the row unit too, so the
    extraction cannot quietly become a second, thinner writer: all three
    columns, and one `grant.revoked` naming this grant with the reason and
    the `revoked_count` the CALLER passed — the count is the review's own (1
    row), not a number the function may invent from a set it never read.
    """
    _premise_no_grants(access_db_url, LAPSED_TARGET.id, REVOKED_ROLE)
    grant_id = _plant_grant(
        access_db_url,
        user_id=LAPSED_TARGET.id,
        role_id=REVOKED_ROLE,
        expires_at=PAST_EXPIRY,
    )
    with pytest.raises(GrantNotActiveError):
        _revoke(access_db_url, user_id=LAPSED_TARGET.id, role_id=REVOKED_ROLE)
    events_before = len(_revoked_events(access_db_url))

    async def scenario() -> GrantFacts:
        engine = create_engine(access_db_url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                result = await session.execute(select(Grant).where(Grant.id == grant_id))
                revoked = await revoke_grant_row(
                    session,
                    SimulatedDirectory(session),
                    grant=result.scalars().one(),
                    reason=REASON,
                    revoked_by_user_id=REVOKER.id,
                    actor_ref=str(REVOKER.id),
                    session_id=None,
                    revoked_count=1,
                    now=REVOKED_AT,
                )
                return _facts(revoked)
        finally:
            await engine.dispose()

    facts = asyncio.run(scenario())

    assert facts.grant_id == grant_id
    row = _row(access_db_url, grant_id)
    assert row.status == REVOKED_GRANT_STATUS
    assert row.revoked_at == REVOKED_AT
    assert row.revoke_reason == REASON
    events = _revoked_events(access_db_url)
    assert len(events) == events_before + 1, "one grant.revoked, for the one row"
    subject_type, subject_id, payload = events[-1]
    assert subject_type == "grant"
    assert subject_id == str(grant_id)
    assert set(payload) == EXPECTED_PAYLOAD_KEYS
    assert payload["user_id"] == str(LAPSED_TARGET.id)
    assert payload["role_id"] == str(REVOKED_ROLE)
    assert payload["reason"] == REASON
    assert payload["revoked_count"] == 1
