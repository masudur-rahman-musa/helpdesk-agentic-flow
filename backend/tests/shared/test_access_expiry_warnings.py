"""An earlier change §6: `warn_expiring_grants`, called directly with a fixed `now`.

Written before the implementation exists. Expected surface (the implementer
must provide exactly this):

``helpdesk.models.notification``
    ``NotificationKind.GRANT_EXPIRING = "grant_expiring"``, the second and
    last member of the enum after this plan (§8 widens
    `test_notification_models.py`'s set to exactly
    `{"grant_expired", "grant_expiring"}`).

``helpdesk.access.expiry``
    ``EXPIRY_WARNING_HORIZON_DAYS = 14`` — the band's width.
    ``_WARNING_TITLE`` — the fixed title literal every warning carries. The
    role name goes in the BODY, so a long role name can never reach `notify`'s
    200-character title cap (criterion 2, decision B).
    ``async def warn_expiring_grants(session, *, now) -> list[Grant]`` — over
    the BOUND tenant only, one `grant_expiring` notification to the HOLDER of
    every `active`, unrevoked grant whose `expires_at` falls in
    `(now, now + 14 days]`, with `subject_type` `grant`, `subject_id` the
    grant id and `read_at` null. A grant held by a `leaver` is never warned
    (decision C), and a grant that already carries a `grant_expiring`
    notification for its own id is not warned again (decision A's dedupe).

The return value is never asserted here: plan §6 fixes no return shape, and
every claim below is readable from the `notifications` table.

Decision A is the boundary this file exists for. The band is inclusive at the
top and strict at the bottom: `now + 14 days` exactly is warned, one
microsecond later is not, and `now` exactly is not — that row is
`expire_grants`', and warning about a grant in the same tick that ends it
would put two contradictory rows in one person's bell.

a project rule: ONE session-scoped database for the whole run, imported from
`test_access_expiry.py` rather than rebuilt — the fixture there creates the
schema exactly once. Per-test isolation is a namespace, not a rebuild: this
job sweeps a whole tenant, so every case seeds a tenant of its OWN with
`_seed.seed_tenant` and no case can see another's rows. `seed_tenant`'s own
grant carries a NULL `expires_at`, so it is never warned and every "no rows"
assertion below stays exact.

a project rule: `test_access_expiry.py` (21a's sweep) and this file are
distinct basenames, and neither collides with anything in the tree.

No pytest-asyncio: each case is a sync function driving one `asyncio.run`.
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from _seed import SeededTenant, seed_tenant

# Imported, not redefined: one database with the schema created once, shared
# with `test_access_expiry.py`. The two files never look at
# the same tenant, so sharing it costs nothing and saves a whole database.
from test_access_expiry import expiry_db_url  # noqa: F401

from helpdesk.shared import access as access_package
from helpdesk.shared.access.expiry import (
    _WARNING_TITLE,
    EXPIRY_WARNING_HORIZON_DAYS,
    warn_expiring_grants,
)
from helpdesk.shared.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.shared.core.repository import ACTIVE_GRANT_STATUS, Repository
from helpdesk.shared.models import Grant, Notification, Role, User
from helpdesk.shared.models.notification import NOTIFICATION_TITLE_MAX_LENGTH, NotificationKind

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
"""The one instant every case warns at. Passed in, never read from a clock."""

GRANTED_AT = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)

HORIZON = timedelta(days=14)
"""Decision A's band width, written as a literal here on purpose.

Asserted equal to `EXPIRY_WARNING_HORIZON_DAYS` by the constants case below,
so a horizon widened in the module moves the boundary cases only through a
test that fails first."""

INSIDE = NOW + timedelta(days=7)
"""Comfortably inside the band: the ordinary grant about to lapse."""
AT_HORIZON = NOW + HORIZON
"""The inclusive top of the band."""
JUST_PAST_HORIZON = NOW + HORIZON + timedelta(microseconds=1)
"""The narrowest value that must NOT be warned."""

EXPIRED_GRANT_STATUS = "expired"
"""What `expire_grants` leaves behind; a warned row must never be in it."""

LEAVER_STATUS_LITERAL = "leaver"
"""`users.status` for somebody on their way out (decision C)."""

ACTIVE_USER_STATUS = "active"

EXPIRING_KIND = "grant_expiring"
EXPIRED_KIND = "grant_expired"
SUBJECT_TYPE = "grant"

LONG_ROLE_NAME = "Fake Role " + "x" * (256 - len("Fake Role "))
"""Exactly the `roles.name` column width (256), and above `notify`'s
200-character TITLE cap. Criterion 2's whole point: this name has to reach a
stored row, which it can only do from the body."""


@dataclass(frozen=True)
class NotificationFacts:
    """What one `notifications` row says, copied out before its session closes."""

    user_id: uuid.UUID
    kind: str
    title: str
    body: str
    subject_type: str | None
    subject_id: str | None
    read_at: Any


# --- the per-case namespace ---------------------------------------------------


def _tenant(url: str) -> SeededTenant:
    """A tenant of this case's own: the per-test namespace.

    `seed_tenant` plants one grant with a NULL `expires_at` and one `active`
    user with no end date, both of which every case leaves in place: a warning
    written for the null-expiry grant would show up as an extra row in every
    single assertion below.
    """

    async def prepare() -> SeededTenant:
        engine = create_engine(url)
        try:
            return await seed_tenant(
                create_session_factory(engine), f"warn-{uuid.uuid4().hex[:12]}"
            )
        finally:
            await engine.dispose()

    return asyncio.run(prepare())


def _plant_grant(
    url: str,
    tenant: SeededTenant,
    *,
    user_id: uuid.UUID | None = None,
    role_id: uuid.UUID | None = None,
    expires_at: datetime | None = INSIDE,
    status: str = ACTIVE_GRANT_STATUS,
    revoked_at: datetime | None = None,
    revoke_reason: str | None = None,
) -> uuid.UUID:
    """One committed `grants` row, written around the sweep."""

    async def scenario() -> uuid.UUID:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant.tenant_id) as session:
                row = await Repository(session, Grant).create(
                    user_id=tenant.user_id if user_id is None else user_id,
                    role_id=tenant.role_b_id if role_id is None else role_id,
                    request_id=None,
                    granted_by_user_id=None,
                    granted_at=GRANTED_AT,
                    expires_at=expires_at,
                    revoked_at=revoked_at,
                    revoke_reason=revoke_reason,
                    status=status,
                )
                return row.id
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _plant_user(url: str, tenant: SeededTenant, *, status: str = ACTIVE_USER_STATUS) -> uuid.UUID:
    """A second, obviously fake person in the seeded department."""
    suffix = uuid.uuid4().hex[:12]

    async def scenario() -> uuid.UUID:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant.tenant_id) as session:
                row = await Repository(session, User).create(
                    department_id=tenant.department_id,
                    email=f"fake.person.{suffix}@example.test",
                    display_name=f"Fake Person {suffix}",
                    password_hash="argon2id$obviously-fake-test-hash",
                    principals=["employee"],
                    status=status,
                    start_date=date(2026, 1, 5),
                    end_date=None,
                )
                return row.id
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _plant_role(url: str, tenant: SeededTenant, *, name: str) -> uuid.UUID:
    """One extra role, so a case can choose the name a warning has to carry."""
    suffix = uuid.uuid4().hex[:12]

    async def scenario() -> uuid.UUID:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant.tenant_id) as session:
                row = await Repository(session, Role).create(
                    system_id=tenant.system_id,
                    slug=f"role-c-{suffix}",
                    name=name,
                    privilege="read",
                    default_expiry_days=30,
                )
                return row.id
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _plant_notification(
    url: str,
    tenant: SeededTenant,
    *,
    user_id: uuid.UUID,
    kind: str,
    subject_id: str,
) -> None:
    """One committed `notifications` row, planted around `notify`.

    Written through the repository rather than through `notify`, so a case can
    plant a row of a kind this job does not write (the `grant_expired` row the
    dedupe must NOT be fooled by).
    """

    async def scenario() -> None:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant.tenant_id) as session:
                await Repository(session, Notification).create(
                    user_id=user_id,
                    kind=kind,
                    title="fake planted notification",
                    body="planted by the test, not by the sweep",
                    subject_type=SUBJECT_TYPE,
                    subject_id=subject_id,
                    read_at=None,
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


# --- driving the surface ------------------------------------------------------


def _warn(url: str, tenant_id: uuid.UUID, *, now: datetime = NOW, times: int = 1) -> None:
    """`warn_expiring_grants`, once per committed transaction."""

    async def scenario() -> None:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            for _ in range(times):
                async with tenant_session(factory, tenant_id) as session:
                    await warn_expiring_grants(session, now=now)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def _notifications(url: str, tenant_id: uuid.UUID) -> list[NotificationFacts]:
    """Every notification of one tenant, whoever wrote it."""

    async def scenario() -> list[NotificationFacts]:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant_id) as session:
                rows = await Repository(session, Notification).list()
                return [
                    NotificationFacts(
                        user_id=row.user_id,
                        kind=str(row.kind),
                        title=str(row.title),
                        body=str(row.body),
                        subject_type=None if row.subject_type is None else str(row.subject_type),
                        subject_id=None if row.subject_id is None else str(row.subject_id),
                        read_at=row.read_at,
                    )
                    for row in rows
                ]
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _warnings(url: str, tenant_id: uuid.UUID) -> list[NotificationFacts]:
    """Only the rows this job claims to write."""
    return [row for row in _notifications(url, tenant_id) if row.kind == EXPIRING_KIND]


# --- the warning itself (criterion 2) -----------------------------------------


def test_a_grant_expiring_inside_the_horizon_warns_its_holder_once(
    expiry_db_url: str,  # noqa: F811
) -> None:
    """Criterion 2, as one exact row.

    Catches four wrong implementations at once. One that warns the holder AND
    their department manager — the `expire_grants` shape this module sits
    beside, refused by decision C, and the case fails on the row count. One
    that writes the kind `grant_expired`, which row 23's route renders as
    access that is already gone, telling a person their still-live access
    ended. One whose `subject_id` is the USER or the ROLE rather than the
    grant: decision B makes the subject fields the ONLY thing a renewal link
    can be composed from, so a warning naming anything else links nowhere. And
    one that stamps `read_at`, which makes the row invisible to the unread
    list that is the whole point of writing it.
    """
    tenant = _tenant(expiry_db_url)
    grant_id = _plant_grant(expiry_db_url, tenant, expires_at=INSIDE)

    _warn(expiry_db_url, tenant.tenant_id)

    rows = _notifications(expiry_db_url, tenant.tenant_id)
    assert [row.user_id for row in rows] == [tenant.user_id]
    assert rows[0].kind == EXPIRING_KIND
    assert rows[0].kind == NotificationKind.GRANT_EXPIRING.value
    assert rows[0].subject_type == SUBJECT_TYPE
    assert rows[0].subject_id == str(grant_id)
    assert rows[0].read_at is None


def test_the_role_name_is_in_the_body_and_the_title_is_the_fixed_literal(
    expiry_db_url: str,  # noqa: F811
) -> None:
    """Criterion 2's second half, at the length that makes it observable.

    Catches a warning that composes the role name into the TITLE. `notify`
    refuses a title over 200 characters with a typed `VALIDATION` error before
    any row is written, and `roles.name` is a 256-character column — so the
    day a tenant names a role longer than 200 characters, that implementation
    stops warning about it at all, and the daily job raises to the runner
    instead of writing anything. The name is planted at exactly the column
    width, above the cap, so the title-composing implementation cannot store
    this row and the body-composing one can.

    The title is asserted as the exact `_WARNING_TITLE` literal rather than
    "some fixed string", so a title that varies per grant — the other way the
    cap gets reached — fails here too.
    """
    tenant = _tenant(expiry_db_url)
    role_id = _plant_role(expiry_db_url, tenant, name=LONG_ROLE_NAME)
    _plant_grant(expiry_db_url, tenant, role_id=role_id, expires_at=INSIDE)
    assert len(LONG_ROLE_NAME) == 256, "premise: the name fills the roles.name column"
    assert len(LONG_ROLE_NAME) > NOTIFICATION_TITLE_MAX_LENGTH, (
        "premise: this name cannot fit in a notification title"
    )

    _warn(expiry_db_url, tenant.tenant_id)

    rows = _warnings(expiry_db_url, tenant.tenant_id)
    assert len(rows) == 1
    assert rows[0].title == _WARNING_TITLE
    assert LONG_ROLE_NAME not in rows[0].title
    assert LONG_ROLE_NAME in rows[0].body
    assert len(rows[0].title) <= NOTIFICATION_TITLE_MAX_LENGTH


# --- the band, both boundaries exactly (criterion 3, decision A) ---------------


def test_a_grant_expiring_at_exactly_the_horizon_is_warned(
    expiry_db_url: str,  # noqa: F811
) -> None:
    """The inclusive half of decision A's top boundary, at the exact instant.

    Catches a strict `<` on the upper bound, which loses the grant that lands
    precisely on the horizon — and, because the daily tick moves by 24 hours,
    every such grant is warned a day later than the horizon promises, or not
    at all if the tick drifts (decision H). The lower bound cannot save this
    row: it is inside the band on that side either way.
    """
    tenant = _tenant(expiry_db_url)
    grant_id = _plant_grant(expiry_db_url, tenant, expires_at=AT_HORIZON)

    _warn(expiry_db_url, tenant.tenant_id)

    rows = _warnings(expiry_db_url, tenant.tenant_id)
    assert [row.subject_id for row in rows] == [str(grant_id)]


def test_a_grant_one_microsecond_past_the_horizon_is_not_warned(
    expiry_db_url: str,  # noqa: F811
) -> None:
    """The exclusive half of the same boundary, one microsecond out.

    Catches a horizon widened past 14 days — a `<=` on a rounded date, a
    comparison against `now.date()`, or the constant quietly changed. A
    warning that arrives weeks early trains people to ignore the bell, which
    costs exactly the renewals this job exists to prompt. Only the upper bound
    can refuse this row: it is above `now`, so the lower bound admits it.
    """
    tenant = _tenant(expiry_db_url)
    _plant_grant(expiry_db_url, tenant, expires_at=JUST_PAST_HORIZON)

    _warn(expiry_db_url, tenant.tenant_id)

    assert _notifications(expiry_db_url, tenant.tenant_id) == []


def test_a_grant_expiring_at_exactly_now_is_not_warned(
    expiry_db_url: str,  # noqa: F811
) -> None:
    """Criterion 3's third row: the STRICT lower bound.

    Catches a band written `expires_at >= now`, which overlaps `expire_grants`
    exactly — 21a decision D expires `expires_at <= now`, so the same grant
    would be expired and warned about in one tick, and the holder's bell would
    carry "your access ended" and "your access is about to end" for one id.
    Only the lower bound can refuse this row; it is well under the horizon.
    """
    tenant = _tenant(expiry_db_url)
    _plant_grant(expiry_db_url, tenant, expires_at=NOW)

    _warn(expiry_db_url, tenant.tenant_id)

    assert _notifications(expiry_db_url, tenant.tenant_id) == []


# --- the dedupe (criterion 4, decision A) -------------------------------------


def test_a_second_run_over_the_same_grant_writes_no_second_notification(
    expiry_db_url: str,  # noqa: F811
) -> None:
    """Criterion 4. Catches the band with no dedupe at all.

    Decision A took the wide band precisely so that a warning is owed until it
    has been GIVEN; the price is that the same grant is in the band on every
    daily tick for a fortnight. Without the anti-join that is fourteen
    identical rows in one person's bell for one grant, which is how a
    notification surface stops being read. Two runs over one grant, one row.
    """
    tenant = _tenant(expiry_db_url)
    grant_id = _plant_grant(expiry_db_url, tenant, expires_at=INSIDE)

    _warn(expiry_db_url, tenant.tenant_id, times=2)

    rows = _warnings(expiry_db_url, tenant.tenant_id)
    assert [row.subject_id for row in rows] == [str(grant_id)]


def test_a_grant_already_carrying_an_expired_notification_is_still_warned(
    expiry_db_url: str,  # noqa: F811
) -> None:
    """Criterion 7: the dedupe is on kind AND subject, never subject alone.

    Catches an anti-join keyed on `subject_id` only. `expire_grants` writes a
    `grant_expired` row against the very same grant id, and a renewed grant
    keeps its id — so a subject-only dedupe silently stops warning about every
    grant that has ever been expired once, which is the population most likely
    to lapse again. The planted row is the shape that sweep leaves behind, and
    the case asserts the warning appears BESIDE it.
    """
    tenant = _tenant(expiry_db_url)
    grant_id = _plant_grant(expiry_db_url, tenant, expires_at=INSIDE)
    _plant_notification(
        expiry_db_url,
        tenant,
        user_id=tenant.user_id,
        kind=EXPIRED_KIND,
        subject_id=str(grant_id),
    )

    _warn(expiry_db_url, tenant.tenant_id)

    rows = _notifications(expiry_db_url, tenant.tenant_id)
    assert sorted(row.kind for row in rows) == [EXPIRED_KIND, EXPIRING_KIND]
    warned = [row for row in rows if row.kind == EXPIRING_KIND]
    assert [row.subject_id for row in warned] == [str(grant_id)]


# --- the excluded rows, one case per conjunct (criterion 5) -------------------


def test_a_grant_that_never_expires_is_not_warned(
    expiry_db_url: str,  # noqa: F811
) -> None:
    """The `expires_at IS NOT NULL` conjunct, alone.

    Postgres compares NULL to a bound as unknown, so this only bites an
    implementation that coalesces the column or filters the set in Python with
    a default date — and that one warns every permanent grant in the tenant,
    every single day, about an expiry that does not exist. Nothing else in the
    filter refuses this row: it is `active`, unrevoked and held by an active
    user.
    """
    tenant = _tenant(expiry_db_url)
    _plant_grant(expiry_db_url, tenant, expires_at=None)

    _warn(expiry_db_url, tenant.tenant_id)

    assert _notifications(expiry_db_url, tenant.tenant_id) == []


def test_a_grant_stamped_revoked_at_inside_the_horizon_is_not_warned(
    expiry_db_url: str,  # noqa: F811
) -> None:
    """The `revoked_at IS NULL` conjunct, ISOLATED from the status conjunct.

    The row is planted `active` WITH a `revoked_at`, which is deliberately not
    the shape `revoke_role_access` leaves (it writes both the status and the
    stamp): a row carrying both would be refused by either conjunct, so it
    would pin neither. This one is refused only by `revoked_at IS NULL`, so
    dropping that conjunct fails exactly this case — and the wrong
    implementation it describes tells somebody to renew access that has
    already been taken off them.
    """
    tenant = _tenant(expiry_db_url)
    _plant_grant(
        expiry_db_url,
        tenant,
        expires_at=INSIDE,
        status=ACTIVE_GRANT_STATUS,
        revoked_at=GRANTED_AT,
        revoke_reason="fake revocation planted to isolate the revoked_at conjunct",
    )

    _warn(expiry_db_url, tenant.tenant_id)

    assert _notifications(expiry_db_url, tenant.tenant_id) == []


def test_an_already_expired_grant_inside_the_horizon_is_not_warned(
    expiry_db_url: str,  # noqa: F811
) -> None:
    """The `status = 'active'` conjunct, ISOLATED from the `revoked_at` one.

    The row is `expired` with a NULL `revoked_at` — exactly what
    `expire_grants` leaves behind — so only the status test can refuse it, and
    dropping that conjunct fails this case and no other. The implementation it
    catches warns about access that ended already, on a row whose expiry is
    still ahead of `now` only because somebody re-dated it; a person told to
    renew what they no longer hold files a ticket, not a request.
    """
    tenant = _tenant(expiry_db_url)
    _plant_grant(
        expiry_db_url,
        tenant,
        expires_at=INSIDE,
        status=EXPIRED_GRANT_STATUS,
        revoked_at=None,
    )

    _warn(expiry_db_url, tenant.tenant_id)

    assert _notifications(expiry_db_url, tenant.tenant_id) == []


def test_another_tenants_grant_inside_the_horizon_is_not_warned(
    expiry_db_url: str,  # noqa: F811
) -> None:
    """Catches a job that builds its own statement over `grants` instead of
    going through the tenant-bound session.

    The runner calls this once per tenant, so a query that ignores the binding
    warns every tenant's holders on the first tenant's tick and writes each
    row under the wrong `tenant_id` — a notification a stranger's bell shows,
    naming a grant id from another company. The neighbour's grant sits at the
    same instant inside the band, so only the scoping can save it.
    """
    tenant = _tenant(expiry_db_url)
    neighbour = _tenant(expiry_db_url)
    mine = _plant_grant(expiry_db_url, tenant, expires_at=INSIDE)
    _plant_grant(expiry_db_url, neighbour, expires_at=INSIDE)

    _warn(expiry_db_url, tenant.tenant_id)

    assert [row.subject_id for row in _warnings(expiry_db_url, tenant.tenant_id)] == [str(mine)]
    assert _notifications(expiry_db_url, neighbour.tenant_id) == []


def test_a_grant_held_by_a_leaver_is_not_warned(
    expiry_db_url: str,  # noqa: F811
) -> None:
    """Decision C's exclusion, which needs the join to `users`.

    Catches a band that reads `grants` alone. The other daily job in this same
    plan is about to revoke every one of this person's grants wholesale, so a
    warning telling them to request a renewal is the system contradicting
    itself inside one tick — and it is the only row here that a filter written
    purely over `grants` cannot refuse: the grant is `active`, unrevoked, and
    squarely inside the band.
    """
    tenant = _tenant(expiry_db_url)
    leaver = _plant_user(expiry_db_url, tenant, status=LEAVER_STATUS_LITERAL)
    _plant_grant(expiry_db_url, tenant, user_id=leaver, expires_at=INSIDE)

    _warn(expiry_db_url, tenant.tenant_id)

    assert _notifications(expiry_db_url, tenant.tenant_id) == []


def test_two_holders_in_the_band_are_each_warned_on_their_own_grant(
    expiry_db_url: str,  # noqa: F811
) -> None:
    """Catches a job that writes ONE row per sweep rather than one per grant,
    and one that sends every warning to the first holder it read.

    Every read of `notifications` is "what is unread for THIS user", so a
    single row carrying two recipients answers no query the bell makes, and a
    row addressed to the wrong holder tells one person about another's access.
    Two holders, two roles, two different expiry instants inside the band.
    """
    tenant = _tenant(expiry_db_url)
    other = _plant_user(expiry_db_url, tenant)
    mine = _plant_grant(expiry_db_url, tenant, expires_at=INSIDE, role_id=tenant.role_b_id)
    theirs = _plant_grant(
        expiry_db_url,
        tenant,
        user_id=other,
        role_id=tenant.role_id,
        expires_at=AT_HORIZON,
    )

    _warn(expiry_db_url, tenant.tenant_id)

    rows = _warnings(expiry_db_url, tenant.tenant_id)
    assert {(row.user_id, row.subject_id) for row in rows} == {
        (tenant.user_id, str(mine)),
        (other, str(theirs)),
    }


# --- the constants ------------------------------------------


def test_the_expiry_warning_constants_are_exactly_the_planned_values() -> None:
    """a project rule on everything this half of the plan introduces.

    Catches a horizon quietly moved — every boundary case above is written
    against 14 days, so a module constant of 7 or 30 would leave them all
    green while the service warned on a different schedule from the one §1
    fixes. Catches the enum member's value drifting from the literal row 23's
    route renders and the anti-join matches on, which would make the dedupe
    match nothing and the bell render an unknown kind. The export is part of
    the surface: `scheduler.jobs` imports the engine the way every other
    caller imports from `helpdesk.access`.
    """
    assert EXPIRY_WARNING_HORIZON_DAYS == 14
    assert timedelta(days=EXPIRY_WARNING_HORIZON_DAYS) == HORIZON
    assert NotificationKind.GRANT_EXPIRING.value == "grant_expiring"
    assert access_package.warn_expiring_grants is warn_expiring_grants
