"""An earlier change, sections 1 and 6: `onboard_user`, called directly over the seed.

Surface under test (written before it exists):

``helpdesk.access.lifecycle``
    ``USER_ONBOARDED_EVENT_TYPE = "user.onboarded"`` (the design).
    ``UNUSABLE_PASSWORD_HASH`` — the sentinel written into the NOT NULL
    `users.password_hash` of a joiner who has no credential yet (decision C).
    ``JOINER_PRINCIPALS = ("employee",)`` — a fixed constant, never an input
    (decision D).
    ``DuplicateEmailError(ToolError)`` — `code = POLICY_BLOCKED` (decision G).
    ``async def onboard_user(session, directory, *, email, display_name,
    department_id, start_date, manager_user_id, created_by_user_id,
    actor_ref=None, session_id=None, now) -> OnboardResult`` — creates one
    `users` row in the BOUND tenant, applies every role of that department's
    `role_templates` set through `helpdesk.access.execute_grant` (decision A),
    and appends one `user.onboarded` naming the NEW USER (decision F), all in
    the caller's transaction. ``OnboardResult`` carries ``user``, ``applied``
    (the grants written) and ``skipped`` (records of ``role_id`` and
    ``reason_code`` for the template roles the engine refused, decision B).

``helpdesk.access.directory``
    ``DirectoryProvider.create_user(...)`` and
    ``DirectoryProvider.list_department_role_ids(department_id)``.

a project rule: the seeded database is `test_access_snapshot.py`'s
session-scoped `access_db_url` fixture, imported rather than rebuilt (argon2
seeding is the slow part of this suite). Per-test isolation inside it is a
namespace: each case creates its OWN department (a fresh uuid name) with its
own template rows, and every joiner's email carries a fresh uuid4, so no case
can see another's users, grants or events. Seeded identities are recomputed
from `scripts.seed.generate_tenant` — deterministic uuid5 ids, never
hardcoded UUIDs.

No pytest-asyncio: each case is a sync function driving one `asyncio.run`.
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text

from helpdesk.shared.access.execute import GRANT_EXECUTED_EVENT_TYPE
from helpdesk.shared.auth.passwords import verify_password
from helpdesk.shared.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.shared.core.errors import ErrorCode
from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.grants import ACTIVE_GRANT_STATUS
from helpdesk.shared.models import Department, Grant, RoleTemplate, User
from helpdesk.shared.policy.engine import classify
from helpdesk.shared.services.access import lifecycle as lifecycle_module
from helpdesk.shared.services.access.directory import SimulatedDirectory
from helpdesk.shared.services.access.lifecycle import (
    JOINER_PRINCIPALS,
    UNUSABLE_PASSWORD_HASH,
    USER_ONBOARDED_EVENT_TYPE,
    DuplicateEmailError,
    onboard_user,
)
from helpdesk.shared.services.access.snapshot import build_request_snapshot

# Imported, not redefined: one seeded database for the whole session, shared
# with `test_access_snapshot.py`, `test_access_execute.py`,
# `test_access_approvals.py` and `test_access_revoke.py`.
from tests.shared.services.access.test_access_snapshot import (
    NOW,
    ROWS,
    access_db_url,  # noqa: F401
)

# The fixed clock every onboarding runs at, so an expiry assertion cannot
# depend on when the suite runs. `NOW` comes from the snapshot module, so
# every file in this suite answers for the same instant.
ONBOARDED_AT = NOW
# The joiner starts on the day they are created: tenure 0, which is what makes
# the engine's NEW_JOINER bump (and therefore the elevated cap) part of the
# expiry numbers pinned below.
START_DATE = date(2026, 9, 1)

# Whoever runs the onboarding. Seeded HR users are read only here — no case
# writes to them — so this cannot collide with another module's namespace.
CREATED_BY = ROWS.users_by_department["HR"][3]
NEW_MANAGER = ROWS.users_by_department["HR"][4]

# The template the happy-path department gets, and the exact number of days
# the ENGINE gives each of those roles for a day-one joiner.
# Two of the three differ from the role's own `default_expiry_days`, which is
# what makes "the engine's number, not the role's" observable:
#   hris.viewer     read,  critical system -> critical class, cap 30  (default 365)
#   wiki.viewer     read,  low system      -> low, bumped NEW_JOINER  -> 180 (default 365)
#   plm.engineer    write, high system     -> elevated, cap 180       (default 180)
HRIS_VIEWER = ROWS.role_ids[("hris", "viewer")]
WIKI_VIEWER = ROWS.role_ids[("wiki", "viewer")]
PLM_ENGINEER = ROWS.role_ids[("plm", "engineer")]
TEMPLATE_ROLES = (HRIS_VIEWER, WIKI_VIEWER, PLM_ENGINEER)
EXPECTED_EXPIRY_DAYS = {HRIS_VIEWER: 30, WIKI_VIEWER: 180, PLM_ENGINEER: 180}
ROLE_DEFAULT_EXPIRY_DAYS = {HRIS_VIEWER: 365, WIKI_VIEWER: 365, PLM_ENGINEER: 180}

# The seeded SoD pair (rule SoD-STD §2.1). A department template naming BOTH
# is a data defect, and decision B says it costs the joiner one role, not the
# whole onboarding.
SOD_ROLE_A = ROWS.role_ids[("payments", "initiator")]
SOD_ROLE_B = ROWS.role_ids[("payments", "approver")]
SOD_NEIGHBOUR = ROWS.role_ids[("grafana", "viewer")]

EXPECTED_PAYLOAD_KEYS = {
    "user_id",
    "email",
    "department_id",
    "manager_user_id",
    "applied_role_ids",
    "skipped_role_ids",
}

ACTIVE_USER_STATUS = "active"


@dataclass(frozen=True)
class GrantFacts:
    """What one written `grants` row says, copied out inside its own session.

    A detached ORM object would refresh against a closed session, so every
    value the assertions need is read inside the transaction that owns it.
    """

    grant_id: uuid.UUID
    user_id: uuid.UUID
    role_id: uuid.UUID
    granted_at: datetime
    expires_at: datetime | None
    status: str


@dataclass(frozen=True)
class UserFacts:
    user_id: uuid.UUID
    email: str
    display_name: str
    department_id: uuid.UUID | None
    principals: tuple[str, ...]
    status: str
    start_date: date
    end_date: date | None
    password_hash: str


@dataclass(frozen=True)
class OnboardFacts:
    """One `OnboardResult`, flattened before its session closes."""

    user: UserFacts
    applied: tuple[GrantFacts, ...]
    skipped: tuple[tuple[uuid.UUID, str], ...]


def _grant_facts(grant: Grant) -> GrantFacts:
    return GrantFacts(
        grant_id=grant.id,
        user_id=grant.user_id,
        role_id=grant.role_id,
        granted_at=grant.granted_at,
        expires_at=grant.expires_at,
        status=grant.status,
    )


def _user_facts(user: User) -> UserFacts:
    return UserFacts(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        department_id=user.department_id,
        principals=tuple(user.principals),
        status=user.status,
        start_date=user.start_date,
        end_date=user.end_date,
        password_hash=user.password_hash,
    )


def _email() -> str:
    """A fresh joiner address: half the per-test namespace, obviously fake."""
    return f"fake.joiner.{uuid.uuid4().hex}@fake.invalid"


# --- driving the surface -------------------------------------------------------


def _onboard(
    url: str,
    *,
    email: str,
    department_id: uuid.UUID,
    display_name: str = "Fake Joiner",
    manager_user_id: uuid.UUID = NEW_MANAGER.id,
    start_date: date = START_DATE,
    now: datetime = ONBOARDED_AT,
) -> OnboardFacts:
    """One `onboard_user` in one committed transaction.

    Every fact is copied out INSIDE the transaction that wrote it, which is
    also what proves the transaction survived a skipped role: a refusal raised
    after a statement had been issued would poison it, and both this read and
    the commit at the end of the block would fail (plan §5).
    """

    async def scenario() -> OnboardFacts:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                result = await onboard_user(
                    session,
                    SimulatedDirectory(session),
                    email=email,
                    display_name=display_name,
                    department_id=department_id,
                    start_date=start_date,
                    manager_user_id=manager_user_id,
                    created_by_user_id=CREATED_BY.id,
                    actor_ref=str(CREATED_BY.id),
                    session_id=None,
                    now=now,
                )
                return OnboardFacts(
                    user=_user_facts(result.user),
                    applied=tuple(_grant_facts(grant) for grant in result.applied),
                    skipped=tuple(
                        (record.role_id, str(record.reason_code)) for record in result.skipped
                    ),
                )
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


# --- probes around the surface, never through it -------------------------------


def _make_department(url: str, role_ids: tuple[uuid.UUID, ...]) -> uuid.UUID:
    """One committed department, owning exactly `role_ids` as its template."""

    async def scenario() -> uuid.UUID:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                department = await Repository(session, Department).create(
                    name=f"Fake Onboarding Dept {uuid.uuid4().hex[:12]}",
                    manager_user_id=NEW_MANAGER.id,
                )
                for role_id in role_ids:
                    await Repository(session, RoleTemplate).create(
                        department_id=department.id, role_id=role_id
                    )
                return department.id
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _users_with_email(url: str, email: str) -> list[UserFacts]:
    async def scenario() -> list[UserFacts]:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                result = await session.execute(select(User).where(User.email == email))
                return [_user_facts(user) for user in result.scalars()]
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


def _account_role_ids(url: str, user_id: uuid.UUID) -> frozenset[uuid.UUID]:
    """What the DIRECTORY says this user holds, as of `ONBOARDED_AT`."""

    async def scenario() -> frozenset[uuid.UUID]:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                return await SimulatedDirectory(session).list_account_role_ids(
                    user_id, ONBOARDED_AT
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
                    requester_user_id=CREATED_BY.id,
                    target_user_id=target_user_id,
                    role_id=role_id,
                    requested_expiry_days=None,
                    now=ONBOARDED_AT,
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


def _onboarded_events(url: str) -> list[tuple[str, str, dict[str, Any]]]:
    """Every `user.onboarded` in this tenant, in `seq` order."""

    async def scenario() -> list[tuple[str, str, dict[str, Any]]]:
        engine = create_engine(url)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(
                    text(
                        "SELECT subject_type, subject_id, payload FROM audit_events "
                        "WHERE tenant_id = :tenant AND event_type = :event_type ORDER BY seq"
                    ),
                    {"tenant": ROWS.tenant_id, "event_type": USER_ONBOARDED_EVENT_TYPE},
                )
                return [(str(row[0]), str(row[1]), dict(row[2])) for row in result]
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _user_count(url: str) -> int:
    async def scenario() -> int:
        engine = create_engine(url)
        try:
            async with engine.connect() as conn:
                total = (
                    await conn.execute(
                        select(func.count())
                        .select_from(User)
                        .where(User.tenant_id == ROWS.tenant_id)
                    )
                ).scalar_one()
                return int(total)
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


# --- the template, applied through the engine (criteria 1 and 2) ----------------


def test_the_whole_template_is_applied_with_the_engines_expiry_not_the_roles(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches decision A regressing to `apply_grant` with the role's own
    `default_expiry_days`.

    Two of the three template roles have an engine number BELOW their column
    default — `hris.viewer` is 30 (critical system) against a default of 365,
    `wiki.viewer` is 180 (the day-one NEW_JOINER bump) against 365 — so an
    onboarding that writes the role default gives a joiner a year of access to
    the HR system that the policy engine caps at a month, and every count in
    this test still passes. Each number is asserted exactly
    and cross-checked against the live engine as a premise, so a policy change
    fails the premise rather than silently rewriting the expectation.

    Also catches a joiner row that carries the caller's principals instead of
    the fixed `("employee",)` (decision D — an HR caller minting an `it_admin`
    is a privilege-escalation path), a real credential instead of the
    unusable sentinel (decision C), and a template applied by fewer grants or
    fewer `grant.executed` events than roles.
    """
    department_id = _make_department(access_db_url, TEMPLATE_ROLES)
    email = _email()
    for role_id in TEMPLATE_ROLES:
        assert (
            _role_default_expiry_days(access_db_url, role_id) == ROLE_DEFAULT_EXPIRY_DAYS[role_id]
        )
    grant_events_before = _event_count(access_db_url, GRANT_EXECUTED_EVENT_TYPE)

    result = _onboard(
        access_db_url, email=email, department_id=department_id, display_name="Fake Joiner One"
    )

    assert result.user.email == email
    assert result.user.display_name == "Fake Joiner One"
    assert result.user.department_id == department_id
    assert result.user.start_date == START_DATE
    assert result.user.end_date is None
    assert result.user.status == ACTIVE_USER_STATUS
    assert result.user.principals == JOINER_PRINCIPALS
    assert result.user.password_hash == UNUSABLE_PASSWORD_HASH
    assert _users_with_email(access_db_url, email) == [result.user], "exactly one row, persisted"

    assert result.skipped == ()
    assert {grant.role_id for grant in result.applied} == set(TEMPLATE_ROLES)
    for grant in result.applied:
        expected_days = EXPECTED_EXPIRY_DAYS[grant.role_id]
        assert (
            _effective_expiry_days(
                access_db_url, target_user_id=result.user.user_id, role_id=grant.role_id
            )
            == expected_days
        ), "premise: the engine still gives this role that many days"
        assert grant.granted_at == ONBOARDED_AT
        assert grant.expires_at == ONBOARDED_AT + timedelta(days=expected_days)
        assert grant.status == ACTIVE_GRANT_STATUS
        assert grant.user_id == result.user.user_id

    assert {grant.role_id for grant in _grants_for_user(access_db_url, result.user.user_id)} == set(
        TEMPLATE_ROLES
    )
    assert _account_role_ids(access_db_url, result.user.user_id) == frozenset(TEMPLATE_ROLES)
    assert _event_count(access_db_url, GRANT_EXECUTED_EVENT_TYPE) == grant_events_before + len(
        TEMPLATE_ROLES
    ), "one grant.executed per applied role, never one per onboarding"


def test_an_empty_template_creates_the_user_and_applies_nothing(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches an onboarding that treats an empty template as an error, or that
    falls back to some default role set when the department names none.

    A department with no `role_templates` row is a valid directory state, and
    §1 says `applied` is `[]` — not a refusal, and not "every role in the
    tenant". The grant table is re-read for the new user, so an implementation
    that returned `[]` while writing a grant anyway fails here, and the
    `grant.executed` count is asserted unchanged so no event can claim access
    that nobody has.
    """
    department_id = _make_department(access_db_url, ())
    email = _email()
    grant_events_before = _event_count(access_db_url, GRANT_EXECUTED_EVENT_TYPE)

    result = _onboard(access_db_url, email=email, department_id=department_id)

    assert result.applied == ()
    assert result.skipped == ()
    assert result.user.department_id == department_id
    assert _users_with_email(access_db_url, email) == [result.user]
    assert _grants_for_user(access_db_url, result.user.user_id) == []
    assert _event_count(access_db_url, GRANT_EXECUTED_EVENT_TYPE) == grant_events_before


def test_a_template_role_in_sod_conflict_is_skipped_and_the_rest_stand(
    access_db_url: str,  # noqa: F811
) -> None:
    """Decision B, and the whole reason decision A routes through the engine.

    The department's template names both halves of seeded rule SoD-STD §2.1,
    so whichever of the pair is applied second is refused by `execute_grant`'s
    §9.5 re-check. Catches three wrong implementations at once: one that fails
    the entire onboarding on a refused role (the joiner would be left with
    nothing because of one bad template row), one that swallows the refusal
    silently (`skipped` empty, so nobody ever learns the template is broken),
    and one that writes the grant before raising — the conflicting role is
    asserted absent from BOTH the grants table and the directory's answer, not
    merely absent from `applied`.

    The neighbouring role and the surviving half of the pair are asserted LIVE
    after the refusal, which is the "every other applied grant still stands"
    half of §1: a refusal raised after a statement had been issued would
    poison the transaction and take them with it (plan §5).

    Order-agnostic on purpose: which half of the pair loses depends on the
    order `list_department_role_ids` answers in, and pinning that order here
    would pin an implementation detail instead of the behaviour.
    """
    department_id = _make_department(access_db_url, (SOD_ROLE_A, SOD_ROLE_B, SOD_NEIGHBOUR))
    email = _email()

    result = _onboard(access_db_url, email=email, department_id=department_id)

    assert len(result.skipped) == 1, result.skipped
    skipped_role_id, reason_code = result.skipped[0]
    assert skipped_role_id in {SOD_ROLE_A, SOD_ROLE_B}
    assert reason_code == ErrorCode.SOD_CONFLICT.value
    survivor = SOD_ROLE_B if skipped_role_id == SOD_ROLE_A else SOD_ROLE_A

    assert {grant.role_id for grant in result.applied} == {survivor, SOD_NEIGHBOUR}
    assert _users_with_email(access_db_url, email) == [result.user], "the joiner still exists"
    held = _account_role_ids(access_db_url, result.user.user_id)
    assert held == frozenset({survivor, SOD_NEIGHBOUR})
    assert skipped_role_id not in {
        grant.role_id for grant in _grants_for_user(access_db_url, result.user.user_id)
    }, "a refused role writes no grants row at all, not even a dead one"


# --- the duplicate email (decision G) ------------------------------------------


def test_a_duplicate_email_is_refused_before_any_write(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches decision G's alternative: letting the unique index raise.

    An `IntegrityError` reaches the caller as `INTERNAL` — a caller-caused
    condition reported as a server fault — and by then the pre-check's other
    promise is already broken: the refusal must leave NO user row, NO grant
    and NO `user.onboarded`. The address used is a seeded user's own, so the
    row genuinely exists in this tenant, and the tenant's user count is
    compared before and after so a second row under the same address (or under
    a mangled variant of it) cannot hide.
    """
    department_id = _make_department(access_db_url, TEMPLATE_ROLES)
    taken = ROWS.users_by_department["Sales"][5].email
    assert len(_users_with_email(access_db_url, taken)) == 1, "premise: the address is in use"
    users_before = _user_count(access_db_url)
    events_before = len(_onboarded_events(access_db_url))
    grant_events_before = _event_count(access_db_url, GRANT_EXECUTED_EVENT_TYPE)

    with pytest.raises(DuplicateEmailError):
        _onboard(access_db_url, email=taken, department_id=department_id)

    assert _user_count(access_db_url) == users_before
    assert len(_users_with_email(access_db_url, taken)) == 1
    assert len(_onboarded_events(access_db_url)) == events_before
    assert _event_count(access_db_url, GRANT_EXECUTED_EVENT_TYPE) == grant_events_before


def test_the_onboard_constants_and_error_code_are_exactly_the_spec_values() -> None:
    """Pins every constant this module introduces.

    `DuplicateEmailError` inheriting `ToolError` without setting `code` answers
    `INTERNAL`, and a `NOT_FOUND` here would make a tenant's address list
    probeable — every other case in this file passes either way, because they
    catch the exception by type. The event type is read as a constant by every
    query above, so a rename would leave this file green and the audit log
    unqueryable. `JOINER_PRINCIPALS` is asserted as the exact tuple: a
    `("employee", "manager")` default would hand every joiner an approval
    seat.

    The export is part of the surface: `mcp_server.tools.lifecycle` imports
    from the module that defines each name.
    """
    assert USER_ONBOARDED_EVENT_TYPE == "user.onboarded"
    assert JOINER_PRINCIPALS == ("employee",)
    assert DuplicateEmailError.code is ErrorCode.POLICY_BLOCKED
    assert DuplicateEmailError.code.value == "POLICY_BLOCKED"
    assert lifecycle_module.onboard_user is onboard_user
    assert lifecycle_module.USER_ONBOARDED_EVENT_TYPE == USER_ONBOARDED_EVENT_TYPE


def test_the_unusable_password_hash_verifies_against_nothing() -> None:
    """Decision C: catches a joiner sentinel that some input can match.

    `verify_password` answers False for a malformed hash and False for a
    mismatch, so the empty password and an arbitrary one are both asked: an
    implementation that set `password_hash` to the empty string, to a hash of
    a known constant, or to anything a caller could guess would let a brand-new
    account with no credential be logged into. The sentinel is also asserted
    non-empty and not a valid argon2 encoding, so "no hash at all" cannot pass
    as "an unusable hash".
    """
    assert UNUSABLE_PASSWORD_HASH != ""
    assert not UNUSABLE_PASSWORD_HASH.startswith("$argon2")
    assert verify_password("", UNUSABLE_PASSWORD_HASH) is False
    assert verify_password("anything", UNUSABLE_PASSWORD_HASH) is False
    assert verify_password(UNUSABLE_PASSWORD_HASH, UNUSABLE_PASSWORD_HASH) is False


# --- the audit record (criterion 3, a project rule) ---------------------------


def test_user_onboarded_names_the_new_user_and_carries_exactly_six_keys(
    access_db_url: str,  # noqa: F811
) -> None:
    """a project rule and decision F: catches an event whose subject is the
    department, the caller or the tool rather than the NEW USER, and one whose
    payload has drifted.

    An auditor asking "how did this person get their access" queries
    `subject_id`; an event subject naming anything else answers that question
    for nobody, while a count-only assertion still passes. The payload keys are
    compared as an exact set, so a dropped `manager_user_id` (decision E's only
    record of who the joiner reports to — there is no column for it) or a
    dropped `skipped_role_ids` (the only record that the template was not fully
    applied) fails here. Both role lists are compared by value against what the
    same call returned, so an event that reports the template rather than what
    was actually applied is caught too; the department deliberately contains a
    conflicting pair so the skipped list is non-empty and cannot pass as `[]`.
    """
    department_id = _make_department(access_db_url, (SOD_ROLE_A, SOD_ROLE_B, SOD_NEIGHBOUR))
    email = _email()
    events_before = len(_onboarded_events(access_db_url))

    result = _onboard(access_db_url, email=email, department_id=department_id)

    events = _onboarded_events(access_db_url)
    assert len(events) == events_before + 1, "exactly one user.onboarded per onboarding"
    subject_type, subject_id, payload = events[-1]
    assert subject_type == "user"
    assert subject_id == str(result.user.user_id)
    assert set(payload) == EXPECTED_PAYLOAD_KEYS
    assert payload["user_id"] == str(result.user.user_id)
    assert payload["email"] == email
    assert payload["department_id"] == str(department_id)
    assert payload["manager_user_id"] == str(NEW_MANAGER.id)
    assert sorted(payload["applied_role_ids"]) == sorted(
        str(grant.role_id) for grant in result.applied
    )
    assert sorted(payload["skipped_role_ids"]) == sorted(
        str(role_id) for role_id, _ in result.skipped
    )
    assert payload["skipped_role_ids"], "premise: this department's template is deliberately broken"


def test_the_directory_port_answers_the_departments_template(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches `list_department_role_ids` reading the wrong side of
    `role_templates` — the role ids, keyed by DEPARTMENT.

    `list_membership_role_ids` already answers this question for a USER, and
    the obvious implementation of the new method delegates to it. That cannot
    work for onboarding: the user does not exist yet when the template has to
    be read. A method that ignored its argument and returned every template
    row in the tenant, or the empty set, would leave the three cases above
    passing only by accident of which department they built, so the port is
    asked directly here with two departments whose templates differ.
    """
    first = _make_department(access_db_url, TEMPLATE_ROLES)
    second = _make_department(access_db_url, (SOD_NEIGHBOUR,))

    async def scenario() -> tuple[frozenset[uuid.UUID], frozenset[uuid.UUID]]:
        engine = create_engine(access_db_url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                directory = SimulatedDirectory(session)
                return (
                    await directory.list_department_role_ids(first),
                    await directory.list_department_role_ids(second),
                )
        finally:
            await engine.dispose()

    first_roles, second_roles = asyncio.run(scenario())

    assert first_roles == frozenset(TEMPLATE_ROLES)
    assert second_roles == frozenset({SOD_NEIGHBOUR})
