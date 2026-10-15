"""An earlier change §6: `sweep_leavers`, and decision D's system actor on both engines.

Written before the implementation exists. Expected surface (the implementer
must provide exactly this):

``helpdesk.access.leavers``
    ``LEAVER_SWEEP_ACTOR_REF = "scheduler:leaver_sweep"`` — what every audit
    event this job causes is filed under.
    ``async def sweep_leavers(session, *, now) -> list[User]`` — over the
    BOUND tenant only, it offboards every user whose `status` is `active` and
    whose `end_date` is strictly before `now.date()` (decision E), calling
    `offboard_user` with a `SimulatedDirectory`, that user's OWN `end_date`
    and the system actor, all in the caller's transaction. A user turned
    `leaver` between the read and their own offboard is SKIPPED
    (`AlreadyLeaverError` is caught, decision I) and the rest of the tenant is
    still offboarded; every other failure raises to the runner.

``helpdesk.access.lifecycle.offboard_user`` and
``helpdesk.access.revoke.revoke_role_access``
    each gain a keyword ``actor_kind: str = "user"``, and their actor-id
    argument (``offboarded_by_user_id`` / ``revoked_by_user_id``) widens to
    ``uuid.UUID | None``. Both refuse with a typed ``VALIDATION`` error when
    the actor id AND ``actor_ref`` are both absent — there has to be SOMETHING
    in ``actor_ref``, or the audit log gains an event under a system kind with
    nobody named at all.

The default is the whole risk of decision D (plan §5): these are the two
most-covered engines in the tree, and a silent flip would relabel every human
revocation in the log as a system one. That is what the two "default
preserved" cases below exist for, and they call the engines exactly the way
the MCP tools do.

a project rule: ONE session-scoped database for the whole run, imported from
`test_access_expiry.py` rather than rebuilt. Per-test isolation is a
namespace, not a rebuild — this job sweeps a whole tenant, so every case seeds
a tenant of its OWN with `_seed.seed_tenant`. That seeded tenant's only user
is `active` with a NULL `end_date`, so it is never due and every count below
stays exact.

No pytest-asyncio: each case is a sync function driving one `asyncio.run`.
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from helpdesk.shared.access import leavers as leavers_module
from helpdesk.shared.access.leavers import LEAVER_SWEEP_ACTOR_REF, sweep_leavers
from helpdesk.shared.access.revoke import GRANT_REVOKED_EVENT_TYPE, revoke_role_access
from helpdesk.shared.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.grants import ACTIVE_GRANT_STATUS, REVOKED_GRANT_STATUS
from helpdesk.shared.models import Grant, User
from helpdesk.shared.services.access import lifecycle as lifecycle_module
from helpdesk.shared.services.access.directory import DirectoryProvider, SimulatedDirectory
from helpdesk.shared.services.access.lifecycle import (
    LEAVER_STATUS,
    OFFBOARD_REVOKE_REASON,
    USER_OFFBOARDED_EVENT_TYPE,
    AlreadyLeaverError,
    OffboardResult,
    offboard_user,
)

# Imported, not redefined: one database with the schema created once, shared
# with `test_access_expiry.py` and `test_access_expiry_warnings.py` (standing
# rule 7). No two of those files ever look at the same tenant.
from tests.shared.test_access_expiry import expiry_db_url  # noqa: F401
from tests.support.seed import SeededTenant, seed_tenant

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
"""The one instant every sweep answers for. Passed in, never read from a clock."""

TODAY = NOW.date()
YESTERDAY = TODAY - timedelta(days=1)
"""Decision E's inclusive half: the last day of access was yesterday."""
LONG_GONE = TODAY - timedelta(days=10)
"""A second due date, far from `YESTERDAY`, so "each user's OWN end_date" is
observable: an implementation reusing `now.date()` or the first user's date
writes the same value for both."""
STILL_HERE = TODAY + timedelta(days=30)

GRANTED_AT = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
FAR_FUTURE = datetime(2099, 6, 1, 12, 0, tzinfo=UTC)
"""Every planted grant is live at `NOW`, so a revocation cannot pass as an
expiry."""

ACTIVE_USER_STATUS = "active"
TENURED_START = date(2019, 3, 7)

SYSTEM_ACTOR_KIND = "system"
USER_ACTOR_KIND = "user"
"""The two `audit_events.actor_kind` values decision D turns on."""


@dataclass(frozen=True)
class UserFacts:
    """What one `users` row says about the person's departure."""

    status: str
    end_date: date | None


@dataclass(frozen=True)
class GrantFacts:
    """What one `grants` row says, copied out before its session closes."""

    grant_id: uuid.UUID
    status: str
    revoked_at: datetime | None
    revoke_reason: str | None


@dataclass(frozen=True)
class EventFacts:
    """What one `audit_events` row says about WHO caused it."""

    actor_kind: str
    actor_ref: str
    subject_type: str
    subject_id: str
    payload: dict[str, Any]


# --- the per-case namespace ---------------------------------------------------


def _tenant(url: str) -> SeededTenant:
    """A tenant of this case's own: the per-test namespace.

    `seed_tenant`'s user is `active` with a NULL `end_date` and holds one
    never-expiring grant, so it is never due: an implementation that swept it
    anyway would show up as an extra `user.offboarded` in every count below.
    """

    async def prepare() -> SeededTenant:
        engine = create_engine(url)
        try:
            return await seed_tenant(
                create_session_factory(engine), f"leaver-{uuid.uuid4().hex[:12]}"
            )
        finally:
            await engine.dispose()

    return asyncio.run(prepare())


def _plant_user(
    url: str,
    tenant: SeededTenant,
    *,
    end_date: date | None,
    status: str = ACTIVE_USER_STATUS,
) -> uuid.UUID:
    """One committed, obviously fake person in the seeded department."""
    suffix = uuid.uuid4().hex[:12]

    async def scenario() -> uuid.UUID:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant.tenant_id) as session:
                row = await Repository(session, User).create(
                    department_id=tenant.department_id,
                    email=f"fake.leaver.{suffix}@example.test",
                    display_name=f"Fake Leaver {suffix}",
                    password_hash="argon2id$obviously-fake-test-hash",
                    principals=["employee"],
                    status=status,
                    start_date=TENURED_START,
                    end_date=end_date,
                )
                return row.id
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _plant_grant(
    url: str,
    tenant: SeededTenant,
    *,
    user_id: uuid.UUID,
    role_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """One committed live `grants` row, held at `NOW`."""

    async def scenario() -> uuid.UUID:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant.tenant_id) as session:
                row = await Repository(session, Grant).create(
                    user_id=user_id,
                    role_id=tenant.role_b_id if role_id is None else role_id,
                    request_id=None,
                    granted_by_user_id=None,
                    granted_at=GRANTED_AT,
                    expires_at=FAR_FUTURE,
                    revoked_at=None,
                    revoke_reason=None,
                    status=ACTIVE_GRANT_STATUS,
                )
                return row.id
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


# --- driving the surface ------------------------------------------------------


def _sweep(url: str, tenant_id: uuid.UUID, *, now: datetime = NOW) -> None:
    """One `sweep_leavers` in one committed transaction."""

    async def scenario() -> None:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant_id) as session:
                await sweep_leavers(session, now=now)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def _offboard_directly(
    url: str,
    tenant_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    offboarded_by_user_id: uuid.UUID | None,
    end_date: date = YESTERDAY,
) -> None:
    """`offboard_user` called the way `mcp_server.tools.lifecycle` calls it.

    An actor id, no `actor_kind` and no `actor_ref`: the shape every existing
    caller uses, which decision D's default has to leave unchanged.
    """

    async def scenario() -> None:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant_id) as session:
                await offboard_user(
                    session,
                    SimulatedDirectory(session),
                    user_id=user_id,
                    end_date=end_date,
                    offboarded_by_user_id=offboarded_by_user_id,
                    session_id=None,
                    now=NOW,
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def _revoke_directly(
    url: str,
    tenant_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    role_id: uuid.UUID,
    revoked_by_user_id: uuid.UUID | None,
) -> None:
    """`revoke_role_access` called the way `mcp_server.tools.access` calls it."""

    async def scenario() -> None:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant_id) as session:
                await revoke_role_access(
                    session,
                    SimulatedDirectory(session),
                    user_id=user_id,
                    role_id=role_id,
                    reason="fake manual revocation, decided by a person",
                    revoked_by_user_id=revoked_by_user_id,
                    session_id=None,
                    now=NOW,
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


# --- probes around the surface, never through it ------------------------------


def _user_facts(url: str, tenant_id: uuid.UUID, user_id: uuid.UUID) -> UserFacts:
    async def scenario() -> UserFacts:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant_id) as session:
                row = await Repository(session, User).get(user_id)
                assert row is not None, f"planted user {user_id} is gone"
                return UserFacts(status=row.status, end_date=row.end_date)
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _grant_facts(url: str, tenant_id: uuid.UUID, grant_id: uuid.UUID) -> GrantFacts:
    async def scenario() -> GrantFacts:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant_id) as session:
                row = await Repository(session, Grant).get(grant_id)
                assert row is not None, f"planted grant {grant_id} is gone"
                return GrantFacts(
                    grant_id=row.id,
                    status=row.status,
                    revoked_at=row.revoked_at,
                    revoke_reason=row.revoke_reason,
                )
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _events(url: str, tenant_id: uuid.UUID, event_type: str) -> list[EventFacts]:
    """Every event of one type in one tenant, in `seq` order."""

    async def scenario() -> list[EventFacts]:
        engine = create_engine(url)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(
                    text(
                        "SELECT actor_kind, actor_ref, subject_type, subject_id, payload "
                        "FROM audit_events WHERE tenant_id = :tenant "
                        "AND event_type = :event_type ORDER BY seq"
                    ),
                    {"tenant": tenant_id, "event_type": event_type},
                )
                return [
                    EventFacts(
                        actor_kind=str(row[0]),
                        actor_ref=str(row[1]),
                        subject_type=str(row[2]),
                        subject_id=str(row[3]),
                        payload=dict(row[4]),
                    )
                    for row in result.all()
                ]
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


# --- who is due (criterion 6, decision E) -------------------------------------


def test_a_user_whose_end_date_has_passed_is_offboarded_with_their_grants_revoked(
    expiry_db_url: str,  # noqa: F811
) -> None:
    """Criterion 6's main claim: the sweep DELEGATES, it does not decide.

    Catches a sweep that only writes `status = 'leaver'` — the cheap
    implementation, and the dangerous one: the design blocks a leaver from
    asking for anything new, so the tables would look tidy while every account
    the person already holds stays live after they have gone. The grant is
    asserted `revoked` with `OFFBOARD_REVOKE_REASON`, which is
    `offboard_user`'s own sentence, so a sweep that revoked the rows itself
    with its own reason fails here too.

    `end_date` is re-read and asserted UNCHANGED: the person's departure date
    is a fact about them, not about the day a job happened to notice it, and
    an implementation passing `now.date()` down would silently rewrite ten
    days of history on the first tick after a delayed deployment.
    """
    tenant = _tenant(expiry_db_url)
    user_id = _plant_user(expiry_db_url, tenant, end_date=LONG_GONE)
    grant_id = _plant_grant(expiry_db_url, tenant, user_id=user_id)

    _sweep(expiry_db_url, tenant.tenant_id)

    facts = _user_facts(expiry_db_url, tenant.tenant_id, user_id)
    assert facts.status == LEAVER_STATUS
    assert facts.end_date == LONG_GONE
    grant = _grant_facts(expiry_db_url, tenant.tenant_id, grant_id)
    assert grant.status == REVOKED_GRANT_STATUS
    assert grant.revoked_at == NOW
    assert grant.revoke_reason == OFFBOARD_REVOKE_REASON


def test_a_user_whose_end_date_was_yesterday_is_swept(
    expiry_db_url: str,  # noqa: F811
) -> None:
    """Decision E's inclusive half, at the exact day.

    Catches a bound written `end_date < now.date() - 1` or one comparing the
    stored DATE against the sweep's TIMESTAMP (`end_date < now` in a column of
    dates, which Postgres widens to midnight and which therefore skips the
    person who left yesterday until the day after). Somebody whose last day
    was yesterday still holding every account today is exactly the gap this
    job closes.
    """
    tenant = _tenant(expiry_db_url)
    user_id = _plant_user(expiry_db_url, tenant, end_date=YESTERDAY)

    _sweep(expiry_db_url, tenant.tenant_id)

    assert _user_facts(expiry_db_url, tenant.tenant_id, user_id).status == LEAVER_STATUS


def test_a_user_whose_end_date_is_today_is_untouched(
    expiry_db_url: str,  # noqa: F811
) -> None:
    """Decision E's exclusive half: `<`, never `<=` (the design's own comparison).

    `end_date` is the LAST day the person holds access, so a `<=` takes every
    account off them in the middle of their final working day — the handover
    day, when they need it most, and the audit log would show a departure
    dated today with no leaver anywhere. Only the lower bound can refuse this
    row: the user is `active` and has a non-null end date, so every other
    conjunct admits it.
    """
    tenant = _tenant(expiry_db_url)
    user_id = _plant_user(expiry_db_url, tenant, end_date=TODAY)
    grant_id = _plant_grant(expiry_db_url, tenant, user_id=user_id)

    _sweep(expiry_db_url, tenant.tenant_id)

    facts = _user_facts(expiry_db_url, tenant.tenant_id, user_id)
    assert facts.status == ACTIVE_USER_STATUS
    assert facts.end_date == TODAY
    assert _grant_facts(expiry_db_url, tenant.tenant_id, grant_id).status == ACTIVE_GRANT_STATUS
    assert _events(expiry_db_url, tenant.tenant_id, USER_OFFBOARDED_EVENT_TYPE) == []


def test_a_user_with_no_end_date_is_never_due(
    expiry_db_url: str,  # noqa: F811
) -> None:
    """The `end_date IS NOT NULL` half of the due set, alone.

    Postgres compares NULL to a date as unknown, so this only bites an
    implementation that reads the tenant's users and filters them in Python
    with a fallback (`(user.end_date or date.min) < today`) — and THAT one
    offboards every permanent employee in the tenant on its first tick, in one
    transaction, as `system`. The planted user is `active` with a live grant
    and a future-dated colleague beside them, so nothing but the null test can
    keep them.
    """
    tenant = _tenant(expiry_db_url)
    permanent = _plant_user(expiry_db_url, tenant, end_date=None)
    staying = _plant_user(expiry_db_url, tenant, end_date=STILL_HERE)
    grant_id = _plant_grant(expiry_db_url, tenant, user_id=permanent)

    _sweep(expiry_db_url, tenant.tenant_id)

    assert _user_facts(expiry_db_url, tenant.tenant_id, permanent).status == ACTIVE_USER_STATUS
    assert _user_facts(expiry_db_url, tenant.tenant_id, staying).status == ACTIVE_USER_STATUS
    assert _grant_facts(expiry_db_url, tenant.tenant_id, grant_id).status == ACTIVE_GRANT_STATUS
    assert _events(expiry_db_url, tenant.tenant_id, USER_OFFBOARDED_EVENT_TYPE) == []


def test_a_user_who_is_already_a_leaver_is_not_swept_again(
    expiry_db_url: str,  # noqa: F811
) -> None:
    """The `status = 'active'` half of the due set.

    No kill claim for that conjunct ALONE: decision I's `AlreadyLeaverError`
    catch answers this row as well, so the case cannot tell the two apart
    (the neighbouring-guard trap, a project rule and 31). What it does
    catch is an implementation with NEITHER — a due set of `end_date < today`
    with the refusal left to propagate, which raises on the first person who
    ever left and rolls back the whole tenant's sweep, every day, for ever.
    The already-departed user keeps a live grant here, so a re-run that got as
    far as revoking would be visible too.
    """
    tenant = _tenant(expiry_db_url)
    departed = _plant_user(expiry_db_url, tenant, end_date=LONG_GONE, status=LEAVER_STATUS)
    grant_id = _plant_grant(expiry_db_url, tenant, user_id=departed)
    due = _plant_user(expiry_db_url, tenant, end_date=YESTERDAY)

    _sweep(expiry_db_url, tenant.tenant_id)

    assert _user_facts(expiry_db_url, tenant.tenant_id, due).status == LEAVER_STATUS
    assert _grant_facts(expiry_db_url, tenant.tenant_id, grant_id).status == ACTIVE_GRANT_STATUS
    offboarded = _events(expiry_db_url, tenant.tenant_id, USER_OFFBOARDED_EVENT_TYPE)
    assert [event.subject_id for event in offboarded] == [str(due)]


def test_another_tenants_due_user_is_untouched(
    expiry_db_url: str,  # noqa: F811
) -> None:
    """Catches a sweep that builds its own statement over `users` instead of
    going through the tenant-bound session.

    The runner calls this once per tenant, so a query that ignores the binding
    offboards every tenant's departures on the first tenant's tick — every
    account of every company in the deployment revoked in one transaction,
    filed under the wrong audit chain. The neighbour's user is due by exactly
    the same date, so only the scoping can save them.
    """
    tenant = _tenant(expiry_db_url)
    neighbour = _tenant(expiry_db_url)
    mine = _plant_user(expiry_db_url, tenant, end_date=YESTERDAY)
    theirs = _plant_user(expiry_db_url, neighbour, end_date=YESTERDAY)

    _sweep(expiry_db_url, tenant.tenant_id)

    assert _user_facts(expiry_db_url, tenant.tenant_id, mine).status == LEAVER_STATUS
    assert _user_facts(expiry_db_url, neighbour.tenant_id, theirs).status == ACTIVE_USER_STATUS
    assert _events(expiry_db_url, neighbour.tenant_id, USER_OFFBOARDED_EVENT_TYPE) == []


def test_each_swept_user_keeps_their_own_end_date(
    expiry_db_url: str,  # noqa: F811
) -> None:
    """Criterion 6's "that user's OWN end_date", pinned with two dates.

    This is the case that catches `now.date()` being reused as everybody's
    departure date, and the subtler one where the loop passes the FIRST due
    user's date to every subsequent offboard. Both users are due, ten days
    apart, and both the column and the `user.offboarded` payload are read: the
    payload is the only record of the date that was asked for, so an event
    carrying today's date for a departure ten days old rewrites the history an
    auditor reconstructs a leaver's access from.
    """
    tenant = _tenant(expiry_db_url)
    recent = _plant_user(expiry_db_url, tenant, end_date=YESTERDAY)
    old = _plant_user(expiry_db_url, tenant, end_date=LONG_GONE)
    assert YESTERDAY != LONG_GONE != TODAY, "premise: three distinct dates"

    _sweep(expiry_db_url, tenant.tenant_id)

    assert _user_facts(expiry_db_url, tenant.tenant_id, recent).end_date == YESTERDAY
    assert _user_facts(expiry_db_url, tenant.tenant_id, old).end_date == LONG_GONE
    dates = {
        event.subject_id: event.payload["end_date"]
        for event in _events(expiry_db_url, tenant.tenant_id, USER_OFFBOARDED_EVENT_TYPE)
    }
    assert dates == {
        str(recent): YESTERDAY.isoformat(),
        str(old): LONG_GONE.isoformat(),
    }


# --- who the audit log says did it (criterion 7, a project rule) ------------


def test_every_event_the_sweep_causes_carries_the_system_actor_and_its_ref(
    expiry_db_url: str,  # noqa: F811
) -> None:
    """Criterion 7, on BOTH event types the sweep produces.

    Catches decision D's rejected alternative: leaving `actor_kind` at `user`
    and putting something system-shaped in `actor_ref` only. An auditor
    filtering the log for human decisions would then find an automatic nightly
    sweep among them, which is the exact confusion `expire_grants`' own
    `system` actor was written to avoid — and `grant.revoked` is the event
    that matters most, because a revocation filed as a person's is a decision
    attributed to somebody who never made it.

    The `grant.revoked` half is what a system actor threaded only as far as
    `offboard_user` fails: two grants are planted, so the per-row events
    cannot pass as the summary one.
    """
    tenant = _tenant(expiry_db_url)
    user_id = _plant_user(expiry_db_url, tenant, end_date=YESTERDAY)
    first = _plant_grant(expiry_db_url, tenant, user_id=user_id, role_id=tenant.role_id)
    second = _plant_grant(expiry_db_url, tenant, user_id=user_id, role_id=tenant.role_b_id)

    _sweep(expiry_db_url, tenant.tenant_id)

    offboarded = _events(expiry_db_url, tenant.tenant_id, USER_OFFBOARDED_EVENT_TYPE)
    assert [event.subject_id for event in offboarded] == [str(user_id)]
    assert offboarded[0].actor_kind == SYSTEM_ACTOR_KIND
    assert offboarded[0].actor_ref == LEAVER_SWEEP_ACTOR_REF
    assert offboarded[0].actor_ref == "scheduler:leaver_sweep"

    revoked = _events(expiry_db_url, tenant.tenant_id, GRANT_REVOKED_EVENT_TYPE)
    assert {event.subject_id for event in revoked} == {str(first), str(second)}
    for event in revoked:
        assert event.actor_kind == SYSTEM_ACTOR_KIND
        assert event.actor_ref == LEAVER_SWEEP_ACTOR_REF


def test_an_offboard_with_an_actor_id_and_no_actor_kind_still_writes_the_user_actor(
    expiry_db_url: str,  # noqa: F811
) -> None:
    """Criterion 8 for `offboard_user`, called exactly as the MCP tool calls
    it: an actor id, no `actor_kind`.

    Catches decision D's parameter defaulting to `"system"` — one word in a
    signature that relabels every human departure in every tenant's log as an
    automatic one, and which nothing else in the suite would notice, because
    no existing case reads `actor_kind` on this event. `actor_ref` is asserted
    to still resolve to the actor's id, so a widened `uuid.UUID | None` that
    stopped rendering the id (leaving the ref empty or literal `None`) fails
    here as well.
    """
    tenant = _tenant(expiry_db_url)
    actor = tenant.user_id
    user_id = _plant_user(expiry_db_url, tenant, end_date=YESTERDAY)
    _plant_grant(expiry_db_url, tenant, user_id=user_id)

    _offboard_directly(
        expiry_db_url,
        tenant.tenant_id,
        user_id=user_id,
        offboarded_by_user_id=actor,
    )

    offboarded = _events(expiry_db_url, tenant.tenant_id, USER_OFFBOARDED_EVENT_TYPE)
    assert [event.subject_id for event in offboarded] == [str(user_id)]
    assert offboarded[0].actor_kind == USER_ACTOR_KIND
    assert offboarded[0].actor_ref == str(actor)
    revoked = _events(expiry_db_url, tenant.tenant_id, GRANT_REVOKED_EVENT_TYPE)
    assert len(revoked) == 1
    assert revoked[0].actor_kind == USER_ACTOR_KIND
    assert revoked[0].actor_ref == str(actor)


def test_a_revocation_with_an_actor_id_and_no_actor_kind_still_writes_the_user_actor(
    expiry_db_url: str,  # noqa: F811
) -> None:
    """Criterion 8 for `revoke_role_access`, reached directly rather than
    through an offboard.

    The engine has its own call site — `mcp_server.tools.access.revoke_access`
    — so a default flipped there is not covered by the offboard case above:
    the two functions each gain their own parameter (§2), and a manual
    revocation relabelled `system` is a person's decision the log no longer
    attributes to anybody. The grant is revoked by a DIFFERENT user from its
    holder, so the ref cannot pass by matching the holder's id.
    """
    tenant = _tenant(expiry_db_url)
    actor = tenant.user_id
    holder = _plant_user(expiry_db_url, tenant, end_date=None)
    grant_id = _plant_grant(expiry_db_url, tenant, user_id=holder, role_id=tenant.role_b_id)
    assert actor != holder, "premise: the revoker is not the holder"

    _revoke_directly(
        expiry_db_url,
        tenant.tenant_id,
        user_id=holder,
        role_id=tenant.role_b_id,
        revoked_by_user_id=actor,
    )

    revoked = _events(expiry_db_url, tenant.tenant_id, GRANT_REVOKED_EVENT_TYPE)
    assert [event.subject_id for event in revoked] == [str(grant_id)]
    assert revoked[0].actor_kind == USER_ACTOR_KIND
    assert revoked[0].actor_ref == str(actor)


# --- the refusal decision D has to carry (criterion 8's second half) ----------


def test_an_offboard_with_neither_an_actor_id_nor_an_actor_ref_is_refused(
    expiry_db_url: str,  # noqa: F811
) -> None:
    """Criterion 8's refusal for `offboard_user`, and the price of widening the
    id to `uuid.UUID | None`.

    Catches the widening done WITHOUT the check: `str(None)` renders the
    literal `"None"` into `actor_ref`, and an offboard would then be filed
    against a departure nobody is named for — unattributable, and
    indistinguishable in a query from a real actor called `None`. `VALIDATION`
    is asserted as the code, not just "an exception", because `AttributeError`
    from an unrendered id would also stop the write while telling the runner
    nothing. Nothing may be written first: the user's status, their grant and
    the event count are all re-read.
    """
    tenant = _tenant(expiry_db_url)
    user_id = _plant_user(expiry_db_url, tenant, end_date=YESTERDAY)
    grant_id = _plant_grant(expiry_db_url, tenant, user_id=user_id)

    with pytest.raises(ToolError) as excinfo:
        _offboard_directly(
            expiry_db_url,
            tenant.tenant_id,
            user_id=user_id,
            offboarded_by_user_id=None,
        )

    assert excinfo.value.code is ErrorCode.VALIDATION
    assert excinfo.value.code.value == "VALIDATION"
    assert _user_facts(expiry_db_url, tenant.tenant_id, user_id).status == ACTIVE_USER_STATUS
    assert _grant_facts(expiry_db_url, tenant.tenant_id, grant_id).status == ACTIVE_GRANT_STATUS
    assert _events(expiry_db_url, tenant.tenant_id, USER_OFFBOARDED_EVENT_TYPE) == []


def test_a_revocation_with_neither_an_actor_id_nor_an_actor_ref_is_refused(
    expiry_db_url: str,  # noqa: F811
) -> None:
    """The same refusal on `revoke_role_access`, which has its own call site
    and therefore its own way of being called with nothing.

    a project rule: the check is applied at BOTH engines the plan widens, or
    the one left out writes `grant.revoked` events with `"None"` in
    `actor_ref` — the per-row events, which are what an auditor reads to find
    who ended a specific grant. The grant is asserted still `active`, so a
    refusal raised after the rows were stamped fails too.
    """
    tenant = _tenant(expiry_db_url)
    holder = _plant_user(expiry_db_url, tenant, end_date=None)
    grant_id = _plant_grant(expiry_db_url, tenant, user_id=holder, role_id=tenant.role_b_id)

    with pytest.raises(ToolError) as excinfo:
        _revoke_directly(
            expiry_db_url,
            tenant.tenant_id,
            user_id=holder,
            role_id=tenant.role_b_id,
            revoked_by_user_id=None,
        )

    assert excinfo.value.code is ErrorCode.VALIDATION
    assert _grant_facts(expiry_db_url, tenant.tenant_id, grant_id).status == ACTIVE_GRANT_STATUS
    assert _events(expiry_db_url, tenant.tenant_id, GRANT_REVOKED_EVENT_TYPE) == []


# --- the concurrent departure (criterion 9, decision I) -----------------------


def test_a_user_turned_leaver_mid_sweep_is_skipped_and_the_rest_still_go(
    expiry_db_url: str,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision I, made deterministic, and the case criterion 9 names.

    No kill claim for the row lock: a real second caller
    would have to interleave inside a `FOR UPDATE`, which two threads sharing
    a database almost never do on demand. The state is produced instead the
    way `test_access_offboard.py::_WithPhantomRole` produces decision H's —
    `offboard_user` is replaced by a proxy that raises `AlreadyLeaverError`
    for ONE user id and delegates every other call to the real engine, which
    is exactly the shape the engine itself raises in (after `lock_user`,
    before any write).

    What it catches is an uncaught refusal: the whole tenant's sweep runs in
    ONE transaction (plan §5), so one person who happened to be offboarded by
    hand this morning would roll back every OTHER departure of that day and
    keep doing so on every tick until somebody noticed. The surviving user's
    status, their revoked grant and a single `user.offboarded` are asserted,
    so a sweep that caught the error and then abandoned the loop fails too.
    """
    tenant = _tenant(expiry_db_url)
    skipped = _plant_user(expiry_db_url, tenant, end_date=LONG_GONE)
    surviving = _plant_user(expiry_db_url, tenant, end_date=YESTERDAY)
    grant_id = _plant_grant(expiry_db_url, tenant, user_id=surviving)

    _sweep_with_a_concurrent_departure(
        expiry_db_url, tenant.tenant_id, raising_for=skipped, monkeypatch=monkeypatch
    )

    assert _user_facts(expiry_db_url, tenant.tenant_id, skipped).status == ACTIVE_USER_STATUS
    assert _user_facts(expiry_db_url, tenant.tenant_id, surviving).status == LEAVER_STATUS
    assert _grant_facts(expiry_db_url, tenant.tenant_id, grant_id).status == REVOKED_GRANT_STATUS
    offboarded = _events(expiry_db_url, tenant.tenant_id, USER_OFFBOARDED_EVENT_TYPE)
    assert [event.subject_id for event in offboarded] == [str(surviving)]


def _sweep_with_a_concurrent_departure(
    url: str,
    tenant_id: uuid.UUID,
    *,
    raising_for: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One sweep whose `offboard_user` refuses exactly one of the due users.

    The proxy is installed on BOTH module attributes, because the engine can
    be reached either as a name imported into `leavers` or through the
    `lifecycle` module; whichever the implementation uses, one of the two
    patches is the one that takes effect (`raising=False`, so the module that
    does not carry the name is simply skipped).
    """

    async def proxy(
        session: Any,
        directory: DirectoryProvider,
        *,
        user_id: uuid.UUID,
        end_date: date,
        offboarded_by_user_id: uuid.UUID | None = None,
        actor_ref: str | None = None,
        actor_kind: str = USER_ACTOR_KIND,
        session_id: uuid.UUID | None = None,
        now: datetime,
    ) -> OffboardResult:
        if user_id == raising_for:
            raise AlreadyLeaverError("somebody else offboarded this user first")
        return await offboard_user(
            session,
            directory,
            user_id=user_id,
            end_date=end_date,
            offboarded_by_user_id=offboarded_by_user_id,
            actor_ref=actor_ref,
            actor_kind=actor_kind,
            session_id=session_id,
            now=now,
        )

    assert hasattr(leavers_module, "offboard_user") or hasattr(lifecycle_module, "offboard_user"), (
        "premise: one of the two modules exposes offboard_user to patch"
    )
    for module in (leavers_module, lifecycle_module):
        monkeypatch.setattr(module, "offboard_user", proxy, raising=False)

    _sweep(url, tenant_id)


# --- the constants and the exports --------------------------


def test_the_leaver_sweep_constants_are_exactly_the_planned_values() -> None:
    """a project rule on what this half of the plan introduces.

    `LEAVER_SWEEP_ACTOR_REF` is the string an auditor greps `actor_ref` for to
    separate the nightly sweep's departures from a person's, and every event
    assertion above reads it as a constant — so a value drifting to
    `"leaver_sweep"` or `"scheduler"` would leave those cases green and the
    log unqueryable by the one name the runbook will print. The export is part
    of the surface: `scheduler.jobs` imports the engine from
    `helpdesk.access`, the way every other caller does.
    """
    assert LEAVER_SWEEP_ACTOR_REF == "scheduler:leaver_sweep"
    assert leavers_module.sweep_leavers is sweep_leavers
