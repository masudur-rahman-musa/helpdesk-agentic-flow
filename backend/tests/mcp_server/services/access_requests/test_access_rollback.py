"""`rollback_request`, called directly over the seed.

Surface under test (written before it exists):

``helpdesk.access.rollback``
    ``ROLLBACK_WINDOW = timedelta(hours=24)`` — the contract's window.
    ``ROLLBACK_REVOKE_REASON = "rollback"`` — the literal written to
    `grants.revoke_reason` and to the `grant.revoked` payload;
    the caller's own text is carried by `grant.rolled_back` instead.
    ``GRANT_ROLLED_BACK_EVENT_TYPE = "grant.rolled_back"``.
    ``def within_rollback_window(granted_at, now) -> bool`` — pure, inclusive
    at exactly `granted_at + ROLLBACK_WINDOW`.
    ``RequestNotExecutedError(ToolError)`` and
    ``RollbackWindowExpiredError(ToolError)`` — both `POLICY_BLOCKED`. The
    third refusal, "no live grant left", reuses
    `helpdesk.access.revoke.GrantNotActiveError` rather than inventing a
    third error.
    ``RollbackResult`` — frozen, carrying ``.request`` and ``.revoked_grants``.
    ``async def rollback_request(session, directory, *, request, reason,
    rolled_back_by_user_id, actor_ref=None, session_id=None, now)
    -> RollbackResult`` — reads the request's LIVE grants under
    `core.repository.lock_live_grants_for_request` (`SELECT … FOR UPDATE`),
    raises `GrantNotActiveError` when that set is empty, revokes every row
    through the shared `revoke_grant_row` writer with the LITERAL reason, and
    appends one `grant.rolled_back` per row naming the GRANT, before moving
    the request to `RequestStatus.ROLLED_BACK`. All in the caller's
    transaction.

The status test and the window test live in the MCP handler, not here, so
this file pins the two errors' codes rather than the branches that raise
them; `test_mcp_rollback_request.py` owns those.

The seeded database is `test_access_snapshot.py`'s
session-scoped `access_db_url` fixture, imported rather than rebuilt (argon2
seeding is the slow part of this suite). Per-test isolation inside it is a
namespace: every case plants its OWN department, system, role, people,
request and grants (uuid4-suffixed names), so no other module's rows are
read or written.

No pytest-asyncio: each case is a sync function driving one `asyncio.run`.
"""

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.mcp_server.services.access_requests.rollback import (
    GRANT_ROLLED_BACK_EVENT_TYPE,
    ROLLBACK_REVOKE_REASON,
    ROLLBACK_WINDOW,
    RequestNotExecutedError,
    RollbackWindowExpiredError,
    rollback_request,
    within_rollback_window,
)
from helpdesk.shared.access.directory import SimulatedDirectory
from helpdesk.shared.access.revoke import GRANT_REVOKED_EVENT_TYPE, GrantNotActiveError
from helpdesk.shared.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.grants import ACTIVE_GRANT_STATUS, REVOKED_GRANT_STATUS
from helpdesk.shared.models import (
    AccessRequest,
    Base,
    Department,
    Grant,
    RequestStatus,
    Role,
    System,
    User,
)

# Imported, not redefined: one seeded database for the whole session, shared
# with `test_access_snapshot.py` and its siblings.
from tests.shared.access.test_access_snapshot import (
    NOW,
    ROWS,
    access_db_url,  # noqa: F401
)

# The fixed instant every rollback answers for, so nothing here depends on
# when the suite runs. `NOW` comes from the snapshot module, so every file in
# this suite shares one clock.
ROLLED_BACK_AT = NOW
# Comfortably inside the 24h window, so no case below can be refused by the
# handler's window test (which this file does not exercise at all).
GRANTED_AT = ROLLED_BACK_AT - timedelta(hours=1)
FAR_EXPIRY = datetime(2099, 6, 1, 12, 0, tzinfo=UTC)

# What a historical revocation said, for the row a rollback must not reach.
HISTORIC_REVOKED_AT = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
HISTORIC_REASON = "fake historical revocation, must not be rewritten"

# The caller's text. Deliberately NOT the literal `rollback`: the column gets the literal and this
# text goes to `grant.rolled_back`, and
# a case whose two strings were equal could not tell those apart.
CALLER_REASON = "fake rollback reason typed by the fake admin"

PLANTED_START = date(2019, 3, 7)
PLANTED_PASSWORD_HASH = "not-a-real-hash-this-user-never-logs-in"

ROLLED_BACK_SUBJECT_TYPE = "grant"
EXPECTED_ROLLED_BACK_PAYLOAD_KEYS = {
    "request_id",
    "user_id",
    "role_id",
    "granted_at",
    "expires_at",
    "reason",
    "rolled_back_count",
}


# --- running one scenario in one transaction -----------------------------------


def in_tenant[T](url: str, work: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Run `work` in one committed tenant-scoped transaction."""

    async def scenario() -> T:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                return await work(session)
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


# --- the planted request -------------------------------------------------------


@dataclass(frozen=True)
class Planted:
    """One executed request, its target and every grant row it produced."""

    request_id: uuid.UUID
    target_user_id: uuid.UUID
    role_id: uuid.UUID
    admin_user_id: uuid.UUID
    grant_ids: tuple[uuid.UUID, ...]


def plant(
    url: str,
    *,
    grants: int = 1,
    status: RequestStatus = RequestStatus.EXECUTED,
    granted_at: datetime = GRANTED_AT,
    expires_at: datetime | None = FAR_EXPIRY,
    revoked: bool = False,
) -> Planted:
    """A whole department, cast, request and grant set, owned by one case."""

    async def work(session: AsyncSession) -> Planted:
        department = await Repository(session, Department).create(
            name=f"Fake Rollback Dept {uuid.uuid4().hex[:12]}", manager_user_id=None
        )

        async def add_user() -> uuid.UUID:
            row = await Repository(session, User).create(
                department_id=department.id,
                email=f"fake.rollback.{uuid.uuid4().hex}@fake.invalid",
                display_name="Fake Rollback Person",
                password_hash=PLANTED_PASSWORD_HASH,
                principals=["employee"],
                status="active",
                start_date=PLANTED_START,
                end_date=None,
            )
            return row.id

        target_user_id = await add_user()
        admin_user_id = await add_user()
        system = await Repository(session, System).create(
            slug=f"fake-sys-{uuid.uuid4().hex[:12]}",
            name="Fake Rollback System",
            sensitivity="low",
            category="other",
            owner_user_id=None,
        )
        role = await Repository(session, Role).create(
            system_id=system.id,
            slug=f"fake-role-{uuid.uuid4().hex[:12]}",
            name="Fake Rollback Role",
            privilege="read",
            default_expiry_days=365,
        )
        request = await Repository(session, AccessRequest).create(
            requester_user_id=target_user_id,
            target_user_id=target_user_id,
            role_id=role.id,
            justification=f"fake rollback fixture request {uuid.uuid4().hex}",
            requested_expiry_days=None,
            risk_class="low",
            decision="auto",
            decision_reasons=[],
            citations=[],
            status=status.value,
            approver_user_ids=[],
            approvals=[],
            session_id=None,
            trace_id=None,
            idempotency_key=None,
        )
        grant_ids: list[uuid.UUID] = []
        for _ in range(grants):
            row = await Repository(session, Grant).create(
                user_id=target_user_id,
                role_id=role.id,
                request_id=request.id,
                granted_by_user_id=admin_user_id,
                granted_at=granted_at,
                expires_at=expires_at,
                revoked_at=HISTORIC_REVOKED_AT if revoked else None,
                revoke_reason=HISTORIC_REASON if revoked else None,
                status=REVOKED_GRANT_STATUS if revoked else ACTIVE_GRANT_STATUS,
            )
            grant_ids.append(row.id)
        return Planted(
            request_id=request.id,
            target_user_id=target_user_id,
            role_id=role.id,
            admin_user_id=admin_user_id,
            grant_ids=tuple(grant_ids),
        )

    return in_tenant(url, work)


# --- driving the surface -------------------------------------------------------


@dataclass(frozen=True)
class RollbackFacts:
    """What one `rollback_request` returned, copied out before its session closes."""

    request_status: str
    revoked_grant_ids: tuple[uuid.UUID, ...]


async def _call(
    session: AsyncSession,
    planted: Planted,
    *,
    reason: str = CALLER_REASON,
    now: datetime = ROLLED_BACK_AT,
) -> RollbackFacts:
    request = await Repository(session, AccessRequest).get(planted.request_id)
    assert request is not None, "premise: the planted request is readable in this tenant"
    result = await rollback_request(
        session,
        SimulatedDirectory(session),
        request=request,
        reason=reason,
        rolled_back_by_user_id=planted.admin_user_id,
        actor_ref=str(planted.admin_user_id),
        session_id=None,
        now=now,
    )
    return RollbackFacts(
        request_status=result.request.status,
        revoked_grant_ids=tuple(grant.id for grant in result.revoked_grants),
    )


def roll_back(
    url: str,
    planted: Planted,
    *,
    reason: str = CALLER_REASON,
    now: datetime = ROLLED_BACK_AT,
) -> RollbackFacts:
    """One `rollback_request` in one committed transaction."""

    async def work(session: AsyncSession) -> RollbackFacts:
        return await _call(session, planted, reason=reason, now=now)

    return in_tenant(url, work)


class _Boom(Exception):
    """Raised by the caller AFTER `rollback_request` returned, to roll it back."""


def roll_back_then_raise(url: str, planted: Planted) -> int:
    """Roll back, then fail the enclosing transaction; return how many rows it revoked."""
    captured: list[int] = []

    async def work(session: AsyncSession) -> None:
        facts = await _call(session, planted)
        captured.append(len(facts.revoked_grant_ids))
        raise _Boom("the caller failed after the rollback was written")

    with contextlib.suppress(_Boom):
        in_tenant(url, work)
    assert captured == [1], "rollback_request must have revoked exactly one flushed row"
    return captured[0]


# --- probes around the surface, never through it -------------------------------


@dataclass(frozen=True)
class GrantFacts:
    """What one `grants` row says, read outside the transaction that wrote it."""

    revoked_at: datetime | None
    revoke_reason: str | None
    status: str
    granted_at: datetime
    expires_at: datetime | None


def grant_row(url: str, grant_id: uuid.UUID) -> GrantFacts:
    async def work(session: AsyncSession) -> GrantFacts:
        row = await Repository(session, Grant).get(grant_id)
        assert row is not None, f"expected a grant row for {grant_id}"
        return GrantFacts(
            revoked_at=row.revoked_at,
            revoke_reason=row.revoke_reason,
            status=row.status,
            granted_at=row.granted_at,
            expires_at=row.expires_at,
        )

    return in_tenant(url, work)


def request_status(url: str, request_id: uuid.UUID) -> str:
    async def work(session: AsyncSession) -> str:
        row = await Repository(session, AccessRequest).get(request_id)
        assert row is not None, f"expected an access request row for {request_id}"
        return row.status

    return in_tenant(url, work)


def events_for(url: str, subject_id: uuid.UUID) -> list[tuple[str, str, dict[str, Any]]]:
    """Every audit event filed against one subject, in `seq` order.

    Filtered by subject rather than counted over the tenant: the seeded
    database carries thousands of events, and only the ones naming this grant
    say anything about this case.
    """

    async def scenario() -> list[tuple[str, str, dict[str, Any]]]:
        engine = create_engine(url)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(
                    text(
                        "SELECT event_type, subject_type, payload FROM audit_events"
                        " WHERE tenant_id = :tenant AND subject_id = :subject ORDER BY seq"
                    ),
                    {"tenant": ROWS.tenant_id, "subject": str(subject_id)},
                )
                return [(str(row[0]), str(row[1]), dict(row[2])) for row in result]
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


# --- the window ----------------------------------------------------


WINDOW_CASES: list[tuple[str, timedelta, bool]] = [
    ("at the grant instant", timedelta(0), True),
    ("one microsecond inside the edge", timedelta(hours=24, microseconds=-1), True),
    ("exactly twenty-four hours", timedelta(hours=24), True),
    ("one microsecond past the edge", timedelta(hours=24, microseconds=1), False),
    ("a full day past", timedelta(hours=48), False),
]


@pytest.mark.parametrize(
    ("elapsed", "inside"),
    [case[1:] for case in WINDOW_CASES],
    ids=[case[0] for case in WINDOW_CASES],
)
def test_the_rollback_window_is_inclusive_at_exactly_24_hours(
    elapsed: timedelta, inside: bool
) -> None:
    """Pins the boundary from both sides, one microsecond apart.

    KILL, row by row: `exactly twenty-four hours` is the only row that fails
    when the comparison is written `<` instead of `<=`, and `one microsecond
    past the edge` is the only row that fails when the window is widened (a
    `timedelta(days=1, seconds=1)`, or a comparison against `now` in whole
    hours). A one-sided table cannot tell an inclusive bound from an exclusive
    one, which is exactly what the window rule fixes.

    `ROLLBACK_WINDOW` itself is asserted as the exact value:
    every other case in this file and in `test_mcp_rollback_request.py` reads
    the constant or a time relative to it, so a window silently changed to 48h
    would leave them green.
    """
    assert timedelta(hours=24) == ROLLBACK_WINDOW
    assert ROLLBACK_WINDOW.total_seconds() == 86400

    assert within_rollback_window(GRANTED_AT, GRANTED_AT + elapsed) is inside


# --- the reason ---------------------------------------------------------------


def test_the_rollback_reason_written_to_the_grant_is_the_literal_rollback(
    access_db_url: str,  # noqa: F811
) -> None:
    """The rollback literal on the COLUMN.

    KILL: passing the caller's `reason` through to `revoke_grant_row` instead
    of `ROLLBACK_REVOKE_REASON`. The caller's text and the literal are
    different strings here on purpose, so that one substitution is the only
    change that flips this case; the event test below fails for the opposite
    substitution (the literal reaching `grant.rolled_back`), so the two cannot
    be satisfied by one wrong reason.

    The literal is pinned as the exact string `"rollback"`:
    it is the contract verbatim and what the sweeps and the admin views
    match on, and every other assertion here reads the constant.
    """
    assert ROLLBACK_REVOKE_REASON == "rollback"
    assert ROLLBACK_REVOKE_REASON != CALLER_REASON
    planted = plant(access_db_url)

    facts = roll_back(access_db_url, planted, reason=CALLER_REASON)

    assert facts.revoked_grant_ids == planted.grant_ids
    row = grant_row(access_db_url, planted.grant_ids[0])
    assert row.revoke_reason == ROLLBACK_REVOKE_REASON
    assert row.status == REVOKED_GRANT_STATUS
    assert row.revoked_at == ROLLED_BACK_AT


# --- the two events -----------------------------------------------------------


def test_a_rolled_back_row_writes_grant_revoked_and_grant_rolled_back(
    access_db_url: str,  # noqa: F811
) -> None:
    """both events, both subjects, every payload key, for one row.

    KILL: filing `grant.rolled_back` against the REQUEST instead of the grant
    (`subject_type="access_request"`, `subject_id=request.id`). Nothing else in
    either file reads this event's subject, and a rollback event that names the
    request answers nothing for an auditor asking what happened to grant X —
    which is the whole point of the two events.

    The payload keys are an exact set, so a dropped `reason` (the only record
    of WHY, since the column carries the literal) or a missing original
    `expires_at` (what the contract's reinstatement half would need) fails here.
    The values are the ORIGINALS read off the row before it died: `granted_at`
    and `expires_at` are asserted against the planted instants, so an
    implementation writing `now` or the post-revocation state fails.

    Both events are asserted in one ordered list for the one subject, so a
    rollback that skips the shared `grant.revoked` writer — leaving a row that
    reads differently to anyone counting revocations — fails too.
    """
    assert GRANT_ROLLED_BACK_EVENT_TYPE == "grant.rolled_back"
    planted = plant(access_db_url)
    grant_id = planted.grant_ids[0]
    assert events_for(access_db_url, grant_id) == [], (
        "premise: this freshly planted grant carries no audit history"
    )

    roll_back(access_db_url, planted)

    written = events_for(access_db_url, grant_id)
    assert [event_type for event_type, _, _ in written] == [
        GRANT_REVOKED_EVENT_TYPE,
        GRANT_ROLLED_BACK_EVENT_TYPE,
    ]
    revoked_payload = written[0][2]
    assert written[0][1] == ROLLED_BACK_SUBJECT_TYPE
    assert revoked_payload["reason"] == ROLLBACK_REVOKE_REASON, "decision B, in the event too"
    assert revoked_payload["revoked_count"] == 1

    subject_type, payload = written[1][1], written[1][2]
    assert subject_type == ROLLED_BACK_SUBJECT_TYPE
    assert set(payload) == EXPECTED_ROLLED_BACK_PAYLOAD_KEYS
    assert payload["request_id"] == str(planted.request_id)
    assert payload["user_id"] == str(planted.target_user_id)
    assert payload["role_id"] == str(planted.role_id)
    assert payload["granted_at"] == GRANTED_AT.isoformat()
    assert payload["expires_at"] == FAR_EXPIRY.isoformat()
    assert payload["reason"] == CALLER_REASON, "the CALLER's text, never the literal"
    assert payload["rolled_back_count"] == 1


def test_a_rolled_back_row_carries_a_null_expiry_when_the_grant_had_none(
    access_db_url: str,  # noqa: F811
) -> None:
    """The nullable half of the same payload.

    KILL: an unguarded `grant.expires_at.isoformat()`, which raises
    `AttributeError` on a grant with no expiry — an `INTERNAL` error across a
    boundary whose contract is the eight typed the contract codes. Every other
    case here plants an expiry, so this row is the only one that reaches the
    `None` branch at all; a never-expiring grant is what `create_access_request`
    writes whenever the role's `default_expiry_days` is absent.
    """
    planted = plant(access_db_url, expires_at=None)
    grant_id = planted.grant_ids[0]

    roll_back(access_db_url, planted)

    payload = events_for(access_db_url, grant_id)[-1][2]
    assert payload["expires_at"] is None
    assert payload["granted_at"] == GRANTED_AT.isoformat()


# --- the refusals (criteria 5 and 6) ---------------------------------------------


def test_the_rollback_errors_carry_their_spec_error_codes() -> None:
    """Pins both codes exactly, as the enum member and as the
    string that crosses the MCP boundary.

    KILL: `class RequestNotExecutedError(ToolError): pass` — an error that
    inherits without setting `code` answers `INTERNAL`, so a request in the
    wrong status is filed as an outage and the caller is told to retry
    something that will never work. The same for the window error.

    `NOT_FOUND` is deliberately not either of these: it is reserved for the two
    absences that must stay indistinguishable (missing, and another tenant's),
    and a refusal here means the caller reached a real request of their own.
    The third refusal, "no live grant left", is `GrantNotActiveError` reused
    from `revoke.py` rather than a third class, so its code is pinned by
    `test_access_revoke.py::test_the_revoke_errors_carry_their_spec_error_codes`;
    asserted here only as "all three answer the same code", which is what
    the tool promises a caller.
    """
    assert issubclass(RequestNotExecutedError, ToolError)
    assert issubclass(RollbackWindowExpiredError, ToolError)
    assert RequestNotExecutedError.code is ErrorCode.POLICY_BLOCKED
    assert RequestNotExecutedError.code.value == "POLICY_BLOCKED"
    assert RollbackWindowExpiredError.code is ErrorCode.POLICY_BLOCKED
    assert RollbackWindowExpiredError.code.value == "POLICY_BLOCKED"
    assert GrantNotActiveError.code is ErrorCode.POLICY_BLOCKED


def test_a_request_whose_grants_are_all_gone_raises_grant_not_active(
    access_db_url: str,  # noqa: F811
) -> None:
    """The empty-set refusal, at the only seam where it is separable.

    KILL: deleting the empty-set refusal in the domain, which would mark the
    request `rolled_back` while nothing was undone — an audit record of an undo
    that undid nothing. This cannot be pinned at the transport, where the
    window refusal and this one both answer `POLICY_BLOCKED` and either could
    be the guard that spoke: here the exception CLASS
    separates them.

    The grant is planted inside the window (`GRANTED_AT`, one hour old), so the
    window predicate would admit it and only its liveness can refuse. The
    historical revocation is re-read afterwards: a refusal must rewrite nothing,
    and the request must still read `executed`.
    """
    planted = plant(access_db_url, revoked=True)

    with pytest.raises(GrantNotActiveError):
        roll_back(access_db_url, planted)

    assert request_status(access_db_url, planted.request_id) == RequestStatus.EXECUTED.value
    row = grant_row(access_db_url, planted.grant_ids[0])
    assert row.revoked_at == HISTORIC_REVOKED_AT
    assert row.revoke_reason == HISTORIC_REASON
    assert events_for(access_db_url, planted.grant_ids[0]) == []


# --- the transaction (the caller's, never its own) ----------------------------


def test_the_revocations_and_their_events_roll_back_together(
    access_db_url: str,  # noqa: F811
) -> None:
    """The domain runs in the CALLER's transaction.

    KILL: a `session.commit()` (or a session of its own) anywhere in
    `rollback_request`. The caller fails AFTER the call returned, so a
    revocation committed on the way out survives a rollback that must take it,
    and the request would read `rolled_back` while the grant it named is alive
    — the exact state the contract exists to prevent.

    The events are counted rather than assumed absent everywhere: the case
    above proves two are normally written for this row, so an implementation
    that never writes them at all fails there, not silently here.
    """
    planted = plant(access_db_url)
    grant_id = planted.grant_ids[0]

    roll_back_then_raise(access_db_url, planted)

    row = grant_row(access_db_url, grant_id)
    assert row.status == ACTIVE_GRANT_STATUS
    assert row.revoked_at is None
    assert row.revoke_reason is None
    assert request_status(access_db_url, planted.request_id) == RequestStatus.EXECUTED.value
    assert events_for(access_db_url, grant_id) == []


# --- the reinstatement half has no reachable input ----------------------------


def test_the_only_executed_artefact_of_a_request_is_a_grant() -> None:
    """Read off the schema rather than argued.

    the contract has two halves: a grant is revoked, and a revocation is
    reinstated with its original expiry. This change implements the first only,
    on the grounds that no request in the model executes a revocation — so the
    second half has no input a `request_id` could reach.

    KILL: adding any second table that carries a `request_id` (a `revocations`
    or `reinstatements` table, the natural way to make a revocation a
    first-class artefact). That is precisely the change that makes
    the contract's reinstatement branch reachable and this grant-only rollback
    incomplete, and nothing else in either file would notice it. A test over
    the tool inputs could not say this: `rollback_request` itself takes a
    request id and revokes, so "no revoking tool takes a request id" is false.
    """
    referencing = {
        table.name
        for table in Base.metadata.sorted_tables
        for column in table.columns
        for foreign_key in column.foreign_keys
        if foreign_key.column.table.name == AccessRequest.__tablename__
    }

    assert referencing == {Grant.__tablename__}
