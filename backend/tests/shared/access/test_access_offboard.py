"""`offboard_user`, called directly over the seed.

Surface under test (written before it exists):

``helpdesk.access.lifecycle``
    ``USER_OFFBOARDED_EVENT_TYPE = "user.offboarded"``.
    ``OFFBOARD_REVOKE_REASON`` — the fixed sentence written onto every
    `grants.revoke_reason` and every `grant.revoked` payload this tool makes,
    so an offboard's revocations are tellable from a manual `revoke_access`
    (caller's free text) and from a transfer's.
    ``LEAVER_STATUS`` — the `users.status` a leaver carries.
    ``AlreadyLeaverError(ToolError)`` — `code = POLICY_BLOCKED`.
    ``async def offboard_user(session, directory, *, user_id, end_date,
    offboarded_by_user_id, actor_ref=None, session_id=None, now)
    -> OffboardResult`` — writes `users.status` and `users.end_date` in the
    BOUND tenant (there is no tenant argument), revokes through
    `helpdesk.access.revoke_role_access`, closes the open requests through
    `helpdesk.access.set_request_status`, and appends one `user.offboarded`
    naming the USER, all in the caller's transaction.
    ``OffboardResult`` carries ``user``, ``revoked_role_ids``,
    ``revoked_grant_count`` and ``closed_request_ids``.

``helpdesk.access.requests``
    ``REQUEST_REJECTED_EVENT_TYPE = "request.rejected"`` — one event per
    closed row, subject the REQUEST, payload naming the request
    and carrying `OFFBOARD_REVOKE_REASON` so an auditor can tell an
    offboard's rejections from a person's.
    ``lock_open_requests_for_target(session, target_user_id)`` — the
    `pending`+`approved` rows TARGETING this user, locked `FOR UPDATE`. Its
    serialising half cannot be observed from one session, so it is pinned end
    to end by `test_mcp_offboard_user.py::
    test_two_callers_racing_the_same_offboard_leave_one_departure`.

The unit of a revocation is `revoke_role_access`'s: the whole live set for
one (user, role), one `grant.revoked` per ROW. So
`revoked_grant_count` counts ROWS and `revoked_role_ids` counts ROLES, and a
role holding two live rows is what tells them apart.

The seeded database is `test_access_snapshot.py`'s
session-scoped `access_db_url` fixture, imported rather than rebuilt (argon2
seeding is the slow part of this suite). Per-test isolation inside it is a
namespace: every case plants its OWN user (a fresh uuid4 address, no
department, so no template is involved) with its own grants and its own
requests, so no case can see another's rows or events. Seeded identities are
recomputed from `scripts.seed.generate_tenant` — deterministic uuid5 ids,
never hardcoded UUIDs.

No pytest-asyncio: each case is a sync function driving one `asyncio.run`.
"""

import asyncio
import contextlib
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import pytest
from sqlalchemy import select, text

from helpdesk.shared.access import requests as requests_module
from helpdesk.shared.access.requests import REQUEST_REJECTED_EVENT_TYPE
from helpdesk.shared.access.revoke import GRANT_REVOKED_EVENT_TYPE
from helpdesk.shared.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.shared.core.errors import ErrorCode
from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.grants import ACTIVE_GRANT_STATUS, REVOKED_GRANT_STATUS
from helpdesk.shared.models import AccessRequest, Grant, RequestStatus, User
from helpdesk.shared.access import offboarding as lifecycle_module
from helpdesk.shared.access.directory import SimulatedDirectory
from helpdesk.shared.access.lifecycle import (
    LEAVER_STATUS,
    OFFBOARD_REVOKE_REASON,
    USER_OFFBOARDED_EVENT_TYPE,
    AlreadyLeaverError,
)
from helpdesk.shared.access.offboarding import offboard_user

# Imported, not redefined: one seeded database for the whole session, shared
# with `test_access_snapshot.py`, `test_access_execute.py`,
# `test_access_revoke.py`, `test_access_lifecycle.py` and
# `test_access_transfer.py`.
from tests.shared.access.test_access_snapshot import (
    NOW,
    ROWS,
    access_db_url,  # noqa: F401
)

# The fixed clock every offboard runs at, so `revoked_at` is never "whenever
# the suite ran". `NOW` comes from the snapshot module, so every file in this
# suite answers for the same instant.
OFFBOARDED_AT = NOW
# The argument. Deliberately NOT `OFFBOARDED_AT.date()`, so an implementation
# that writes `now.date()` into `users.end_date` — the effects take place now
# either way, so nothing else would notice — fails.
END_DATE = date(2026, 10, 31)
# Well past the engine's new-joiner threshold. Nothing here is classified, but
# a planted user needs a start date and a leaver's tenure must not be odd.
TENURED_START = date(2019, 3, 7)

# Whoever runs the offboard. A seeded HR user, read only — no case writes to
# it — so it cannot collide with another module's namespace.
OFFBOARDED_BY = ROWS.users_by_department["HR"][10]
# The third party a leaver's own filed request is about: a real
# seeded user, so the request's FK holds. Read only.
SOMEBODY_ELSE = ROWS.users_by_department["HR"][11]

# Three roles for the held set. None is named by a seeded SoD rule and none is
# in any planted user's template (they have no department), so the only grants
# any case sees are the ones it planted.
ROLE_ONE = ROWS.role_ids[("mes", "viewer")]
ROLE_TWO = ROWS.role_ids[("grafana", "viewer")]
ROLE_THREE = ROWS.role_ids[("crm", "viewer")]
# Named by nothing and held by nobody: the role a concurrent `revoke_access`
# emptied between this tool's read and its revocation.
PHANTOM_ROLE = ROWS.role_ids[("wiki", "viewer")]

# The payload, exactly five keys.
EXPECTED_PAYLOAD_KEYS = {
    "user_id",
    "end_date",
    "revoked_role_ids",
    "revoked_grant_count",
    "closed_request_ids",
}

ACTIVE_USER_STATUS = "active"

# Planted grants get an expiry far ahead of `OFFBOARDED_AT`, so every planted
# row is live and a revocation cannot pass as an expiry.
PLANTED_EXPIRY = datetime(2099, 6, 1, 12, 0, tzinfo=NOW.tzinfo)
PLANTED_GRANTED_AT = datetime(2026, 1, 1, 9, 0, tzinfo=NOW.tzinfo)


class _Boom(Exception):
    """The caller failing after `offboard_user` returned."""


@dataclass(frozen=True)
class GrantFacts:
    """What one `grants` row says, copied out inside its own session."""

    grant_id: uuid.UUID
    role_id: uuid.UUID
    revoked_at: datetime | None
    revoke_reason: str | None
    status: str


@dataclass(frozen=True)
class UserFacts:
    """What one `users` row says about the person's departure."""

    status: str
    end_date: date | None


@dataclass(frozen=True)
class OffboardFacts:
    """One `OffboardResult`, flattened before its session closes."""

    user_id: uuid.UUID
    revoked_role_ids: tuple[uuid.UUID, ...]
    revoked_grant_count: int
    closed_request_ids: tuple[uuid.UUID, ...]


class _WithPhantomRole(SimulatedDirectory):
    """The directory, reporting one role the user does NOT actually hold.

    the race, made deterministic: `offboard_user` reads the held set
    once and then calls `revoke_role_access` per role, which re-locks that
    role's live set. A concurrent `revoke_access` landing between the two
    leaves exactly this shape — a role in the read, an empty live set at the
    revocation, and `GrantNotActiveError` raised for that role alone. Only the
    read is bent here; every write still goes to the real tables.
    """

    def __init__(self, session: Any, phantom_role_id: uuid.UUID) -> None:
        super().__init__(session)
        self._phantom_role_id = phantom_role_id

    async def list_account_role_ids(
        self, user_id: uuid.UUID, now: datetime
    ) -> frozenset[uuid.UUID]:
        held = await super().list_account_role_ids(user_id, now)
        return held | {self._phantom_role_id}


# --- driving the surface -------------------------------------------------------


def _offboard(
    url: str,
    *,
    user_id: uuid.UUID,
    end_date: date = END_DATE,
    now: datetime = OFFBOARDED_AT,
    phantom_role_id: uuid.UUID | None = None,
) -> OffboardFacts:
    """One `offboard_user` in one committed transaction.

    Every fact is copied out INSIDE the transaction that wrote it, which is
    also what proves the transaction survived a skipped role: a refusal caught
    after a statement had been issued would poison it, and both this read and
    the commit at the end of the block would fail.
    """

    async def scenario() -> OffboardFacts:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                directory = (
                    SimulatedDirectory(session)
                    if phantom_role_id is None
                    else _WithPhantomRole(session, phantom_role_id)
                )
                result = await offboard_user(
                    session,
                    directory,
                    user_id=user_id,
                    end_date=end_date,
                    offboarded_by_user_id=OFFBOARDED_BY.id,
                    actor_ref=str(OFFBOARDED_BY.id),
                    session_id=None,
                    now=now,
                )
                return OffboardFacts(
                    user_id=result.user.id,
                    revoked_role_ids=tuple(result.revoked_role_ids),
                    revoked_grant_count=result.revoked_grant_count,
                    closed_request_ids=tuple(result.closed_request_ids),
                )
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _offboard_then_raise(url: str, *, user_id: uuid.UUID) -> None:
    """Offboard, then fail the enclosing transaction."""

    async def scenario() -> None:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                await offboard_user(
                    session,
                    SimulatedDirectory(session),
                    user_id=user_id,
                    end_date=END_DATE,
                    offboarded_by_user_id=OFFBOARDED_BY.id,
                    actor_ref=str(OFFBOARDED_BY.id),
                    session_id=None,
                    now=OFFBOARDED_AT,
                )
                raise _Boom("the caller failed after the offboard was written")
        finally:
            await engine.dispose()

    with contextlib.suppress(_Boom):
        asyncio.run(scenario())


# --- probes around the surface, never through it -------------------------------


def _plant_user(url: str, *, status: str = ACTIVE_USER_STATUS) -> uuid.UUID:
    """One committed, obviously fake user in the seeded tenant, no department."""

    async def scenario() -> uuid.UUID:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                user = await Repository(session, User).create(
                    department_id=None,
                    email=f"fake.leaver.{uuid.uuid4().hex}@fake.invalid",
                    display_name="Fake Leaver",
                    password_hash="not-a-real-hash-this-user-never-logs-in",
                    principals=["employee"],
                    status=status,
                    start_date=TENURED_START,
                    end_date=None,
                )
                return user.id
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _plant_grant(url: str, *, user_id: uuid.UUID, role_id: uuid.UUID) -> uuid.UUID:
    """One committed live `grants` row."""

    async def scenario() -> uuid.UUID:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                grant = await Repository(session, Grant).create(
                    user_id=user_id,
                    role_id=role_id,
                    request_id=None,
                    granted_by_user_id=None,
                    granted_at=PLANTED_GRANTED_AT,
                    expires_at=PLANTED_EXPIRY,
                    revoked_at=None,
                    revoke_reason=None,
                    status=ACTIVE_GRANT_STATUS,
                )
                return grant.id
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _plant_request(
    url: str,
    *,
    requester_user_id: uuid.UUID,
    target_user_id: uuid.UUID,
    role_id: uuid.UUID,
    status: RequestStatus,
) -> uuid.UUID:
    """One committed `access_requests` row in the seeded tenant."""

    async def scenario() -> uuid.UUID:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                row = await Repository(session, AccessRequest).create(
                    requester_user_id=requester_user_id,
                    target_user_id=target_user_id,
                    role_id=role_id,
                    justification=f"fake offboard fixture request {uuid.uuid4().hex}",
                    requested_expiry_days=None,
                    risk_class="critical",
                    decision="approval",
                    decision_reasons=[],
                    citations=[],
                    status=status.value,
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


def _user_facts(url: str, user_id: uuid.UUID) -> UserFacts:
    async def scenario() -> UserFacts:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                user = await Repository(session, User).get(user_id)
                assert user is not None
                return UserFacts(status=user.status, end_date=user.end_date)
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _grants_by_role(url: str, user_id: uuid.UUID) -> dict[uuid.UUID, list[GrantFacts]]:
    async def scenario() -> dict[uuid.UUID, list[GrantFacts]]:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                result = await session.execute(select(Grant).where(Grant.user_id == user_id))
                by_role: dict[uuid.UUID, list[GrantFacts]] = {}
                for grant in result.scalars():
                    by_role.setdefault(grant.role_id, []).append(
                        GrantFacts(
                            grant_id=grant.id,
                            role_id=grant.role_id,
                            revoked_at=grant.revoked_at,
                            revoke_reason=grant.revoke_reason,
                            status=grant.status,
                        )
                    )
                return by_role
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _live_role_ids(url: str, user_id: uuid.UUID) -> frozenset[uuid.UUID]:
    """What the DIRECTORY says this user holds, as of `OFFBOARDED_AT`."""

    async def scenario() -> frozenset[uuid.UUID]:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                return await SimulatedDirectory(session).list_account_role_ids(
                    user_id, OFFBOARDED_AT
                )
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _request_status(url: str, request_id: uuid.UUID) -> str:
    async def scenario() -> str:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                row = await Repository(session, AccessRequest).get(request_id)
                assert row is not None
                return row.status
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


def _events_of_type(url: str, event_type: str) -> list[tuple[str, str, dict[str, Any]]]:
    """Every event of one type in this tenant, in `seq` order."""

    async def scenario() -> list[tuple[str, str, dict[str, Any]]]:
        engine = create_engine(url)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(
                    text(
                        "SELECT subject_type, subject_id, payload FROM audit_events "
                        "WHERE tenant_id = :tenant AND event_type = :event_type ORDER BY seq"
                    ),
                    {"tenant": ROWS.tenant_id, "event_type": event_type},
                )
                return [(str(row[0]), str(row[1]), dict(row[2])) for row in result]
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


# --- the revocations ----------------------------------------


def test_every_live_grant_is_revoked_and_the_count_is_rows_not_roles(
    access_db_url: str,  # noqa: F811
) -> None:
    """Over a leaver holding four live rows across three
       roles.

       Catches the two ways the counts can be wrong at once. An implementation
       that revoked "the grant" for a role — the whole reason for making
       the live SET the unit — leaves the second row of `ROLE_ONE` live, so the
       person still holds the role after their own offboard; the directory's
       answer is asserted EMPTY, which is what that fails. An implementation that
       set `revoked_grant_count` from `len(revoked_role_ids)` answers 3 where the
       contract says 4, and every other assertion here still passes.

       The `grant.revoked` delta is counted as well, so an offboard that wrote
       the rows directly instead of going through `revoke_role_access`
       leaves the tables right and the audit log four events short, and the
       reason on each row is asserted as the exact `OFFBOARD_REVOKE_REASON`
    rather than "some reason", so a transfer's sentence or the
       caller's free text fails.
    """
    user_id = _plant_user(access_db_url)
    first = _plant_grant(access_db_url, user_id=user_id, role_id=ROLE_ONE)
    second = _plant_grant(access_db_url, user_id=user_id, role_id=ROLE_ONE)
    third = _plant_grant(access_db_url, user_id=user_id, role_id=ROLE_TWO)
    fourth = _plant_grant(access_db_url, user_id=user_id, role_id=ROLE_THREE)
    revoked_before = _event_count(access_db_url, GRANT_REVOKED_EVENT_TYPE)

    result = _offboard(access_db_url, user_id=user_id)

    assert result.user_id == user_id
    assert set(result.revoked_role_ids) == {ROLE_ONE, ROLE_TWO, ROLE_THREE}
    assert len(result.revoked_role_ids) == 3, "one entry per ROLE, never per row"
    assert result.revoked_grant_count == 4, "the count is ROWS, and one role holds two"

    by_role = _grants_by_role(access_db_url, user_id)
    assert {grant.grant_id for grant in by_role[ROLE_ONE]} == {first, second}
    for role_id in (ROLE_ONE, ROLE_TWO, ROLE_THREE):
        for grant in by_role[role_id]:
            assert grant.status == REVOKED_GRANT_STATUS
            assert grant.revoked_at == OFFBOARDED_AT
            assert grant.revoke_reason == OFFBOARD_REVOKE_REASON
    assert {grant.grant_id for grant in by_role[ROLE_TWO]} == {third}
    assert {grant.grant_id for grant in by_role[ROLE_THREE]} == {fourth}

    assert _live_role_ids(access_db_url, user_id) == frozenset()
    assert _event_count(access_db_url, GRANT_REVOKED_EVENT_TYPE) == revoked_before + 4, (
        "one grant.revoked per ROW (criterion 3), never one per role and never none"
    )


def test_the_status_becomes_leaver_and_the_end_date_is_the_argument(
    access_db_url: str,  # noqa: F811
) -> None:
    """The other half: catches an offboard that revokes everything
    and leaves the person `active`, and one that writes `now.date()`.

    The status write is what the engine reads to block every new grant
    for a leaver, so an offboard that took the access away without it leaves
    the person able to request it all back the same afternoon. `END_DATE` is
    deliberately a different day from `OFFBOARDED_AT.date()` — the effects
    happen now either way, so nothing else in this file would
    notice a `now.date()` written into the column — and both values are
    asserted exactly, including that they differ.
    """
    user_id = _plant_user(access_db_url)
    assert OFFBOARDED_AT.date() != END_DATE, "premise: the argument is not the clock's date"
    before = _user_facts(access_db_url, user_id)
    assert before.status == ACTIVE_USER_STATUS
    assert before.end_date is None

    _offboard(access_db_url, user_id=user_id)

    after = _user_facts(access_db_url, user_id)
    assert after.status == LEAVER_STATUS
    assert after.status == "leaver"
    assert after.end_date == END_DATE


# --- the closures --------------------------


def test_the_pending_and_approved_requests_targeting_the_user_are_rejected(
    access_db_url: str,  # noqa: F811
) -> None:
    """The case that tells closing only the user's own requests from
        closing everything touching the user.

        The leaver is the TARGET of a `pending` and an `approved` row and the
        REQUESTER of a third row that is about somebody else. An implementation
        that closed "everything touching the user" — the wider reading — would reject that
        third row too and strip a colleague of an approval chain they still need,
        so it is asserted still `pending` and absent from `closed_request_ids`.
        An implementation that closed only the `pending` rows leaves the
        `approved` one, which is exactly the row that can still become a grant,
    reopening the access this tool just removed.

        The status written is asserted as `rejected` and not `expired` or
        `failed`: the spec fixes the vocabulary and `rejected` is the only member
        meaning "ended without being executed". One
        `request.rejected` per closed row is counted, and its subject and payload
        are read: the subject is the REQUEST, so a request's own audit trail does
        not end at its creation, and the payload carries
        `OFFBOARD_REVOKE_REASON`, which is what lets an auditor tell an offboard's
        rejections from a person's.
    """
    user_id = _plant_user(access_db_url)
    pending = _plant_request(
        access_db_url,
        requester_user_id=user_id,
        target_user_id=user_id,
        role_id=ROLE_ONE,
        status=RequestStatus.PENDING,
    )
    approved = _plant_request(
        access_db_url,
        requester_user_id=OFFBOARDED_BY.id,
        target_user_id=user_id,
        role_id=ROLE_TWO,
        status=RequestStatus.APPROVED,
    )
    filed_for_somebody_else = _plant_request(
        access_db_url,
        requester_user_id=user_id,
        target_user_id=SOMEBODY_ELSE.id,
        role_id=ROLE_THREE,
        status=RequestStatus.PENDING,
    )
    rejected_before = _event_count(access_db_url, REQUEST_REJECTED_EVENT_TYPE)

    result = _offboard(access_db_url, user_id=user_id)

    assert set(result.closed_request_ids) == {pending, approved}
    assert filed_for_somebody_else not in result.closed_request_ids
    assert _request_status(access_db_url, pending) == RequestStatus.REJECTED.value
    assert _request_status(access_db_url, approved) == RequestStatus.REJECTED.value
    assert _request_status(access_db_url, filed_for_somebody_else) == RequestStatus.PENDING.value, (
        "a request the leaver FILED for somebody else stays open (decision A)"
    )

    assert _event_count(access_db_url, REQUEST_REJECTED_EVENT_TYPE) == rejected_before + 2
    events = _events_of_type(access_db_url, REQUEST_REJECTED_EVENT_TYPE)[-2:]
    subjects = {subject_id for _, subject_id, _ in events}
    assert subjects == {str(pending), str(approved)}
    for subject_type, subject_id, payload in events:
        assert subject_type == "access_request", "the subject is the REQUEST (decision C)"
        assert payload["request_id"] == subject_id
        assert payload["reason"] == OFFBOARD_REVOKE_REASON


def test_an_executed_and_a_rejected_request_targeting_the_user_are_untouched(
    access_db_url: str,  # noqa: F811
) -> None:
    """The boundary rows: catches "every request targeting the user",
    which the case above cannot tell from the right answer.

    `executed` and `rejected` are both ended already. Rewriting an `executed`
    row to `rejected` would erase the only record that the access was actually
    granted — the grant it produced is revoked by this same call, so nothing
    else would look wrong — and re-closing an already-`rejected` row writes a
    second `request.rejected` for a decision somebody already made. Both
    statuses are re-read and the `request.rejected` delta is asserted zero,
    which is what an over-broad status filter fails.
    """
    user_id = _plant_user(access_db_url)
    executed = _plant_request(
        access_db_url,
        requester_user_id=OFFBOARDED_BY.id,
        target_user_id=user_id,
        role_id=ROLE_ONE,
        status=RequestStatus.EXECUTED,
    )
    already_rejected = _plant_request(
        access_db_url,
        requester_user_id=OFFBOARDED_BY.id,
        target_user_id=user_id,
        role_id=ROLE_TWO,
        status=RequestStatus.REJECTED,
    )
    rejected_before = _event_count(access_db_url, REQUEST_REJECTED_EVENT_TYPE)

    result = _offboard(access_db_url, user_id=user_id)

    assert result.closed_request_ids == ()
    assert _request_status(access_db_url, executed) == RequestStatus.EXECUTED.value
    assert _request_status(access_db_url, already_rejected) == RequestStatus.REJECTED.value
    assert _event_count(access_db_url, REQUEST_REJECTED_EVENT_TYPE) == rejected_before


# --- the refusal, before any write -------------------


def test_an_already_leaver_target_is_refused_before_any_write(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches the "succeed idempotently" alternative, and a
    refusal raised after the first effects have run.

    A second offboard has nothing left to revoke, so a success would write a
    `user.offboarded` recording a departure that already happened, with empty
    lists that read like the person had no access — the audit log gains a
    duplicate departure that no later reader can tell from the real one. The
    event delta is asserted zero for all three domain events.

    The planted leaver deliberately still HOLDS a live grant and has an open
    request, which is what makes "before any write" observable: a refusal
    raised after the revocation loop leaves that grant revoked and the request
    rejected while the caller was told `POLICY_BLOCKED`.
    """
    user_id = _plant_user(access_db_url, status=LEAVER_STATUS)
    grant_id = _plant_grant(access_db_url, user_id=user_id, role_id=ROLE_ONE)
    request_id = _plant_request(
        access_db_url,
        requester_user_id=OFFBOARDED_BY.id,
        target_user_id=user_id,
        role_id=ROLE_TWO,
        status=RequestStatus.PENDING,
    )
    revoked_before = _event_count(access_db_url, GRANT_REVOKED_EVENT_TYPE)
    rejected_before = _event_count(access_db_url, REQUEST_REJECTED_EVENT_TYPE)
    offboarded_before = _event_count(access_db_url, USER_OFFBOARDED_EVENT_TYPE)

    with pytest.raises(AlreadyLeaverError):
        _offboard(access_db_url, user_id=user_id)

    facts = _user_facts(access_db_url, user_id)
    assert facts.status == LEAVER_STATUS
    assert facts.end_date is None, "a refused offboard writes no end date"
    grants = _grants_by_role(access_db_url, user_id)[ROLE_ONE]
    assert len(grants) == 1
    assert grants[0].grant_id == grant_id
    assert grants[0].status == ACTIVE_GRANT_STATUS, "a refused offboard revokes nothing"
    assert grants[0].revoked_at is None
    assert _request_status(access_db_url, request_id) == RequestStatus.PENDING.value
    assert _event_count(access_db_url, GRANT_REVOKED_EVENT_TYPE) == revoked_before
    assert _event_count(access_db_url, REQUEST_REJECTED_EVENT_TYPE) == rejected_before
    assert _event_count(access_db_url, USER_OFFBOARDED_EVENT_TYPE) == offboarded_before


# --- the empty case ----------------------------------


def test_a_user_with_no_grants_and_no_open_requests_offboards_cleanly(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches the alternative that refuses when there is nothing
    to revoke, the way `revoke_access` refuses `GrantNotActiveError`.

    That refusal exists because a caller named ONE grant and must not be told
    it died when it did not; here nobody named anything, and offboarding is
    about the person's state. The refusal would leave a departing person
    `active` forever precisely because they had no access — the one case where
    nothing else in the system would ever notice. The status, the end date and
    a `user.offboarded` carrying EMPTY lists and a zero count are all
    asserted, so an implementation that returned early without the summary
    event fails too.
    """
    user_id = _plant_user(access_db_url)
    offboarded_before = _event_count(access_db_url, USER_OFFBOARDED_EVENT_TYPE)

    result = _offboard(access_db_url, user_id=user_id)

    assert result.revoked_role_ids == ()
    assert result.revoked_grant_count == 0
    assert result.closed_request_ids == ()
    facts = _user_facts(access_db_url, user_id)
    assert facts.status == LEAVER_STATUS
    assert facts.end_date == END_DATE
    events = _events_of_type(access_db_url, USER_OFFBOARDED_EVENT_TYPE)
    assert len(events) == offboarded_before + 1
    _, subject_id, payload = events[-1]
    assert subject_id == str(user_id)
    assert payload["revoked_role_ids"] == []
    assert payload["revoked_grant_count"] == 0
    assert payload["closed_request_ids"] == []


# --- the concurrent revocation -----------------------


def test_a_role_emptied_before_its_revocation_is_omitted_and_the_rest_stands(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches the alternative that lets `GrantNotActiveError`
    propagate, which rolls the WHOLE offboard back because somebody else
    revoked one role first.

    `revoke_role_access` locks per (user, role), not per user, so between this
    tool's read of the held set and its call for a given role the set can go
    empty. The `_WithPhantomRole` view reproduces exactly that state: the read
    reports `PHANTOM_ROLE`, the live set for it is empty, and the revocation
    raises. Under the alternative this whole call raises instead of returning,
    the person stays `active` and their other role stays live — which is what
    every assertion below fails on.

    The phantom role is asserted ABSENT from `revoked_role_ids` (a caught
    error reported as a revocation would be a false record) and the count
    stays at the one real row, so an implementation that counted the attempt
    fails too.
    """
    user_id = _plant_user(access_db_url)
    real_grant = _plant_grant(access_db_url, user_id=user_id, role_id=ROLE_ONE)
    request_id = _plant_request(
        access_db_url,
        requester_user_id=OFFBOARDED_BY.id,
        target_user_id=user_id,
        role_id=ROLE_TWO,
        status=RequestStatus.PENDING,
    )
    assert PHANTOM_ROLE not in _live_role_ids(access_db_url, user_id), (
        "premise: the phantom role's live set is already empty"
    )

    result = _offboard(access_db_url, user_id=user_id, phantom_role_id=PHANTOM_ROLE)

    assert set(result.revoked_role_ids) == {ROLE_ONE}
    assert PHANTOM_ROLE not in result.revoked_role_ids
    assert result.revoked_grant_count == 1
    assert set(result.closed_request_ids) == {request_id}
    facts = _user_facts(access_db_url, user_id)
    assert facts.status == LEAVER_STATUS, "the emptied role costs that role, not the offboard"
    assert facts.end_date == END_DATE
    assert _request_status(access_db_url, request_id) == RequestStatus.REJECTED.value
    grants = _grants_by_role(access_db_url, user_id)[ROLE_ONE]
    assert len(grants) == 1
    assert grants[0].grant_id == real_grant
    assert grants[0].status == REVOKED_GRANT_STATUS


# --- the transaction ---------------------------------------------


def test_the_offboard_and_its_events_roll_back_together(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches an offboard that commits (or opens its own session
    for) the status write, a revocation, a closure or an event.

    The caller fails AFTER `offboard_user` returned, so anything committed in
    its own transaction survives a rollback that must take it. That would
    leave the worst partial state this tool can produce: a person marked
    `leaver` whose grants came back, or grants revoked for somebody the
    directory still calls `active`. All five effects are re-read — status,
    end date, the grant row, the request status and all three event counts.
    """
    user_id = _plant_user(access_db_url)
    grant_id = _plant_grant(access_db_url, user_id=user_id, role_id=ROLE_ONE)
    request_id = _plant_request(
        access_db_url,
        requester_user_id=OFFBOARDED_BY.id,
        target_user_id=user_id,
        role_id=ROLE_TWO,
        status=RequestStatus.PENDING,
    )
    revoked_before = _event_count(access_db_url, GRANT_REVOKED_EVENT_TYPE)
    rejected_before = _event_count(access_db_url, REQUEST_REJECTED_EVENT_TYPE)
    offboarded_before = _event_count(access_db_url, USER_OFFBOARDED_EVENT_TYPE)

    _offboard_then_raise(access_db_url, user_id=user_id)

    facts = _user_facts(access_db_url, user_id)
    assert facts.status == ACTIVE_USER_STATUS
    assert facts.end_date is None
    grants = _grants_by_role(access_db_url, user_id)[ROLE_ONE]
    assert len(grants) == 1
    assert grants[0].grant_id == grant_id
    assert grants[0].status == ACTIVE_GRANT_STATUS
    assert grants[0].revoked_at is None
    assert _request_status(access_db_url, request_id) == RequestStatus.PENDING.value
    assert _event_count(access_db_url, GRANT_REVOKED_EVENT_TYPE) == revoked_before
    assert _event_count(access_db_url, REQUEST_REJECTED_EVENT_TYPE) == rejected_before
    assert _event_count(access_db_url, USER_OFFBOARDED_EVENT_TYPE) == offboarded_before


# --- the audit record --------------------------


def test_user_offboarded_names_the_user_and_carries_exactly_five_keys(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches an event whose subject is the caller or the
    tool rather than the USER, and one whose payload has drifted from the contract's
    five keys.

    An auditor asking "when did this person leave and what did they lose"
    queries `subject_id`; an event subject naming anything else answers that
    for nobody, while a count-only assertion still passes. The keys are
    compared as an exact set, so a dropped `end_date` — the only record of the
    date that was ASKED for, which the immediate effects otherwise
    hide — or a dropped `closed_request_ids` fails here. Every value is
    compared against what the same call returned, so an event that reports the
    intent rather than what actually happened is caught too, and the leaver
    deliberately holds two roles and two open requests so no list can pass as
    `[]`.
    """
    user_id = _plant_user(access_db_url)
    _plant_grant(access_db_url, user_id=user_id, role_id=ROLE_ONE)
    _plant_grant(access_db_url, user_id=user_id, role_id=ROLE_TWO)
    _plant_request(
        access_db_url,
        requester_user_id=OFFBOARDED_BY.id,
        target_user_id=user_id,
        role_id=ROLE_ONE,
        status=RequestStatus.PENDING,
    )
    _plant_request(
        access_db_url,
        requester_user_id=OFFBOARDED_BY.id,
        target_user_id=user_id,
        role_id=ROLE_TWO,
        status=RequestStatus.APPROVED,
    )
    events_before = len(_events_of_type(access_db_url, USER_OFFBOARDED_EVENT_TYPE))

    result = _offboard(access_db_url, user_id=user_id)

    events = _events_of_type(access_db_url, USER_OFFBOARDED_EVENT_TYPE)
    assert len(events) == events_before + 1, "exactly one user.offboarded per offboard"
    subject_type, subject_id, payload = events[-1]
    assert subject_type == "user"
    assert subject_id == str(user_id)
    assert set(payload) == EXPECTED_PAYLOAD_KEYS
    assert payload["user_id"] == str(user_id)
    assert payload["end_date"] == END_DATE.isoformat()
    assert sorted(payload["revoked_role_ids"]) == sorted(
        str(role_id) for role_id in result.revoked_role_ids
    )
    assert payload["revoked_grant_count"] == result.revoked_grant_count
    assert sorted(payload["closed_request_ids"]) == sorted(
        str(request_id) for request_id in result.closed_request_ids
    )
    assert payload["revoked_role_ids"], "premise: this leaver held roles"
    assert payload["closed_request_ids"], "premise: this leaver had open requests"


# --- the constants -------------------------------------------


def test_the_offboard_constants_and_error_code_are_exactly_the_spec_values(
    access_db_url: str,  # noqa: F811
) -> None:
    """Pins every constant this change introduces.

    `AlreadyLeaverError` inheriting `ToolError` without setting `code` answers
    `INTERNAL` — a caller-caused refusal reported as a server fault — and the
    refusal case above passes either way, because it catches by type. Both
    event types are read as constants by every query in this file, so a rename
    would leave the file green and the audit log unqueryable.
    `OFFBOARD_REVOKE_REASON` is what an auditor greps `grants.revoke_reason`
    for to tell a departure's revocations from a transfer's and from a manual
    one, so it is pinned as the exact string.

    The exports are part of the surface: `mcp_server.tools.lifecycle` imports
    from the module that defines each name.
    """
    assert USER_OFFBOARDED_EVENT_TYPE == "user.offboarded"
    assert REQUEST_REJECTED_EVENT_TYPE == "request.rejected"
    assert OFFBOARD_REVOKE_REASON == "offboarded: the user left the organisation"
    assert LEAVER_STATUS == "leaver"
    assert AlreadyLeaverError.code is ErrorCode.POLICY_BLOCKED
    assert AlreadyLeaverError.code.value == "POLICY_BLOCKED"
    assert lifecycle_module.offboard_user is offboard_user
    assert lifecycle_module.USER_OFFBOARDED_EVENT_TYPE == USER_OFFBOARDED_EVENT_TYPE
    assert lifecycle_module.OFFBOARD_REVOKE_REASON == OFFBOARD_REVOKE_REASON
    assert requests_module.REQUEST_REJECTED_EVENT_TYPE == REQUEST_REJECTED_EVENT_TYPE
