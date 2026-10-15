"""An earlier change §6: `send_review_reminders`, called directly with a fixed `now`.

Written before the implementation exists. Expected surface (the implementer
must provide exactly this):

``helpdesk.models.notification``
    ``NotificationKind.REVIEW_OVERDUE = "review_overdue"`` and
    ``NotificationKind.REVIEW_ESCALATED = "review_escalated"``, the third and
    fourth members of the enum after this plan (§8 widens
    `test_notification_models.py`'s set to exactly four).

``helpdesk.access.review_schedule`` (re-exported from ``helpdesk.access``)
    ``REVIEW_ESCALATION_DAYS = 14`` — how far past `due_at` an open review has
    to be before the `it_admin` principals hear about it (decision J,
    INCLUSIVE).
    ``_REMINDER_TITLE`` — the fixed title every reminder carries. The scope
    name goes in the BODY, so a 256-character department name can never reach
    `notify`'s 200-character title cap.
    ``async def send_review_reminders(session, *, now)`` — over the BOUND
    tenant only, one `review_overdue` notification to the REVIEWER of every
    review whose `status` is `open`, whose `due_at` is not null and whose
    `due_at < now`, with `subject_type` `access_review`, `subject_id` the
    review id and `read_at` null. A review at or past
    `now - REVIEW_ESCALATION_DAYS` ALSO writes one `review_escalated` row to
    every `active` user of the tenant carrying the `it_admin` principal
    (decision F: as well as the reminder, never instead of it).

The return value is never asserted here: plan §6 fixes no return shape, and
every claim below is readable from the `notifications` table.

Decision E is deliberate and is asserted as such: there is no dedupe at all,
because the daily cadence gate IS the dedupe and the design asks for a DAILY
reminder. `test_running_the_job_twice_writes_two_reminders_for_one_review`
states that as the intended behaviour rather than leaving it to be discovered
as a bug.

a project rule: the session factory is the shared one from `conftest.py` --
one engine for the whole run, schema created once. Per-test isolation is a
namespace, not a rebuild: this job sweeps a whole tenant, so every case seeds
a tenant of its OWN with `_seed.seed_tenant`. That seeded tenant holds no
review at all and its only user carries `["employee"]` — read off `_seed.py`,
not remembered — so a case that plants no `it_admin` has none, and every "no
rows" assertion below stays exact.

a project rule: this file, `test_access_review_generation.py` and
`test_access_reviews.py` are three distinct basenames.

No pytest-asyncio: each case is a sync function driving one `asyncio.run`.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from helpdesk.shared.access import review_schedule as review_schedule_module
from helpdesk.shared.access.review_schedule import (
    _REMINDER_TITLE,
    REVIEW_ESCALATION_DAYS,
    send_review_reminders,
)
from helpdesk.shared.core.db import tenant_session
from helpdesk.shared.crud import Repository
from helpdesk.shared.models import AccessReview, Department, Notification, ReviewStatus, User
from helpdesk.shared.models.notification import NOTIFICATION_TITLE_MAX_LENGTH, NotificationKind
from tests.support.seed import SeededTenant, seed_tenant

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
"""The one instant every reminder run answers for. Passed in, never read from
a clock."""

PERIOD = "2026-Q3"
"""`quarter_period(NOW)`, written out: this file never calls the generator, so
the period is only a label on the rows it plants."""

ESCALATION = timedelta(days=14)
"""Decision J's band, written as a literal here on purpose.

Asserted equal to `REVIEW_ESCALATION_DAYS` by the constants case, so a value
changed in the module moves the boundary cases only through a test that fails
first."""

OVERDUE = NOW - timedelta(days=1)
"""Comfortably overdue and comfortably short of the escalation band."""
JUST_OVERDUE = NOW - timedelta(microseconds=1)
"""The narrowest value that IS overdue against a strict `<`."""
AT_ESCALATION = NOW - ESCALATION
"""The inclusive edge of decision J's escalation band."""
JUST_INSIDE_ESCALATION = NOW - ESCALATION + timedelta(microseconds=1)
"""The narrowest value that must NOT escalate."""
NOT_YET_DUE = NOW + timedelta(days=3)

OVERDUE_KIND = "review_overdue"
ESCALATED_KIND = "review_escalated"
SUBJECT_TYPE = "access_review"

IT_ADMIN = "it_admin"
EMPLOYEE = "employee"
ACTIVE_USER_STATUS = "active"
LEAVER_STATUS_LITERAL = "leaver"

TENURED_START = date(2019, 3, 7)

LONG_DEPARTMENT_NAME = "Fake Department " + "x" * (256 - len("Fake Department "))
"""Exactly the `departments.name` column width (256), and above `notify`'s
200-character TITLE cap. Criterion 13's whole point: this name has to reach a
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


def _in_tenant[T](
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    work: Callable[[AsyncSession], Awaitable[T]],
) -> T:
    """Run `work` in one committed, tenant-bound transaction of its own."""

    async def scenario() -> T:
        async with tenant_session(factory, tenant_id) as session:
            return await work(session)

    return asyncio.run(scenario())


def _tenant(factory: async_sessionmaker[AsyncSession]) -> SeededTenant:
    """A tenant of this case's own: the per-test namespace.

    `seed_tenant` plants no review and no notification, and its single user
    carries `["employee"]` only — so a tenant starts with nothing overdue and
    nobody to escalate to, and every count below is the case's own doing.
    """

    async def prepare() -> SeededTenant:
        return await seed_tenant(factory, f"revrem-{uuid.uuid4().hex[:12]}")

    return asyncio.run(prepare())


def _plant_user(
    factory: async_sessionmaker[AsyncSession],
    tenant: SeededTenant,
    *,
    principals: list[str] | None = None,
    status: str = ACTIVE_USER_STATUS,
) -> uuid.UUID:
    """A second, obviously fake person in the seeded department."""
    suffix = uuid.uuid4().hex[:12]

    async def work(session: AsyncSession) -> uuid.UUID:
        row = await Repository(session, User).create(
            department_id=tenant.department_id,
            email=f"fake.person.{suffix}@example.test",
            display_name=f"Fake Person {suffix}",
            password_hash="argon2id$obviously-fake-test-hash",
            principals=[EMPLOYEE] if principals is None else principals,
            status=status,
            start_date=TENURED_START,
            end_date=None,
        )
        return row.id

    return _in_tenant(factory, tenant.tenant_id, work)


def _plant_department(
    factory: async_sessionmaker[AsyncSession], tenant: SeededTenant, *, name: str
) -> uuid.UUID:
    """One extra department, so a case can choose the name a reminder carries."""

    async def work(session: AsyncSession) -> uuid.UUID:
        row = await Repository(session, Department).create(name=name, manager_user_id=None)
        return row.id

    return _in_tenant(factory, tenant.tenant_id, work)


def _plant_review(
    factory: async_sessionmaker[AsyncSession],
    tenant: SeededTenant,
    *,
    reviewer_user_id: uuid.UUID | None = None,
    department_id: uuid.UUID | None = None,
    due_at: datetime | None = OVERDUE,
    status: str = ReviewStatus.OPEN.value,
) -> uuid.UUID:
    """One committed `access_reviews` row, planted around `open_review`.

    Written through the repository rather than through the engine, so a case
    can choose the exact `due_at` and status the band is about; the review's
    items are irrelevant to a reminder, which reads the review row alone.
    """

    async def work(session: AsyncSession) -> uuid.UUID:
        row = await Repository(session, AccessReview).create(
            reviewer_user_id=tenant.user_id if reviewer_user_id is None else reviewer_user_id,
            department_id=tenant.department_id if department_id is None else department_id,
            system_id=None,
            period=PERIOD,
            status=status,
            due_at=due_at,
            completed_at=NOW if status == ReviewStatus.COMPLETED.value else None,
        )
        return row.id

    return _in_tenant(factory, tenant.tenant_id, work)


# --- driving the surface ------------------------------------------------------


def _remind(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    *,
    now: datetime = NOW,
    times: int = 1,
) -> None:
    """`send_review_reminders`, once per committed transaction."""

    async def scenario() -> None:
        for _ in range(times):
            async with tenant_session(factory, tenant_id) as session:
                await send_review_reminders(session, now=now)

    asyncio.run(scenario())


def _notifications(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> list[NotificationFacts]:
    """Every notification of one tenant, whoever wrote it."""

    async def work(session: AsyncSession) -> list[NotificationFacts]:
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

    return _in_tenant(factory, tenant_id, work)


def _of_kind(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, kind: str
) -> list[NotificationFacts]:
    return [row for row in _notifications(factory, tenant_id) if row.kind == kind]


# --- the reminder itself (criterion 13) ---------------------------------------


def test_an_overdue_open_review_reminds_its_reviewer_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Criterion 13, as one exact row.

    Catches four wrong implementations at once. One that writes to the
    department's members or to every `it_admin` rather than to the REVIEWER —
    the only person who can act on the review, and the person the design names.
    One that writes the kind `review_escalated` for an ordinary overdue
    review, which would make row 23's bell shout about every review a day
    late. One whose `subject_id` is the department, the period or the reviewer
    rather than the review: those two fields are the only thing a link to the
    review queue can be composed from, so a reminder naming anything else
    links nowhere. And one that stamps `read_at`, which makes the row
    invisible to the unread list that is the whole point of writing it.
    """
    tenant = _tenant(session_factory)
    reviewer = _plant_user(session_factory, tenant)
    review_id = _plant_review(session_factory, tenant, reviewer_user_id=reviewer, due_at=OVERDUE)

    _remind(session_factory, tenant.tenant_id)

    rows = _notifications(session_factory, tenant.tenant_id)
    assert [row.user_id for row in rows] == [reviewer]
    assert rows[0].kind == OVERDUE_KIND
    assert rows[0].kind == NotificationKind.REVIEW_OVERDUE.value
    assert rows[0].subject_type == SUBJECT_TYPE
    assert rows[0].subject_id == str(review_id)
    assert rows[0].read_at is None


def test_the_scope_name_is_in_the_body_and_the_title_is_the_fixed_literal(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Criterion 13's second half, at the length that makes it observable.

    Catches a reminder that composes the department name into the TITLE.
    `notify` refuses a title over 200 characters with a typed `VALIDATION`
    error before any row is written, and `departments.name` is a
    256-character column — so the day a tenant names a department longer than
    200 characters, that implementation stops reminding about its reviews at
    all and the daily job raises to the runner instead of writing anything.
    The name is planted at exactly the column width, above the cap, so the
    title-composing implementation cannot store this row and the
    body-composing one can.

    The title is asserted as the exact `_REMINDER_TITLE` literal rather than
    "some fixed string", so a title that varies per review — the other way the
    cap gets reached — fails here too.
    """
    tenant = _tenant(session_factory)
    reviewer = _plant_user(session_factory, tenant)
    department_id = _plant_department(session_factory, tenant, name=LONG_DEPARTMENT_NAME)
    _plant_review(
        session_factory,
        tenant,
        reviewer_user_id=reviewer,
        department_id=department_id,
        due_at=OVERDUE,
    )
    assert len(LONG_DEPARTMENT_NAME) == 256, "premise: the name fills the departments.name column"
    assert len(LONG_DEPARTMENT_NAME) > NOTIFICATION_TITLE_MAX_LENGTH, (
        "premise: this name cannot fit in a notification title"
    )

    _remind(session_factory, tenant.tenant_id)

    rows = _of_kind(session_factory, tenant.tenant_id, OVERDUE_KIND)
    assert len(rows) == 1
    assert rows[0].title == _REMINDER_TITLE
    assert LONG_DEPARTMENT_NAME not in rows[0].title
    assert LONG_DEPARTMENT_NAME in rows[0].body
    assert len(rows[0].title) <= NOTIFICATION_TITLE_MAX_LENGTH


# --- the overdue boundary, both halves exactly (criterion 14) -----------------


def test_a_review_due_at_exactly_now_is_not_overdue(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Criterion 14's strict half, at the exact instant.

    Catches `due_at <= now`, or a comparison rounded to a date
    (`due_at::date <= now::date`), either of which nags a reviewer on the
    morning the review is due — before they are late at all. A review due
    exactly now is DUE, not overdue, and a reminder that arrives on time
    trains people to read the bell as noise. Only the lower comparison can
    refuse this row: the review is `open`, has a `due_at` and belongs to the
    swept tenant.
    """
    tenant = _tenant(session_factory)
    _plant_review(session_factory, tenant, due_at=NOW)

    _remind(session_factory, tenant.tenant_id)

    assert _notifications(session_factory, tenant.tenant_id) == []


def test_a_review_one_microsecond_past_its_due_date_is_overdue(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Criterion 14's other half, one microsecond out.

    Catches a band widened to "a day late" — `due_at < now - 1 day`, or a
    comparison against `now.date()` — which delays every reminder by a full
    daily tick and, on the escalation side, moves the fourteen-day promise to
    fifteen. Nothing else in the filter can refuse this row, so it fails only
    when the comparison itself is wrong.
    """
    tenant = _tenant(session_factory)
    review_id = _plant_review(session_factory, tenant, due_at=JUST_OVERDUE)

    _remind(session_factory, tenant.tenant_id)

    rows = _of_kind(session_factory, tenant.tenant_id, OVERDUE_KIND)
    assert [row.subject_id for row in rows] == [str(review_id)]


# --- the excluded rows, one case per conjunct (criterion 15) ------------------


def test_a_review_with_no_due_date_is_never_reminded_about(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The `due_at IS NOT NULL` conjunct, alone.

    `open_review`'s `due_at` defaults to null and the MCP tool may leave it
    so, so this population is real. Postgres compares NULL to a bound as
    unknown, so the conjunct only bites an implementation that coalesces the
    column or filters in Python with a default date — and that one reminds
    about every review that was never given a deadline, daily, for ever.
    Nothing else here refuses this row: it is `open` and in the swept tenant.
    """
    tenant = _tenant(session_factory)
    _plant_review(session_factory, tenant, due_at=None)

    _remind(session_factory, tenant.tenant_id)

    assert _notifications(session_factory, tenant.tenant_id) == []


def test_a_completed_review_long_past_its_due_date_is_never_reminded_about(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The `status = 'open'` conjunct, ISOLATED from the other two.

    The row carries a real `due_at` deep inside the escalation band, so only
    the status test can refuse it and dropping that conjunct fails this case
    and no other. What it catches is a job that nags reviewers — and, at this
    `due_at`, every `it_admin` in the tenant — about work that was FINISHED,
    every day, for every review the tenant has ever completed. That is the
    failure mode that gets a notification surface switched off.
    """
    tenant = _tenant(session_factory)
    _plant_review(
        session_factory,
        tenant,
        due_at=NOW - timedelta(days=60),
        status=ReviewStatus.COMPLETED.value,
    )

    _remind(session_factory, tenant.tenant_id)

    assert _notifications(session_factory, tenant.tenant_id) == []


def test_another_tenants_overdue_review_is_never_reminded_about(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Catches a job that builds its own statement over `access_reviews`
    instead of going through the tenant-bound session.

    The runner calls this once per tenant, so a query that ignores the binding
    reminds every tenant's reviewers on the first tenant's tick and writes
    each row under the wrong `tenant_id` — a notification a stranger's bell
    shows, naming a review id from another company. The neighbour's review is
    overdue by exactly the same amount, so only the scoping can save it.
    """
    tenant = _tenant(session_factory)
    neighbour = _tenant(session_factory)
    mine = _plant_review(session_factory, tenant, due_at=OVERDUE)
    _plant_review(session_factory, neighbour, due_at=OVERDUE)

    _remind(session_factory, tenant.tenant_id)

    rows = _of_kind(session_factory, tenant.tenant_id, OVERDUE_KIND)
    assert [row.subject_id for row in rows] == [str(mine)]
    assert _notifications(session_factory, neighbour.tenant_id) == []


# --- the escalation band, both halves exactly (criteria 16 and 17) ------------


def test_a_review_exactly_fourteen_days_overdue_escalates_to_every_active_it_admin(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Decision J's INCLUSIVE edge, at the exact instant, plus criterion 16's
    recipient set.

    Catches a strict `<` on the escalation bound, which loses the review that
    lands precisely on day fourteen — and because the tick moves by 24 hours,
    every such review escalates a day late or, if the tick drifts, not at all.
    "14 days overdue" reads as "at least 14 days", and the inclusive side is
    the one that fires on the day the runbook says it will. Also catches an
    escalation sent to ONE `it_admin` (the first read) rather than to every
    active holder of the principal: two are planted, and a plain `employee` is
    planted beside them so "everybody in the tenant" fails as well.
    """
    tenant = _tenant(session_factory)
    reviewer = _plant_user(session_factory, tenant)
    admin_one = _plant_user(session_factory, tenant, principals=[IT_ADMIN])
    admin_two = _plant_user(session_factory, tenant, principals=[EMPLOYEE, IT_ADMIN])
    _plant_user(session_factory, tenant, principals=[EMPLOYEE])
    review_id = _plant_review(
        session_factory, tenant, reviewer_user_id=reviewer, due_at=AT_ESCALATION
    )

    _remind(session_factory, tenant.tenant_id)

    escalated = _of_kind(session_factory, tenant.tenant_id, ESCALATED_KIND)
    assert {row.user_id for row in escalated} == {admin_one, admin_two}
    assert {row.subject_id for row in escalated} == {str(review_id)}
    assert {row.subject_type for row in escalated} == {SUBJECT_TYPE}


def test_a_review_one_microsecond_short_of_fourteen_days_does_not_escalate(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The exclusive half of the same boundary, one microsecond in.

    Catches an escalation band quietly widened — a `>=` on a rounded date, a
    comparison against `now.date()`, or the constant changed to 7 — which
    escalates a review a day (or a week) before the design says the reviewer has
    run out of rope, putting every `it_admin` in the tenant on a mailing list
    for reviews that are merely late. The reviewer's own reminder is asserted
    present, so an implementation that simply stopped writing anything for
    this row fails here too.
    """
    tenant = _tenant(session_factory)
    reviewer = _plant_user(session_factory, tenant)
    _plant_user(session_factory, tenant, principals=[IT_ADMIN])
    review_id = _plant_review(
        session_factory, tenant, reviewer_user_id=reviewer, due_at=JUST_INSIDE_ESCALATION
    )

    _remind(session_factory, tenant.tenant_id)

    assert _of_kind(session_factory, tenant.tenant_id, ESCALATED_KIND) == []
    overdue = _of_kind(session_factory, tenant.tenant_id, OVERDUE_KIND)
    assert [(row.user_id, row.subject_id) for row in overdue] == [(reviewer, str(review_id))]


def test_an_escalating_review_still_writes_the_reviewers_own_reminder(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Decision F: the escalation is an ADDITION, never a substitution.

    Catches the rejected alternative, an `if escalating: … else: …` that sends
    the `it_admin` row INSTEAD of the reviewer's. the design reads "reminder to
    reviewer; 14 days overdue → escalate to it_admin", and the reviewer who
    stops being told on day fourteen is the one person who can still decide
    the items. Two kinds, one review, and the recipients are asserted apart:
    the reviewer gets exactly the `review_overdue` row and the admin exactly
    the `review_escalated` one, so a job that sent both kinds to both people
    fails as well.
    """
    tenant = _tenant(session_factory)
    reviewer = _plant_user(session_factory, tenant)
    admin = _plant_user(session_factory, tenant, principals=[IT_ADMIN])
    review_id = _plant_review(
        session_factory, tenant, reviewer_user_id=reviewer, due_at=AT_ESCALATION - timedelta(days=3)
    )

    _remind(session_factory, tenant.tenant_id)

    rows = _notifications(session_factory, tenant.tenant_id)
    assert {(row.user_id, row.kind, row.subject_id) for row in rows} == {
        (reviewer, OVERDUE_KIND, str(review_id)),
        (admin, ESCALATED_KIND, str(review_id)),
    }


def test_a_tenant_with_no_active_it_admin_escalates_to_nobody_and_still_reminds(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Criterion 17's first half.

    Catches an escalation that falls back to somebody when the principal is
    unheld — every user of the tenant, the reviewer twice, or a crash on an
    empty list that would take the whole tenant's reminders down with it. A
    small tenant with no `it_admin` at all is an ordinary state, not an
    error, and the reviewer must still be reminded: the `review_overdue` row
    is asserted present beside the empty escalation set, so "wrote nothing"
    does not pass.
    """
    tenant = _tenant(session_factory)
    reviewer = _plant_user(session_factory, tenant)
    review_id = _plant_review(
        session_factory, tenant, reviewer_user_id=reviewer, due_at=AT_ESCALATION
    )

    _remind(session_factory, tenant.tenant_id)

    rows = _notifications(session_factory, tenant.tenant_id)
    assert [(row.user_id, row.kind, row.subject_id) for row in rows] == [
        (reviewer, OVERDUE_KIND, str(review_id))
    ]


def test_a_leaver_holding_it_admin_is_never_escalated_to(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Criterion 17's second half, ISOLATED from the principal test.

    Both planted admins carry `it_admin`, so only the `status = 'active'`
    conjunct can tell them apart and dropping it fails exactly this case. What
    it catches is a tenant's departed IT lead collecting escalations about
    reviews they can no longer act on — the design blocks a leaver from asking
    for anything at all, and 21b's sweep is busy revoking their access in the
    same tick, so a notification to them is the system contradicting itself.
    The active admin's row is asserted present, so a query that escalated to
    nobody fails too.
    """
    tenant = _tenant(session_factory)
    reviewer = _plant_user(session_factory, tenant)
    active_admin = _plant_user(session_factory, tenant, principals=[IT_ADMIN])
    _plant_user(session_factory, tenant, principals=[IT_ADMIN], status=LEAVER_STATUS_LITERAL)
    _plant_review(session_factory, tenant, reviewer_user_id=reviewer, due_at=AT_ESCALATION)

    _remind(session_factory, tenant.tenant_id)

    escalated = _of_kind(session_factory, tenant.tenant_id, ESCALATED_KIND)
    assert [row.user_id for row in escalated] == [active_admin]


# --- one row per review, and the deliberate absence of a dedupe --------------


def test_two_overdue_reviews_for_one_reviewer_write_one_row_each(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Criterion 18. Catches a job that writes one row per REVIEWER rather
    than one per review — the natural shape if the overdue set is grouped by
    recipient to save a write.

    `subject_id` is the only field a link to the review queue can be built
    from, so one row covering two reviews names at most one of them and the
    reviewer is never told about the other. The two reviews are over different
    departments and have different `due_at` values, so a merged row cannot
    accidentally be right.
    """
    tenant = _tenant(session_factory)
    reviewer = _plant_user(session_factory, tenant)
    other_department = _plant_department(
        session_factory, tenant, name=f"Fake Second Dept {uuid.uuid4().hex[:12]}"
    )
    first = _plant_review(session_factory, tenant, reviewer_user_id=reviewer, due_at=OVERDUE)
    second = _plant_review(
        session_factory,
        tenant,
        reviewer_user_id=reviewer,
        department_id=other_department,
        due_at=JUST_OVERDUE,
    )

    _remind(session_factory, tenant.tenant_id)

    rows = _of_kind(session_factory, tenant.tenant_id, OVERDUE_KIND)
    assert {row.user_id for row in rows} == {reviewer}
    assert {row.subject_id for row in rows} == {str(first), str(second)}
    assert len(rows) == 2


def test_running_the_job_twice_writes_two_reminders_for_one_review(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Decision E, stated as the intended behaviour rather than discovered as a
    bug.

    the design asks for a DAILY reminder and the cadence gate already makes one
    run a day, so the anti-join `warn_expiring_grants` uses would turn that
    daily nag into a single notification never repeated — a review left open
    for a quarter would be mentioned once, on the day after it fell due. This
    case catches that dedupe being copied across from the expiry warning
    because the two jobs look alike: two runs, two rows, one review.

    It is the one case here that would go RED for a change somebody might
    think is an improvement, which is exactly why decision E is written down.
    """
    tenant = _tenant(session_factory)
    reviewer = _plant_user(session_factory, tenant)
    review_id = _plant_review(session_factory, tenant, reviewer_user_id=reviewer, due_at=OVERDUE)

    _remind(session_factory, tenant.tenant_id, times=2)

    rows = _of_kind(session_factory, tenant.tenant_id, OVERDUE_KIND)
    assert [row.subject_id for row in rows] == [str(review_id), str(review_id)]
    assert {row.user_id for row in rows} == {reviewer}


def test_a_review_that_is_not_yet_due_writes_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The ordinary case the whole band exists to leave alone.

    Catches a filter dropped altogether — a job that reminds about every open
    review in the tenant on every tick, which is indistinguishable from the
    correct implementation in any case that plants only overdue rows. The
    review here is open, has a `due_at` and belongs to the swept tenant; only
    the comparison against `now` can refuse it.
    """
    tenant = _tenant(session_factory)
    _plant_review(session_factory, tenant, due_at=NOT_YET_DUE)

    _remind(session_factory, tenant.tenant_id)

    assert _notifications(session_factory, tenant.tenant_id) == []


# --- the constants ------------------------------------------


def test_the_review_reminder_constants_are_exactly_the_planned_values() -> None:
    """a project rule on everything this half of the plan introduces.

    Catches the escalation window quietly moved: every boundary case above is
    written against 14 days, so a module constant of 7 or 30 would leave them
    all green while the service escalated on a different schedule from the one
    §1 fixes. Catches either enum member's value drifting from the literal row
    23's route will render — a stored `kind` nothing renders is a bell entry a
    person sees as blank — and pins the enum's two new members as the values
    `send_review_reminders` writes.

    The export is part of the surface: `scheduler.jobs` imports the engine
    from `helpdesk.access`, the way every other caller does.
    """
    assert REVIEW_ESCALATION_DAYS == 14
    assert timedelta(days=REVIEW_ESCALATION_DAYS) == ESCALATION
    assert NotificationKind.REVIEW_OVERDUE.value == "review_overdue"
    assert NotificationKind.REVIEW_ESCALATED.value == "review_escalated"
    assert review_schedule_module.send_review_reminders is send_review_reminders
