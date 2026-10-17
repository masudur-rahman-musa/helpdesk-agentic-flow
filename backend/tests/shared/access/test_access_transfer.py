"""`transfer_user`, called directly over the seed.

Surface under test (written before it exists):

``helpdesk.access.lifecycle``
    ``USER_TRANSFERRED_EVENT_TYPE = "user.transferred"``.
    ``TRANSFER_REVOKE_REASON`` — the `grants.revoke_reason` every revocation
    this tool makes carries, so a transfer's revocations are tellable from a
    manual one in the table and in the event.
    ``SameDepartmentError(ToolError)`` — `code = POLICY_BLOCKED`.
    ``LeaverTransferError(ToolError)`` — `code = POLICY_BLOCKED`.
    ``async def transfer_user(session, directory, *, user_id,
    new_department_id, effective_date, transferred_by_user_id,
    actor_ref=None, session_id=None, now) -> TransferResult`` — writes
    `users.department_id` in the BOUND tenant (there is no tenant argument,
    so the tenant comes from the token), revokes through `helpdesk.access.revoke_role_access`,
    applies through `helpdesk.access.execute_grant`, and appends one
    `user.transferred` naming the USER, all in the caller's transaction.
    ``TransferResult`` carries ``user``, ``from_department_id``,
    ``revoked_role_ids``, ``applied_role_ids``, ``skipped``
    (``SkippedRole`` records, the dataclass), ``retained_role_ids``
    and ``non_standard_role_ids``.

``helpdesk.core.repository``
    ``lock_user(session, user_id)`` — the bound tenant's user row, locked
    `FOR UPDATE` for the caller's transaction. Its serialising
    half cannot be observed from one session, so it is pinned end to end by
    `test_mcp_transfer_user.py::
    test_two_callers_racing_the_same_transfer_leave_one_move`.

The four sets, at one instant `now`:
    revoke = held & old template, minus the new template;
    apply = new template, minus what is held;
    retained = held & new template, untouched (same grant id, same expiry);
    non-standard = held, minus both templates, left live.

The seeded database is `test_access_snapshot.py`'s
session-scoped `access_db_url` fixture, imported rather than rebuilt (argon2
seeding is the slow part of this suite). Per-test isolation inside it is a
namespace: every case plants its OWN two departments (fresh uuid names) with
their own template rows and its OWN user (a fresh uuid4 address), so no case
can see another's users, grants or events. Seeded identities are recomputed
from `scripts.seed.generate_tenant` — deterministic uuid5 ids, never
hardcoded UUIDs.

No pytest-asyncio: each case is a sync function driving one `asyncio.run`.
"""

import asyncio
import contextlib
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text

from helpdesk.shared.access.execute import GRANT_EXECUTED_EVENT_TYPE
from helpdesk.shared.access.revoke import GRANT_REVOKED_EVENT_TYPE
from helpdesk.shared.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.shared.core.errors import ErrorCode
from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.grants import ACTIVE_GRANT_STATUS, REVOKED_GRANT_STATUS
from helpdesk.shared.models import Department, Grant, RoleTemplate, User
from helpdesk.shared.policy.engine import classify
from helpdesk.shared.access import transfers as lifecycle_module
from helpdesk.shared.access.directory import SimulatedDirectory
from helpdesk.shared.access.lifecycle import (
    TRANSFER_REVOKE_REASON,
    USER_TRANSFERRED_EVENT_TYPE,
    LeaverTransferError,
    SameDepartmentError,
)
from helpdesk.shared.access.transfers import transfer_user
from helpdesk.shared.access.snapshot import build_request_snapshot

# Imported, not redefined: one seeded database for the whole session, shared
# with `test_access_snapshot.py`, `test_access_execute.py`,
# `test_access_revoke.py` and `test_access_lifecycle.py`.
from tests.shared.access.test_access_snapshot import (
    NOW,
    ROWS,
    access_db_url,  # noqa: F401
)

# The fixed clock every transfer runs at, so an expiry assertion cannot depend
# on when the suite runs. `NOW` comes from the snapshot module, so every file
# in this suite answers for the same instant.
TRANSFERRED_AT = NOW
# Carried in the event and nowhere else: there is no column for
# it and no scheduler yet. Deliberately NOT `now`'s date, so an
# implementation that writes `now.date()` into the payload fails.
EFFECTIVE_DATE = date(2026, 9, 15)
# Well over the engine's 30-day NEW_JOINER threshold, so no case here is
# classified by tenure: the template membership is the only thing that moves
# a class, which is what the case rests on.
TENURED_START = date(2019, 3, 7)

# Whoever runs the transfer. A seeded HR user, read only — no case writes to
# it — so it cannot collide with another module's namespace.
TRANSFERRED_BY = ROWS.users_by_department["HR"][6]
PLANTED_MANAGER = ROWS.users_by_department["HR"][7]

# The four corners of the diff, each in exactly one of the four sets.
#   OLD_ONLY       mes.viewer      old template only        -> revoked
#   BOTH           wiki.viewer     both templates           -> retained
#   NEW_ONLY       grafana.viewer  new template only        -> applied
#   NON_STANDARD   crm.viewer      neither template, held   -> left live
OLD_ONLY_ROLE = ROWS.role_ids[("mes", "viewer")]
BOTH_ROLE = ROWS.role_ids[("wiki", "viewer")]
NEW_ONLY_ROLE = ROWS.role_ids[("grafana", "viewer")]
NON_STANDARD_ROLE = ROWS.role_ids[("crm", "viewer")]

# The engine's numbers for `grafana.viewer` (low sensitivity, read privilege,
# tenured target), which is what makes the expiry choice observable:
#   in the department's template   -> low        -> cap 365
#   outside it                     -> elevated   -> cap 180
# Both are asserted against the live engine as premises below, so a policy
# change fails the premise rather than silently rewriting the expectation.
IN_TEMPLATE_EXPIRY_DAYS = 365
OUT_OF_TEMPLATE_EXPIRY_DAYS = 180

# `hris.viewer` is a read role whose own `default_expiry_days` is 365, and the
# engine caps it at 30 because `hris` is a critical system. That gap is what
# "the engine's number, not the role's" means here.
CAPPED_ROLE = ROWS.role_ids[("hris", "viewer")]
CAPPED_EXPIRY_DAYS = 30
CAPPED_ROLE_DEFAULT_EXPIRY_DAYS = 365

# The seeded SoD pair (the standard separation-of-duties rule). The target holds the initiator half
# as a NON-STANDARD grant (in neither template, so it is retained rather than
# revoked) and the new template names the approver half, so the application is
# refused at execution while everything else stands.
SOD_HELD_ROLE = ROWS.role_ids[("payments", "initiator")]
SOD_TEMPLATE_ROLE = ROWS.role_ids[("payments", "approver")]

# The payload, exactly nine keys.
EXPECTED_PAYLOAD_KEYS = {
    "user_id",
    "from_department_id",
    "to_department_id",
    "effective_date",
    "revoked_role_ids",
    "applied_role_ids",
    "skipped_role_ids",
    "retained_role_ids",
    "non_standard_role_ids",
}

ACTIVE_USER_STATUS = "active"
LEAVER_USER_STATUS = "leaver"

# Planted grants get an expiry far from anything the engine would compute, so
# a retained grant that was silently re-granted (a new row, or the same row
# re-stamped) cannot pass as untouched.
PLANTED_EXPIRY = datetime(2099, 6, 1, 12, 0, tzinfo=NOW.tzinfo)
PLANTED_GRANTED_AT = datetime(2026, 1, 1, 9, 0, tzinfo=NOW.tzinfo)


class _Boom(Exception):
    """The caller failing after `transfer_user` returned."""


@dataclass(frozen=True)
class GrantFacts:
    """What one `grants` row says, copied out inside its own session."""

    grant_id: uuid.UUID
    user_id: uuid.UUID
    role_id: uuid.UUID
    granted_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    revoke_reason: str | None
    status: str


@dataclass(frozen=True)
class TransferFacts:
    """One `TransferResult`, flattened before its session closes."""

    user_id: uuid.UUID
    department_id: uuid.UUID | None
    from_department_id: uuid.UUID | None
    revoked_role_ids: tuple[uuid.UUID, ...]
    applied_role_ids: tuple[uuid.UUID, ...]
    skipped: tuple[tuple[uuid.UUID, str], ...]
    retained_role_ids: tuple[uuid.UUID, ...]
    non_standard_role_ids: tuple[uuid.UUID, ...]


def _grant_facts(grant: Grant) -> GrantFacts:
    return GrantFacts(
        grant_id=grant.id,
        user_id=grant.user_id,
        role_id=grant.role_id,
        granted_at=grant.granted_at,
        expires_at=grant.expires_at,
        revoked_at=grant.revoked_at,
        revoke_reason=grant.revoke_reason,
        status=grant.status,
    )


# --- driving the surface -------------------------------------------------------


def _transfer(
    url: str,
    *,
    user_id: uuid.UUID,
    new_department_id: uuid.UUID,
    effective_date: date = EFFECTIVE_DATE,
    now: datetime = TRANSFERRED_AT,
) -> TransferFacts:
    """One `transfer_user` in one committed transaction.

    Every fact is copied out INSIDE the transaction that wrote it, which is
    also what proves the transaction survived a skipped role: a refusal raised
    after a statement had been issued would poison it, and both this read and
    the commit at the end of the block would fail.
    """

    async def scenario() -> TransferFacts:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                result = await transfer_user(
                    session,
                    SimulatedDirectory(session),
                    user_id=user_id,
                    new_department_id=new_department_id,
                    effective_date=effective_date,
                    transferred_by_user_id=TRANSFERRED_BY.id,
                    actor_ref=str(TRANSFERRED_BY.id),
                    session_id=None,
                    now=now,
                )
                return TransferFacts(
                    user_id=result.user.id,
                    department_id=result.user.department_id,
                    from_department_id=result.from_department_id,
                    revoked_role_ids=tuple(result.revoked_role_ids),
                    applied_role_ids=tuple(result.applied_role_ids),
                    skipped=tuple(
                        (record.role_id, str(record.reason_code)) for record in result.skipped
                    ),
                    retained_role_ids=tuple(result.retained_role_ids),
                    non_standard_role_ids=tuple(result.non_standard_role_ids),
                )
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _transfer_then_raise(url: str, *, user_id: uuid.UUID, new_department_id: uuid.UUID) -> None:
    """Transfer, then fail the enclosing transaction."""

    async def scenario() -> None:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                await transfer_user(
                    session,
                    SimulatedDirectory(session),
                    user_id=user_id,
                    new_department_id=new_department_id,
                    effective_date=EFFECTIVE_DATE,
                    transferred_by_user_id=TRANSFERRED_BY.id,
                    actor_ref=str(TRANSFERRED_BY.id),
                    session_id=None,
                    now=TRANSFERRED_AT,
                )
                raise _Boom("the caller failed after the transfer was written")
        finally:
            await engine.dispose()

    with contextlib.suppress(_Boom):
        asyncio.run(scenario())


# --- probes around the surface, never through it -------------------------------


def _make_department(url: str, role_ids: tuple[uuid.UUID, ...]) -> uuid.UUID:
    """One committed department, owning exactly `role_ids` as its template."""

    async def scenario() -> uuid.UUID:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                department = await Repository(session, Department).create(
                    name=f"Fake Transfer Dept {uuid.uuid4().hex[:12]}",
                    manager_user_id=PLANTED_MANAGER.id,
                )
                for role_id in role_ids:
                    await Repository(session, RoleTemplate).create(
                        department_id=department.id, role_id=role_id
                    )
                return department.id
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _plant_user(
    url: str,
    *,
    department_id: uuid.UUID | None,
    status: str = ACTIVE_USER_STATUS,
) -> uuid.UUID:
    """One committed, obviously fake user in the seeded tenant."""

    async def scenario() -> uuid.UUID:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                user = await Repository(session, User).create(
                    department_id=department_id,
                    email=f"fake.mover.{uuid.uuid4().hex}@fake.invalid",
                    display_name="Fake Mover",
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
    """One committed live `grants` row, with an expiry no engine would pick."""

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


def _grants_for_user(url: str, user_id: uuid.UUID) -> list[GrantFacts]:
    async def scenario() -> list[GrantFacts]:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                result = await session.execute(select(Grant).where(Grant.user_id == user_id))
                return [_grant_facts(grant) for grant in result.scalars()]
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _grants_by_role(url: str, user_id: uuid.UUID) -> dict[uuid.UUID, list[GrantFacts]]:
    by_role: dict[uuid.UUID, list[GrantFacts]] = {}
    for grant in _grants_for_user(url, user_id):
        by_role.setdefault(grant.role_id, []).append(grant)
    return by_role


def _department_of(url: str, user_id: uuid.UUID) -> uuid.UUID | None:
    async def scenario() -> uuid.UUID | None:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                user = await Repository(session, User).get(user_id)
                assert user is not None
                return user.department_id
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _live_role_ids(url: str, user_id: uuid.UUID) -> frozenset[uuid.UUID]:
    """What the DIRECTORY says this user holds, as of `TRANSFERRED_AT`."""

    async def scenario() -> frozenset[uuid.UUID]:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                return await SimulatedDirectory(session).list_account_role_ids(
                    user_id, TRANSFERRED_AT
                )
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _effective_expiry_days(url: str, *, target_user_id: uuid.UUID, role_id: uuid.UUID) -> int:
    """The engine's own number for this (target, role), asked around the module."""

    async def scenario() -> int:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                snapshot = await build_request_snapshot(
                    session,
                    SimulatedDirectory(session),
                    requester_user_id=TRANSFERRED_BY.id,
                    target_user_id=target_user_id,
                    role_id=role_id,
                    requested_expiry_days=None,
                    now=TRANSFERRED_AT,
                )
                return classify(snapshot).effective_expiry_days
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _role_default_expiry_days(url: str, role_id: uuid.UUID) -> int:
    async def scenario() -> int:
        engine = create_engine(url)
        try:
            async with engine.connect() as conn:
                value = (
                    await conn.execute(
                        text("SELECT default_expiry_days FROM roles WHERE id = :id"),
                        {"id": role_id},
                    )
                ).scalar_one()
                return int(value)
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


def _transferred_events(url: str) -> list[tuple[str, str, dict[str, Any]]]:
    """Every `user.transferred` in this tenant, in `seq` order."""

    async def scenario() -> list[tuple[str, str, dict[str, Any]]]:
        engine = create_engine(url)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(
                    text(
                        "SELECT subject_type, subject_id, payload FROM audit_events "
                        "WHERE tenant_id = :tenant AND event_type = :event_type ORDER BY seq"
                    ),
                    {"tenant": ROWS.tenant_id, "event_type": USER_TRANSFERRED_EVENT_TYPE},
                )
                return [(str(row[0]), str(row[1]), dict(row[2])) for row in result]
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


# --- the four-way diff --------------------------------


def test_the_diff_revokes_applies_retains_and_leaves_the_non_standard_grant(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches every wrong shape of the four sets at once, over one user who
    sits in all four corners.

    A transfer that re-applied the WHOLE new template writes a second grant
    row for the retained role — so the retained grant is asserted to be the
    same grant id with the same `expires_at` and the role is asserted to have
    exactly ONE row, which a re-apply fails even though every set in the
    return value would still look right. A transfer that revoked everything
    the old template named would take the retained role away; a transfer that
    revoked "everything held" would take the NON-STANDARD grant, which
    the design says stays live and untouched. The revoked row is checked as a
    row (`status`, `revoked_at`, `revoke_reason`), not merely as an id in the
    answer, so a transfer that reported a revocation it never wrote fails.

    The two grant events are counted as deltas — one `grant.revoked` and one
    `grant.executed` — so a transfer that wrote the rows directly instead of
    going through `revoke_role_access` and `execute_grant`
    leaves the tables right and the audit log empty, and fails here.
    """
    old_department = _make_department(access_db_url, (OLD_ONLY_ROLE, BOTH_ROLE))
    new_department = _make_department(access_db_url, (BOTH_ROLE, NEW_ONLY_ROLE))
    user_id = _plant_user(access_db_url, department_id=old_department)
    old_only_grant = _plant_grant(access_db_url, user_id=user_id, role_id=OLD_ONLY_ROLE)
    retained_grant = _plant_grant(access_db_url, user_id=user_id, role_id=BOTH_ROLE)
    non_standard_grant = _plant_grant(access_db_url, user_id=user_id, role_id=NON_STANDARD_ROLE)
    revoked_before = _event_count(access_db_url, GRANT_REVOKED_EVENT_TYPE)
    executed_before = _event_count(access_db_url, GRANT_EXECUTED_EVENT_TYPE)

    result = _transfer(access_db_url, user_id=user_id, new_department_id=new_department)

    assert result.user_id == user_id
    assert result.from_department_id == old_department
    assert result.department_id == new_department
    assert _department_of(access_db_url, user_id) == new_department
    assert result.skipped == ()
    assert set(result.revoked_role_ids) == {OLD_ONLY_ROLE}
    assert set(result.applied_role_ids) == {NEW_ONLY_ROLE}
    assert set(result.retained_role_ids) == {BOTH_ROLE}
    assert set(result.non_standard_role_ids) == {NON_STANDARD_ROLE}

    by_role = _grants_by_role(access_db_url, user_id)

    assert len(by_role[OLD_ONLY_ROLE]) == 1
    revoked = by_role[OLD_ONLY_ROLE][0]
    assert revoked.grant_id == old_only_grant
    assert revoked.status == REVOKED_GRANT_STATUS
    assert revoked.revoked_at == TRANSFERRED_AT
    assert revoked.revoke_reason == TRANSFER_REVOKE_REASON

    assert len(by_role[BOTH_ROLE]) == 1, "a retained role gets no second grant row"
    retained = by_role[BOTH_ROLE][0]
    assert retained.grant_id == retained_grant, "the SAME grant, never a fresh one"
    assert retained.expires_at == PLANTED_EXPIRY, "untouched, not re-stamped"
    assert retained.granted_at == PLANTED_GRANTED_AT
    assert retained.status == ACTIVE_GRANT_STATUS
    assert retained.revoked_at is None

    assert len(by_role[NON_STANDARD_ROLE]) == 1
    non_standard = by_role[NON_STANDARD_ROLE][0]
    assert non_standard.grant_id == non_standard_grant
    assert non_standard.status == ACTIVE_GRANT_STATUS
    assert non_standard.revoked_at is None
    assert non_standard.expires_at == PLANTED_EXPIRY

    assert len(by_role[NEW_ONLY_ROLE]) == 1
    applied = by_role[NEW_ONLY_ROLE][0]
    assert applied.granted_at == TRANSFERRED_AT
    assert applied.status == ACTIVE_GRANT_STATUS

    assert _live_role_ids(access_db_url, user_id) == frozenset(
        {BOTH_ROLE, NON_STANDARD_ROLE, NEW_ONLY_ROLE}
    )
    assert _event_count(access_db_url, GRANT_REVOKED_EVENT_TYPE) == revoked_before + 1
    assert _event_count(access_db_url, GRANT_EXECUTED_EVENT_TYPE) == executed_before + 1


def test_an_applied_role_gets_the_engines_expiry_not_the_roles_default(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches a transfer that writes grants directly (or through
    `apply_grant`) with the role's own `default_expiry_days`.

    `hris.viewer` is a read role whose column default is 365 days and whose
    engine number is 30, because `hris` is a critical system — so an
    implementation that skipped `execute_grant` would give a mover a year of
    access to the HR system that the spec caps at a month, and every count in
    the case above still passes. Both numbers are asserted exactly (
    asserted as literals) and the engine's is cross-checked live as a premise, so a policy
    change fails the premise rather than rewriting the expectation.
    """
    old_department = _make_department(access_db_url, ())
    new_department = _make_department(access_db_url, (CAPPED_ROLE,))
    user_id = _plant_user(access_db_url, department_id=old_department)
    assert (
        _role_default_expiry_days(access_db_url, CAPPED_ROLE) == CAPPED_ROLE_DEFAULT_EXPIRY_DAYS
    ), "premise: the role's own default is still the larger number"

    result = _transfer(access_db_url, user_id=user_id, new_department_id=new_department)

    assert set(result.applied_role_ids) == {CAPPED_ROLE}
    assert (
        _effective_expiry_days(access_db_url, target_user_id=user_id, role_id=CAPPED_ROLE)
        == CAPPED_EXPIRY_DAYS
    ), "premise: the engine still caps this role at 30 days"
    grants = _grants_by_role(access_db_url, user_id)[CAPPED_ROLE]
    assert len(grants) == 1
    assert grants[0].expires_at == TRANSFERRED_AT + timedelta(days=CAPPED_EXPIRY_DAYS)
    assert grants[0].expires_at != TRANSFERRED_AT + timedelta(days=CAPPED_ROLE_DEFAULT_EXPIRY_DAYS)


def test_the_applications_are_classified_against_the_new_department(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches the department write landing AFTER the applications.

       `build_request_snapshot` reads `role_in_department_template` off the
       target's CURRENT department, so a pass that applied first sees every
       arriving role as out-of-template. `grafana.viewer` is low-sensitivity and
       read, so in the new template it is `low` (cap 365) and outside it
       `elevated` (cap 180): the mover would silently get half the access life
       their new department's own template entitles them to, and every set in the
       return value would still be correct. Both numbers are asserted exactly
    and both are cross-checked live — the out-of-template
       one against a control user still sitting in the old department, so the
       premise is a fact about the engine and not about this implementation.

       This is the case that fails if the expiry choice is reversed, and only this one:
       the diff case above uses the same role but would pass at either expiry.
    """
    old_department = _make_department(access_db_url, ())
    new_department = _make_department(access_db_url, (NEW_ONLY_ROLE,))
    user_id = _plant_user(access_db_url, department_id=old_department)
    control_id = _plant_user(access_db_url, department_id=old_department)
    assert (
        _effective_expiry_days(access_db_url, target_user_id=control_id, role_id=NEW_ONLY_ROLE)
        == OUT_OF_TEMPLATE_EXPIRY_DAYS
    ), "premise: outside a template this role is elevated, capped at 180"

    result = _transfer(access_db_url, user_id=user_id, new_department_id=new_department)

    assert set(result.applied_role_ids) == {NEW_ONLY_ROLE}
    assert (
        _effective_expiry_days(access_db_url, target_user_id=user_id, role_id=NEW_ONLY_ROLE)
        == IN_TEMPLATE_EXPIRY_DAYS
    ), "premise: inside the new template the same role is low, capped at 365"
    grants = _grants_by_role(access_db_url, user_id)[NEW_ONLY_ROLE]
    assert len(grants) == 1
    assert grants[0].expires_at == TRANSFERRED_AT + timedelta(days=IN_TEMPLATE_EXPIRY_DAYS)
    assert IN_TEMPLATE_EXPIRY_DAYS != OUT_OF_TEMPLATE_EXPIRY_DAYS


def test_a_new_template_role_in_sod_conflict_is_skipped_and_the_rest_stands(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches a transfer that fails whole on a refused role, one
    that swallows the refusal, and one that writes the grant before raising.

    The mover holds `payments.initiator` outside both templates, and the new
    template names `payments.approver`, so the seeded standard separation-of-duties rule refuses
    the application at execution. the contract says that costs the ROLE and not the
    move: the department change, the revocation of the old-template role and
    the other applied role all still stand, and the refused role is asserted
    absent from the grants table and from the directory's answer — not merely
    absent from `applied_role_ids`, which a write-then-raise would satisfy.

    The typed code is asserted as `SOD_CONFLICT` and not `POLICY_BLOCKED`:
    `reclassify` blocks on a conflict too, so the two codes are one line apart
    and an auditor must be able to tell "these roles cannot be held together"
    from "you may not have this".
    """
    old_department = _make_department(access_db_url, (OLD_ONLY_ROLE,))
    new_department = _make_department(access_db_url, (SOD_TEMPLATE_ROLE, NEW_ONLY_ROLE))
    user_id = _plant_user(access_db_url, department_id=old_department)
    _plant_grant(access_db_url, user_id=user_id, role_id=OLD_ONLY_ROLE)
    _plant_grant(access_db_url, user_id=user_id, role_id=SOD_HELD_ROLE)

    result = _transfer(access_db_url, user_id=user_id, new_department_id=new_department)

    assert len(result.skipped) == 1, result.skipped
    skipped_role_id, reason_code = result.skipped[0]
    assert skipped_role_id == SOD_TEMPLATE_ROLE
    assert reason_code == ErrorCode.SOD_CONFLICT.value
    assert set(result.applied_role_ids) == {NEW_ONLY_ROLE}
    assert set(result.revoked_role_ids) == {OLD_ONLY_ROLE}
    assert set(result.non_standard_role_ids) == {SOD_HELD_ROLE}

    assert _department_of(access_db_url, user_id) == new_department
    by_role = _grants_by_role(access_db_url, user_id)
    assert SOD_TEMPLATE_ROLE not in by_role, "a refused role writes no grants row at all"
    assert _live_role_ids(access_db_url, user_id) == frozenset({SOD_HELD_ROLE, NEW_ONLY_ROLE})
    assert by_role[OLD_ONLY_ROLE][0].status == REVOKED_GRANT_STATUS


# --- the two refusals, before any write ---------------------


def test_a_transfer_into_the_same_department_is_refused_before_any_write(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches the "succeed with an empty diff" alternative.

    Nothing about the tables changes either way — the diff IS empty — so the
    only observable difference is the audit log, which would gain a
    `user.transferred` recording a move that never happened. The
    `user.transferred` count is asserted unchanged, and so are the grant
    events and the department, so a refusal raised after the event was
    appended fails too.
    """
    department = _make_department(access_db_url, (BOTH_ROLE,))
    user_id = _plant_user(access_db_url, department_id=department)
    _plant_grant(access_db_url, user_id=user_id, role_id=BOTH_ROLE)
    transferred_before = len(_transferred_events(access_db_url))
    revoked_before = _event_count(access_db_url, GRANT_REVOKED_EVENT_TYPE)
    executed_before = _event_count(access_db_url, GRANT_EXECUTED_EVENT_TYPE)

    with pytest.raises(SameDepartmentError):
        _transfer(access_db_url, user_id=user_id, new_department_id=department)

    assert _department_of(access_db_url, user_id) == department
    assert len(_transferred_events(access_db_url)) == transferred_before
    assert _event_count(access_db_url, GRANT_REVOKED_EVENT_TYPE) == revoked_before
    assert _event_count(access_db_url, GRANT_EXECUTED_EVENT_TYPE) == executed_before
    assert _live_role_ids(access_db_url, user_id) == frozenset({BOTH_ROLE})


def test_a_leaver_target_is_refused_before_any_revocation(
    access_db_url: str,  # noqa: F811
) -> None:
    """The only case that can tell it from doing nothing.

    The engine blocks every new grant for a leaver on its own, so
    a transfer that let the leaver through would report every arriving role in
    `skipped` — but revocations are not policy decisions and would still run,
    stripping the leaver's remaining access under an event that says they
    moved. The old-template grant is therefore asserted STILL LIVE, which is
    what fails when the refusal is deleted; the `skipped`-only alternative
    leaves this case's grants revoked.
    """
    old_department = _make_department(access_db_url, (OLD_ONLY_ROLE,))
    new_department = _make_department(access_db_url, (NEW_ONLY_ROLE,))
    user_id = _plant_user(access_db_url, department_id=old_department, status=LEAVER_USER_STATUS)
    grant_id = _plant_grant(access_db_url, user_id=user_id, role_id=OLD_ONLY_ROLE)
    transferred_before = len(_transferred_events(access_db_url))
    revoked_before = _event_count(access_db_url, GRANT_REVOKED_EVENT_TYPE)

    with pytest.raises(LeaverTransferError):
        _transfer(access_db_url, user_id=user_id, new_department_id=new_department)

    assert _department_of(access_db_url, user_id) == old_department
    grants = _grants_by_role(access_db_url, user_id)[OLD_ONLY_ROLE]
    assert len(grants) == 1
    assert grants[0].grant_id == grant_id
    assert grants[0].status == ACTIVE_GRANT_STATUS, "a refused transfer strips nothing"
    assert grants[0].revoked_at is None
    assert len(_transferred_events(access_db_url)) == transferred_before
    assert _event_count(access_db_url, GRANT_REVOKED_EVENT_TYPE) == revoked_before


def test_the_transfer_constants_and_error_codes_are_exactly_the_spec_values(
    access_db_url: str,  # noqa: F811
) -> None:
    """Pins every constant this module introduces.

    Either error inheriting `ToolError` without setting `code` answers
    `INTERNAL` — a caller-caused refusal reported as a server fault — and
    every case above passes either way, because they catch by type. The event
    type is read as a constant by every query in this file, so a rename would
    leave the file green and the audit log unqueryable. `TRANSFER_REVOKE_REASON`
    is what an auditor greps `grants.revoke_reason` for to find the moves, so
    it is pinned as the exact string rather than "some non-empty reason".

    The exports are part of the surface: `mcp_server.tools.lifecycle` imports
    from the module that defines each name.
    """
    assert USER_TRANSFERRED_EVENT_TYPE == "user.transferred"
    assert TRANSFER_REVOKE_REASON == "transferred out of the department that granted this role"
    assert SameDepartmentError.code is ErrorCode.POLICY_BLOCKED
    assert SameDepartmentError.code.value == "POLICY_BLOCKED"
    assert LeaverTransferError.code is ErrorCode.POLICY_BLOCKED
    assert LeaverTransferError.code.value == "POLICY_BLOCKED"
    assert lifecycle_module.transfer_user is transfer_user
    assert lifecycle_module.USER_TRANSFERRED_EVENT_TYPE == USER_TRANSFERRED_EVENT_TYPE
    assert lifecycle_module.TRANSFER_REVOKE_REASON == TRANSFER_REVOKE_REASON


# --- the additive case -------------------------------------------


def test_a_user_with_no_department_transfers_additively(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches a `department_id is None` treated as an error, and
    a `None` old department read as "revoke everything".

    `list_membership_role_ids` answers the empty frozenset for a user with no
    department, so the old template is empty and nothing may be revoked — but
    an implementation that took "the old template" from anywhere else, or that
    refused a null department outright, is exactly what this shape hides.
    `from_department_id` is asserted `None` so the event below can record the
    move honestly, and the held non-template grant is asserted still live.
    """
    new_department = _make_department(access_db_url, (NEW_ONLY_ROLE,))
    user_id = _plant_user(access_db_url, department_id=None)
    held = _plant_grant(access_db_url, user_id=user_id, role_id=NON_STANDARD_ROLE)
    revoked_before = _event_count(access_db_url, GRANT_REVOKED_EVENT_TYPE)

    result = _transfer(access_db_url, user_id=user_id, new_department_id=new_department)

    assert result.from_department_id is None
    assert result.department_id == new_department
    assert result.revoked_role_ids == ()
    assert set(result.applied_role_ids) == {NEW_ONLY_ROLE}
    assert result.retained_role_ids == ()
    assert set(result.non_standard_role_ids) == {NON_STANDARD_ROLE}
    assert _department_of(access_db_url, user_id) == new_department
    assert _grants_by_role(access_db_url, user_id)[NON_STANDARD_ROLE][0].grant_id == held
    assert _live_role_ids(access_db_url, user_id) == frozenset({NON_STANDARD_ROLE, NEW_ONLY_ROLE})
    assert _event_count(access_db_url, GRANT_REVOKED_EVENT_TYPE) == revoked_before


# --- the transaction --------------------------------------------------


def test_the_transfer_and_its_events_roll_back_together(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches a transfer that commits (or opens its own session for) the
    department write, a revocation, a grant or an event.

    The caller fails AFTER `transfer_user` returned, so anything committed in
    its own transaction survives a rollback that must take it — and the
    instant the design leaves open, where the mover holds neither the old role
    nor the new one, becomes visible state rather than a moment inside one
    transaction. All four effects are re-read: the department, the
    revoked row, the applied row and both grant events.
    """
    old_department = _make_department(access_db_url, (OLD_ONLY_ROLE,))
    new_department = _make_department(access_db_url, (NEW_ONLY_ROLE,))
    user_id = _plant_user(access_db_url, department_id=old_department)
    grant_id = _plant_grant(access_db_url, user_id=user_id, role_id=OLD_ONLY_ROLE)
    revoked_before = _event_count(access_db_url, GRANT_REVOKED_EVENT_TYPE)
    executed_before = _event_count(access_db_url, GRANT_EXECUTED_EVENT_TYPE)
    transferred_before = len(_transferred_events(access_db_url))

    _transfer_then_raise(access_db_url, user_id=user_id, new_department_id=new_department)

    assert _department_of(access_db_url, user_id) == old_department
    grants = _grants_by_role(access_db_url, user_id)
    assert list(grants) == [OLD_ONLY_ROLE], "the applied grant must be gone with the rest"
    assert grants[OLD_ONLY_ROLE][0].grant_id == grant_id
    assert grants[OLD_ONLY_ROLE][0].status == ACTIVE_GRANT_STATUS
    assert grants[OLD_ONLY_ROLE][0].revoked_at is None
    assert _event_count(access_db_url, GRANT_REVOKED_EVENT_TYPE) == revoked_before
    assert _event_count(access_db_url, GRANT_EXECUTED_EVENT_TYPE) == executed_before
    assert len(_transferred_events(access_db_url)) == transferred_before


# --- the audit record --------------------------


def test_user_transferred_names_the_user_and_carries_exactly_nine_keys(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches an event whose subject is the
    department, the caller or the tool rather than the USER, and one whose
    payload has drifted from the contract's nine keys.

    An auditor asking "why does this person hold this" queries `subject_id`;
    an event subject naming anything else answers that for nobody, while a
    count-only assertion still passes. The keys are compared as an exact set,
    so a dropped `non_standard_role_ids` (the ONLY record of the
    grants nothing else surfaces until a review runs), a dropped
    `effective_date` (the only record of it — there is no column) or
    a dropped `skipped_role_ids` fails here. Every list is compared by value
    against what the same call returned, so an event that reports the template
    rather than what actually happened is caught too, and the department
    deliberately names a conflicting role so `skipped_role_ids` is non-empty
    and cannot pass as `[]`.
    """
    old_department = _make_department(access_db_url, (OLD_ONLY_ROLE, BOTH_ROLE))
    new_department = _make_department(access_db_url, (BOTH_ROLE, NEW_ONLY_ROLE, SOD_TEMPLATE_ROLE))
    user_id = _plant_user(access_db_url, department_id=old_department)
    _plant_grant(access_db_url, user_id=user_id, role_id=OLD_ONLY_ROLE)
    _plant_grant(access_db_url, user_id=user_id, role_id=BOTH_ROLE)
    _plant_grant(access_db_url, user_id=user_id, role_id=SOD_HELD_ROLE)
    events_before = len(_transferred_events(access_db_url))

    result = _transfer(access_db_url, user_id=user_id, new_department_id=new_department)

    events = _transferred_events(access_db_url)
    assert len(events) == events_before + 1, "exactly one user.transferred per transfer"
    subject_type, subject_id, payload = events[-1]
    assert subject_type == "user"
    assert subject_id == str(user_id)
    assert set(payload) == EXPECTED_PAYLOAD_KEYS
    assert payload["user_id"] == str(user_id)
    assert payload["from_department_id"] == str(old_department)
    assert payload["to_department_id"] == str(new_department)
    assert payload["effective_date"] == EFFECTIVE_DATE.isoformat()
    assert sorted(payload["revoked_role_ids"]) == sorted(
        str(role_id) for role_id in result.revoked_role_ids
    )
    assert sorted(payload["applied_role_ids"]) == sorted(
        str(role_id) for role_id in result.applied_role_ids
    )
    assert sorted(payload["retained_role_ids"]) == sorted(
        str(role_id) for role_id in result.retained_role_ids
    )
    assert sorted(payload["non_standard_role_ids"]) == sorted(
        str(role_id) for role_id in result.non_standard_role_ids
    )
    assert sorted(payload["skipped_role_ids"]) == sorted(
        str(role_id) for role_id, _ in result.skipped
    )
    assert payload["skipped_role_ids"], "premise: this template deliberately conflicts"
