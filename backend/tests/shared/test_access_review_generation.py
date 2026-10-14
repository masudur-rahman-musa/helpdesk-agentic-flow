"""An earlier change §6: `quarter_period`, `check_period` and `generate_reviews`.

Written before the implementation exists. Expected surface (the implementer
must provide exactly this):

``helpdesk.access.reviews``
    ``REVIEW_PERIOD_PATTERN = r"[0-9]{4}-Q[1-4]"`` — matched with
    ``re.fullmatch``.
    ``PERIOD_LENGTH = 16`` — the same number as
    ``helpdesk.models.access.PERIOD_LENGTH``, the `access_reviews.period`
    column width, never a second literal.
    ``def quarter_period(now: datetime) -> str`` — `now`'s calendar quarter as
    ``YYYY-Qn``.
    ``def check_period(value: str) -> str`` — the accepted period, or a
    ``ToolError`` whose ``code`` is ``ErrorCode.VALIDATION``. The error CLASS
    is deliberately not named here: the plan fixes the code, not the class, so
    every refusal below is asserted as `ToolError` plus its code, the shape
    `test_access_leavers.py` already uses.
    ``open_review`` gains ``opened_by_user_id: uuid.UUID | None`` and
    ``actor_kind: str = USER_ACTOR_KIND``, both resolved through the existing
    ``revoke.resolve_actor_ref`` (decision D).

``helpdesk.access.review_schedule`` (new, re-exported from
``helpdesk.access``)
    ``GENERATED_SYSTEM_SENSITIVITIES = frozenset({"high", "critical"})``
    ``REVIEW_DUE_DAYS = 14``
    ``REVIEW_GENERATION_ACTOR_REF = "scheduler:review_generation"``
    ``async def generate_reviews(session, directory, *, now)`` — one review
    per department and per high/critical system of the BOUND tenant, for
    ``quarter_period(now)``, ``due_at = now + 14 days``, opened with the
    system actor. A scope with no owner is skipped (decision C), a scope with
    an OPEN review for that period is skipped (`ReviewAlreadyOpenError` is
    caught, decision A) and every other failure raises to the runner.

a project rule: ONE session-scoped database for the whole run, imported from
`test_access_expiry.py` rather than rebuilt — the schema is created there
exactly once. Per-test isolation is a namespace, not a rebuild: this job
sweeps a whole tenant, so every case seeds a tenant of its OWN with
`_seed.seed_tenant` and no case can see another's rows.

What `seed_tenant` plants matters to every count below, and is read off
`_seed.py` rather than remembered: its one department has a NULL
`manager_user_id` and its one system is `low` sensitivity, so a freshly
seeded tenant generates NOTHING at all. Each case turns on exactly the
scope(s) it is about, which is what keeps "no review" assertions exact.

Decision A's idempotence needs a scope whose review stays OPEN: a scope with
no active grants opens a review already `completed` (12c decision F), and
`_refuse_if_open` blocks on `open` only. Every case that runs the generator
twice therefore plants a live grant in each scope — see
`test_a_second_run_over_the_same_tenant_opens_no_second_review`, whose
docstring says so.

a project rule: `test_access_reviews.py` (12c's engine),
`test_access_review_generation.py` and `test_access_review_reminders.py` are
three distinct basenames, and none collides with anything in the tree.

No pytest-asyncio: each case is a sync function driving one `asyncio.run`.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Imported, not redefined: one database with the schema created once, shared
# with `test_access_expiry.py` and its siblings. No two of
# those files ever look at the same tenant.
from test_access_expiry import expiry_db_url  # noqa: F401

from helpdesk.shared import access as access_package
from helpdesk.shared.access.review_schedule import (
    GENERATED_SYSTEM_SENSITIVITIES,
    REVIEW_DUE_DAYS,
    REVIEW_GENERATION_ACTOR_REF,
    generate_reviews,
)
from helpdesk.shared.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.shared.core.errors import ErrorCode, NotFoundError, ToolError
from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.grants import ACTIVE_GRANT_STATUS
from helpdesk.shared.models import (
    AccessReview,
    Department,
    Grant,
    ReviewStatus,
    Role,
    System,
    User,
)
from helpdesk.shared.models.access import PERIOD_LENGTH as COLUMN_PERIOD_LENGTH
from helpdesk.shared.access import review_schedule as review_schedule_module
from helpdesk.shared.services.access import reviews as reviews_module
from helpdesk.shared.services.access.directory import DirectoryReader, SimulatedDirectory
from helpdesk.shared.services.access.reviews import (
    PERIOD_LENGTH,
    REVIEW_OPENED_EVENT_TYPE,
    REVIEW_PERIOD_PATTERN,
    OpenReviewResult,
    check_period,
    open_review,
    quarter_period,
)
from tests.support.seed import SeededTenant, seed_tenant

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
"""The one instant every generation answers for, and plan §1's own example:
`2026-09-09` is in `2026-Q3`. Passed in, never read from a clock."""

EXPECTED_PERIOD = "2026-Q3"
"""What `quarter_period(NOW)` must answer, written out rather than computed,
so a case asserting the stored `period` cannot agree with a broken
`quarter_period` by using it."""

DUE_AT = NOW + timedelta(days=14)
"""Decision B's horizon, as a literal. Asserted equal to
`now + timedelta(days=REVIEW_DUE_DAYS)` by the constants case, so a horizon
moved in the module fails there before it can quietly move every case here."""

GRANTED_AT = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
FAR_EXPIRY = datetime(2099, 6, 1, 12, 0, tzinfo=UTC)
"""Every planted grant is live at `NOW`, so a scope that carries one opens a
review that STAYS open."""

CRITICAL = "critical"
HIGH = "high"
ACTIVE_USER_STATUS = "active"
SYSTEM_ACTOR_KIND = "system"
USER_ACTOR_KIND = "user"
SUBJECT_TYPE = "access_review"
EXPECTED_PAYLOAD_KEYS = {
    "review_id",
    "department_id",
    "system_id",
    "period",
    "reviewer_user_id",
    "item_count",
    "flag_counts",
}
"""Decision K's seven keys, read off `reviews.py::_append_opened` rather than
remembered: `review.opened` is the only event `open_review`
composes, and this plan may not change what it carries."""

TENURED_START = date(2019, 3, 7)


@dataclass(frozen=True)
class ReviewFacts:
    """What one `access_reviews` row says, copied out before its session closes."""

    review_id: uuid.UUID
    reviewer_user_id: uuid.UUID
    department_id: uuid.UUID | None
    system_id: uuid.UUID | None
    period: str
    status: str
    due_at: datetime | None


@dataclass(frozen=True)
class EventFacts:
    """What one `audit_events` row says about WHO caused it."""

    actor_kind: str
    actor_ref: str
    subject_type: str
    subject_id: str
    payload: dict[str, Any]


# --- the per-case namespace ---------------------------------------------------


def _in_tenant[T](
    url: str, tenant_id: uuid.UUID, work: Callable[[AsyncSession], Awaitable[T]]
) -> T:
    """Run `work` in one committed, tenant-bound transaction of its own."""

    async def scenario() -> T:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant_id) as session:
                return await work(session)
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _tenant(url: str) -> SeededTenant:
    """A tenant of this case's own: the per-test namespace.

    `seed_tenant`'s department has no manager and its system is `low`, so
    nothing in a freshly seeded tenant is generated for until a case says so.
    """

    async def prepare() -> SeededTenant:
        engine = create_engine(url)
        try:
            return await seed_tenant(
                create_session_factory(engine), f"revgen-{uuid.uuid4().hex[:12]}"
            )
        finally:
            await engine.dispose()

    return asyncio.run(prepare())


def _set_manager(url: str, tenant: SeededTenant, *, manager_user_id: uuid.UUID | None) -> None:
    """Give (or take away) the seeded department's manager."""

    async def work(session: AsyncSession) -> None:
        await Repository(session, Department).update(
            tenant.department_id, manager_user_id=manager_user_id
        )

    _in_tenant(url, tenant.tenant_id, work)


def _plant_department(
    url: str, tenant: SeededTenant, *, manager_user_id: uuid.UUID | None
) -> uuid.UUID:
    """One extra department, with or without an owner."""
    suffix = uuid.uuid4().hex[:12]

    async def work(session: AsyncSession) -> uuid.UUID:
        row = await Repository(session, Department).create(
            name=f"Fake Dept {suffix}", manager_user_id=manager_user_id
        )
        return row.id

    return _in_tenant(url, tenant.tenant_id, work)


def _plant_system(
    url: str,
    tenant: SeededTenant,
    *,
    sensitivity: str,
    owner_user_id: uuid.UUID | None,
) -> uuid.UUID:
    """One extra system at the sensitivity the case is about."""
    suffix = uuid.uuid4().hex[:12]

    async def work(session: AsyncSession) -> uuid.UUID:
        row = await Repository(session, System).create(
            slug=f"fake-sys-{suffix}",
            name=f"Fake System {suffix}",
            sensitivity=sensitivity,
            category="other",
            owner_user_id=owner_user_id,
        )
        return row.id

    return _in_tenant(url, tenant.tenant_id, work)


def _plant_user(url: str, tenant: SeededTenant) -> uuid.UUID:
    """A second, obviously fake person in the seeded department."""
    suffix = uuid.uuid4().hex[:12]

    async def work(session: AsyncSession) -> uuid.UUID:
        row = await Repository(session, User).create(
            department_id=tenant.department_id,
            email=f"fake.person.{suffix}@example.test",
            display_name=f"Fake Person {suffix}",
            password_hash="argon2id$obviously-fake-test-hash",
            principals=["employee"],
            status=ACTIVE_USER_STATUS,
            start_date=TENURED_START,
            end_date=None,
        )
        return row.id

    return _in_tenant(url, tenant.tenant_id, work)


def _plant_live_grant_in_system(
    url: str, tenant: SeededTenant, *, system_id: uuid.UUID, user_id: uuid.UUID
) -> uuid.UUID:
    """One role in that system and one live grant on it.

    A system scope with no active grant opens a review already `completed`
    (12c decision F), which `_refuse_if_open` does not block — so a case about
    idempotence has to give the scope something to review.
    """
    suffix = uuid.uuid4().hex[:12]

    async def work(session: AsyncSession) -> uuid.UUID:
        role = await Repository(session, Role).create(
            system_id=system_id,
            slug=f"role-{suffix}",
            name=f"Fake Role {suffix}",
            privilege="read",
            default_expiry_days=30,
        )
        grant = await Repository(session, Grant).create(
            user_id=user_id,
            role_id=role.id,
            request_id=None,
            granted_by_user_id=None,
            granted_at=GRANTED_AT,
            expires_at=FAR_EXPIRY,
            revoked_at=None,
            revoke_reason=None,
            status=ACTIVE_GRANT_STATUS,
        )
        return grant.id

    return _in_tenant(url, tenant.tenant_id, work)


def _plant_review(
    url: str,
    tenant: SeededTenant,
    *,
    department_id: uuid.UUID | None = None,
    system_id: uuid.UUID | None = None,
    period: str = EXPECTED_PERIOD,
    status: str = ReviewStatus.OPEN.value,
) -> uuid.UUID:
    """One committed `access_reviews` row, planted around `open_review`."""

    async def work(session: AsyncSession) -> uuid.UUID:
        row = await Repository(session, AccessReview).create(
            reviewer_user_id=tenant.user_id,
            department_id=department_id,
            system_id=system_id,
            period=period,
            status=status,
            due_at=None,
            completed_at=None,
        )
        return row.id

    return _in_tenant(url, tenant.tenant_id, work)


# --- driving the surface ------------------------------------------------------


def _generate(url: str, tenant_id: uuid.UUID, *, now: datetime = NOW, times: int = 1) -> None:
    """`generate_reviews`, once per committed transaction."""

    async def scenario() -> None:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            for _ in range(times):
                async with tenant_session(factory, tenant_id) as session:
                    await generate_reviews(session, SimulatedDirectory(session), now=now)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def _open_review_as_the_tool_does(
    url: str,
    tenant_id: uuid.UUID,
    *,
    department_id: uuid.UUID,
    opened_by_user_id: uuid.UUID | None,
    period: str = EXPECTED_PERIOD,
) -> None:
    """`open_review` called exactly the way `mcp_server/tools/reviews.py` calls it.

    An opener id, no `actor_kind` and no `actor_ref`: the shape every existing
    caller uses, which decision D's default has to leave unchanged.
    """

    async def scenario() -> None:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant_id) as session:
                await open_review(
                    session,
                    SimulatedDirectory(session),
                    department_id=department_id,
                    system_id=None,
                    period=period,
                    opened_by_user_id=opened_by_user_id,
                    due_at=None,
                    session_id=None,
                    now=NOW,
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


# --- probes around the surface, never through it ------------------------------


def _reviews(url: str, tenant_id: uuid.UUID) -> list[ReviewFacts]:
    """Every review of one tenant, whoever opened it."""

    async def work(session: AsyncSession) -> list[ReviewFacts]:
        rows = await Repository(session, AccessReview).list()
        return [
            ReviewFacts(
                review_id=row.id,
                reviewer_user_id=row.reviewer_user_id,
                department_id=row.department_id,
                system_id=row.system_id,
                period=str(row.period),
                status=str(row.status),
                due_at=row.due_at,
            )
            for row in rows
        ]

    return _in_tenant(url, tenant_id, work)


def _scopes(rows: list[ReviewFacts]) -> set[tuple[uuid.UUID | None, uuid.UUID | None]]:
    return {(row.department_id, row.system_id) for row in rows}


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


# --- the period itself (criteria 1 and 2) -------------------------------------


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        pytest.param(datetime(2026, 1, 1, 0, 0, tzinfo=UTC), "2026-Q1", id="jan-1-q1"),
        pytest.param(datetime(2026, 4, 1, 0, 0, tzinfo=UTC), "2026-Q2", id="apr-1-q2"),
        pytest.param(datetime(2026, 7, 1, 0, 0, tzinfo=UTC), "2026-Q3", id="jul-1-q3"),
        pytest.param(datetime(2026, 10, 1, 0, 0, tzinfo=UTC), "2026-Q4", id="oct-1-q4"),
        pytest.param(NOW, EXPECTED_PERIOD, id="plan-example-2026-09-09"),
        pytest.param(datetime(2027, 3, 31, 23, 59, tzinfo=UTC), "2027-Q1", id="next-year-q1"),
    ],
)
def test_quarter_period_answers_the_calendar_quarter(instant: datetime, expected: str) -> None:
    """Criterion 1, one row per quarter and one for the year.

    Catches the two arithmetics that read plausibly and are wrong: `month // 3`
    (which answers `Q0` in January and `Q1` for April, so every quarter is
    named one behind and January's reviews are filed under a period nothing
    else will ever produce) and `(month - 1) // 3` without the `+ 1`, which
    does the same at a different offset. The first day of each quarter is the
    value both get wrong, which is why every row is one. The year row catches
    a period built from a hard-coded or ISO-week year, which would file
    2027's reviews under 2026 and make a whole quarter's set collide with the
    previous year's on the (scope, period) duplicate check.
    """
    assert quarter_period(instant) == expected


def test_the_last_microsecond_of_a_quarter_and_the_first_of_the_next_differ() -> None:
    """Criterion 1's boundary, both halves, exact.

    Catches a quarter derived with any rounding at all — `now.date()` widened
    to a month, a `>=` on the wrong side of the month boundary, or a period
    computed from `now + something`. The daily generator runs on the day a
    quarter turns over, and a boundary off by one instant opens that day's
    reviews under the OUTGOING quarter, where the next run's
    `ReviewAlreadyOpenError` will never match them: every scope would then
    carry two reviews for one quarter, one of them named for the wrong one.
    """
    last = datetime(2026, 9, 30, 23, 59, 59, 999999, tzinfo=UTC)
    first = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)

    assert quarter_period(last) == "2026-Q3"
    assert quarter_period(first) == "2026-Q4"
    assert quarter_period(last) != quarter_period(first)


def test_check_period_accepts_a_well_formed_quarter() -> None:
    """The positive half, without which every refusal below is satisfied by a
    `check_period` that refuses everything.

    Catches a pattern anchored or escaped wrongly (`\\d{4}-Q[1-4]$` compiled
    against a different flag set, a stray `^` inside a `fullmatch`), which
    would refuse the very strings `quarter_period` produces and stop the
    generator opening anything at all — a quarter with no reviews, and the
    only sign of it a `VALIDATION` in the scheduler log.
    """
    assert check_period("2026-Q1") == "2026-Q1"
    assert check_period(quarter_period(NOW)) == EXPECTED_PERIOD


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("2026-Q5", id="quarter-five"),
        pytest.param("2026-q1", id="lower-case-q"),
        pytest.param("26-Q1", id="two-digit-year"),
        pytest.param("2026-Q1\n", id="trailing-newline"),
        pytest.param("2026-Q1\r\n", id="trailing-crlf"),
        pytest.param("2026-Q1 ", id="trailing-space"),
    ],
)
def test_check_period_refuses_these(value: str) -> None:
    """Criterion 2's rejection table, a project rule's three trailing rows
    included.

    The three trailing rows are the ones that carry the kill: `re.match` and a
    Pydantic `Field(pattern=...)` both anchor only at the start, so both
    ACCEPT `"2026-Q1\\n"` — and a period with a newline in it is a stored
    label an operator reading a review queue cannot tell from the real one,
    while `_refuse_if_open` compares it by equality and therefore opens a
    SECOND review for a scope that already has one. The first three rows
    catch a pattern loosened to `[0-9]{2,4}-[Qq][0-9]`, which lets a quarter
    that does not exist name a review nothing will ever complete.

    The error CODE is asserted, not just "an exception": a `re` error or an
    `AttributeError` would also stop the write while telling the caller
    nothing it can act on, and the design is the contract across this boundary.
    """
    with pytest.raises(ToolError) as excinfo:
        check_period(value)

    assert excinfo.value.code is ErrorCode.VALIDATION
    assert excinfo.value.code.value == "VALIDATION"


def test_check_period_refuses_a_period_longer_than_the_column() -> None:
    """Criterion 2's length half, on the pattern-free path.

    The PATTERN is the narrower gate: it fixes all seven characters, so no
    17-character string can ever satisfy it and an "accepted at exactly 16"
    row is unsatisfiable. This case therefore claims no separate kill for the
    length branch — the pattern refuses this input whether or not the length
    check exists. What it does pin is the OUTCOME:
    an oversized period is refused by `check_period`, typed, rather than
    reaching a `String(16)` column and aborting the transaction with an
    asyncpg `StringDataRightTruncation` after the review and its items were
    built.

    The constant itself is pinned by the equality below: `PERIOD_LENGTH` must
    BE the column width, not a second literal beside it, or the pre-check and
    the column can drift apart in either direction.
    """
    oversized = "2026-Q1" + "1" * 10
    assert len(oversized) == PERIOD_LENGTH + 1

    with pytest.raises(ToolError) as excinfo:
        check_period(oversized)

    assert excinfo.value.code is ErrorCode.VALIDATION
    assert PERIOD_LENGTH == 16
    assert PERIOD_LENGTH == COLUMN_PERIOD_LENGTH


# --- what gets generated (criteria 3 and 4) -----------------------------------


def test_a_department_and_a_critical_system_are_each_generated_for(
    expiry_db_url: str,  # noqa: F811
) -> None:
    """Criterion 3, as two exact rows.

    Catches four wrong implementations. One that generates for departments
    only — the cheap half — which leaves every high-risk SYSTEM unreviewed
    while the job reports success. One that files the reviews under a period
    of its own (a date, a month, an empty string), which makes the next run's
    already-open check miss and the review queue unreadable. One that leaves
    `due_at` null, which decision B exists to prevent: a null-due review can
    never be overdue, so `send_review_reminders` would have nothing to read
    and the design's reminder would be dead code. And one that puts the CALLER
    in `reviewer_user_id` — there is no caller here, so the reviewer is
    asserted to be each scope's own owner.

    The seeded `low` system is left in place and asserted absent, so the two
    rows are the whole set.
    """
    tenant = _tenant(expiry_db_url)
    manager_id = _plant_user(expiry_db_url, tenant)
    _set_manager(expiry_db_url, tenant, manager_user_id=manager_id)
    owner_id = _plant_user(expiry_db_url, tenant)
    system_id = _plant_system(expiry_db_url, tenant, sensitivity=CRITICAL, owner_user_id=owner_id)

    _generate(expiry_db_url, tenant.tenant_id)

    rows = _reviews(expiry_db_url, tenant.tenant_id)
    assert _scopes(rows) == {(tenant.department_id, None), (None, system_id)}
    assert {row.period for row in rows} == {EXPECTED_PERIOD}
    assert {row.due_at for row in rows} == {DUE_AT}
    assert {row.due_at for row in rows} == {NOW + timedelta(days=REVIEW_DUE_DAYS)}
    by_scope = {(row.department_id, row.system_id): row.reviewer_user_id for row in rows}
    assert by_scope[(tenant.department_id, None)] == manager_id
    assert by_scope[(None, system_id)] == owner_id


def test_a_high_sensitivity_system_is_generated_for(
    expiry_db_url: str,  # noqa: F811
) -> None:
    """The second member of `GENERATED_SYSTEM_SENSITIVITIES`, on its own.

    Catches a filter narrowed to `critical` alone, which is exactly the shape
    somebody writes when the only case they ran planted a `critical` system:
    every `high` system in the estate then goes unreviewed for ever, and no
    other case in this file notices. The department is left ownerless, so the
    single review here can only have come from the system branch.
    """
    tenant = _tenant(expiry_db_url)
    owner_id = _plant_user(expiry_db_url, tenant)
    system_id = _plant_system(expiry_db_url, tenant, sensitivity=HIGH, owner_user_id=owner_id)

    _generate(expiry_db_url, tenant.tenant_id)

    assert _scopes(_reviews(expiry_db_url, tenant.tenant_id)) == {(None, system_id)}


@pytest.mark.parametrize(
    "sensitivity",
    [pytest.param("medium", id="medium"), pytest.param("low", id="low")],
)
def test_a_system_of_this_sensitivity_is_never_generated_for(
    expiry_db_url: str,  # noqa: F811
    sensitivity: str,
) -> None:
    """Criterion 4, one case per excluded value, so a widened filter fails
    exactly one of them.

    Catches `sensitivity != "low"` (which admits `medium`) and a filter that
    reads the column and ignores it (which admits both). The system carries a
    real owner and a live grant, so it is generated for the moment the
    sensitivity test is dropped — nothing else in the generator can refuse
    it. The department is left ownerless so the assertion is the EMPTY set,
    not "one fewer row".
    """
    tenant = _tenant(expiry_db_url)
    owner_id = _plant_user(expiry_db_url, tenant)
    system_id = _plant_system(
        expiry_db_url, tenant, sensitivity=sensitivity, owner_user_id=owner_id
    )
    _plant_live_grant_in_system(expiry_db_url, tenant, system_id=system_id, user_id=tenant.user_id)

    _generate(expiry_db_url, tenant.tenant_id)

    assert _reviews(expiry_db_url, tenant.tenant_id) == []


# --- the already-open skip and idempotence (criterion 5, decision A) ----------


def test_a_scope_with_an_open_review_is_skipped_and_the_other_scope_still_opens(
    expiry_db_url: str,  # noqa: F811
) -> None:
    """Criterion 5's first half: the refusal is caught PER SCOPE.

    Catches a generator that lets `ReviewAlreadyOpenError` out. The whole
    tenant is generated in ONE transaction (plan §5), so one department that
    was reviewed by hand this morning would roll back every OTHER scope of
    that tenant and keep doing so on every tick until somebody noticed — a
    quarter with no reviews and a daily exception nobody reads. It also
    catches a generator that catches the error and then abandons the loop:
    the system review is asserted present, not merely "no exception".

    The planted review is `open` for exactly this quarter, which is the only
    state `_refuse_if_open` blocks on, and it is the only review the
    department ends up with.
    """
    tenant = _tenant(expiry_db_url)
    manager_id = _plant_user(expiry_db_url, tenant)
    _set_manager(expiry_db_url, tenant, manager_user_id=manager_id)
    owner_id = _plant_user(expiry_db_url, tenant)
    system_id = _plant_system(expiry_db_url, tenant, sensitivity=CRITICAL, owner_user_id=owner_id)
    planted = _plant_review(expiry_db_url, tenant, department_id=tenant.department_id)

    _generate(expiry_db_url, tenant.tenant_id)

    rows = _reviews(expiry_db_url, tenant.tenant_id)
    assert _scopes(rows) == {(tenant.department_id, None), (None, system_id)}
    department_rows = [row for row in rows if row.department_id == tenant.department_id]
    assert [row.review_id for row in department_rows] == [planted]


def test_a_second_run_over_the_same_tenant_opens_no_second_review(
    expiry_db_url: str,  # noqa: F811
) -> None:
    """Criterion 5's second half, and the whole reason decision A registers a
    QUARTERLY job on a DAILY cadence.

    Catches a generator with no already-open handling at all: the same scopes
    are due on every tick for three months, so without the skip a department
    collects roughly ninety reviews per quarter and its manager's queue stops
    being readable. Two runs, one review per scope.

    Both scopes are planted with a LIVE grant on purpose: a scope with no
    active grants opens a review already `completed` (12c decision F), and
    `_refuse_if_open` blocks on `open` only — so an empty scope would
    duplicate here no matter how the skip is written, and the case would be
    asserting the engine's gap rather than this plan's rule.
    """
    tenant = _tenant(expiry_db_url)
    manager_id = _plant_user(expiry_db_url, tenant)
    _set_manager(expiry_db_url, tenant, manager_user_id=manager_id)
    owner_id = _plant_user(expiry_db_url, tenant)
    system_id = _plant_system(expiry_db_url, tenant, sensitivity=CRITICAL, owner_user_id=owner_id)
    _plant_live_grant_in_system(expiry_db_url, tenant, system_id=system_id, user_id=tenant.user_id)

    _generate(expiry_db_url, tenant.tenant_id, times=2)

    rows = _reviews(expiry_db_url, tenant.tenant_id)
    assert len(rows) == 2
    assert _scopes(rows) == {(tenant.department_id, None), (None, system_id)}
    assert {row.status for row in rows} == {ReviewStatus.OPEN.value}


# --- the ownerless scopes (criterion 6, decision C) ---------------------------


def test_a_department_with_no_manager_is_skipped_and_writes_no_review(
    expiry_db_url: str,  # noqa: F811
) -> None:
    """Decision C for the department half, isolated.

    Catches the rejected alternative — falling back to an `it_admin`, or to
    "the caller" the way `open_review` does when a scope names nobody — which
    files a quarterly review against somebody who never owned the department
    and who cannot answer for it. `access_reviews.reviewer_user_id` is NOT
    nullable, so something has to be chosen and the choice has to be visible;
    the gap is meant to read as a scope with no review.

    The critical system IS generated for in the same run, so this case cannot
    pass because the generator did nothing.
    """
    tenant = _tenant(expiry_db_url)
    ownerless = _plant_department(expiry_db_url, tenant, manager_user_id=None)
    owner_id = _plant_user(expiry_db_url, tenant)
    system_id = _plant_system(expiry_db_url, tenant, sensitivity=CRITICAL, owner_user_id=owner_id)

    _generate(expiry_db_url, tenant.tenant_id)

    rows = _reviews(expiry_db_url, tenant.tenant_id)
    assert _scopes(rows) == {(None, system_id)}
    assert [row for row in rows if row.department_id == ownerless] == []


def test_a_system_with_no_owner_is_skipped_and_writes_no_review(
    expiry_db_url: str,  # noqa: F811
) -> None:
    """Decision C for the system half, which has its own read and its own way
    of being got wrong.

    Catches a skip applied to `departments.manager_user_id` only: an unowned
    `critical` system would then be reviewed by whoever the fallback picked,
    which is the one review an auditor is most likely to read and the one
    least likely to be answerable. The department IS generated for in the same
    run, so a generator that skipped everything fails here.
    """
    tenant = _tenant(expiry_db_url)
    manager_id = _plant_user(expiry_db_url, tenant)
    _set_manager(expiry_db_url, tenant, manager_user_id=manager_id)
    ownerless = _plant_system(expiry_db_url, tenant, sensitivity=CRITICAL, owner_user_id=None)

    _generate(expiry_db_url, tenant.tenant_id)

    rows = _reviews(expiry_db_url, tenant.tenant_id)
    assert _scopes(rows) == {(tenant.department_id, None)}
    assert [row for row in rows if row.system_id == ownerless] == []


def test_another_tenants_scopes_are_never_generated_for(
    expiry_db_url: str,  # noqa: F811
) -> None:
    """Criterion 7. Catches a generator that builds its own statement over
    `departments` and `systems` instead of going through the tenant-bound
    session.

    The runner calls this once per tenant, so a read that ignores the binding
    opens every tenant's reviews on the first tenant's tick — each row written
    under the wrong `tenant_id`, naming a stranger's department, and each with
    a reviewer from another company. The neighbour's scopes are set up
    identically, so only the scoping can save them.
    """
    tenant = _tenant(expiry_db_url)
    neighbour = _tenant(expiry_db_url)
    for seeded in (tenant, neighbour):
        manager_id = _plant_user(expiry_db_url, seeded)
        _set_manager(expiry_db_url, seeded, manager_user_id=manager_id)
        _plant_system(expiry_db_url, seeded, sensitivity=CRITICAL, owner_user_id=manager_id)

    _generate(expiry_db_url, tenant.tenant_id)

    assert len(_reviews(expiry_db_url, tenant.tenant_id)) == 2
    assert _reviews(expiry_db_url, neighbour.tenant_id) == []
    assert _events(expiry_db_url, neighbour.tenant_id, REVIEW_OPENED_EVENT_TYPE) == []


# --- the actor on the audit event (criteria 8 to 10, decision D) --------------


def test_every_review_opened_event_the_generator_causes_carries_the_system_actor(
    expiry_db_url: str,  # noqa: F811
) -> None:
    """Criterion 8, over the whole union of events this run composes.

    `review.opened` is the ONLY event `open_review` composes — read off
    `reviews.py::_append_opened` rather than remembered —
    and the generator opens one per scope, so every event of the run is
    asserted, not just the first.

    Catches decision D's rejected alternative: leaving `actor_kind` at `user`
    and putting something system-shaped in `actor_ref`, which files a
    quarterly automatic sweep in the audit log as a person's decision. An
    auditor separating "the machine opened this" from "a manager opened this"
    reads `actor_kind`, and a query for the system's own work would return
    every human review as well. The subject and the seven payload keys are
    asserted UNCHANGED: this plan threads a new actor
    through an existing event and may not quietly reshape what it carries.
    """
    tenant = _tenant(expiry_db_url)
    manager_id = _plant_user(expiry_db_url, tenant)
    _set_manager(expiry_db_url, tenant, manager_user_id=manager_id)
    system_id = _plant_system(expiry_db_url, tenant, sensitivity=CRITICAL, owner_user_id=manager_id)

    _generate(expiry_db_url, tenant.tenant_id)

    events = _events(expiry_db_url, tenant.tenant_id, REVIEW_OPENED_EVENT_TYPE)
    reviews = {str(row.review_id): row for row in _reviews(expiry_db_url, tenant.tenant_id)}
    assert len(events) == 2
    for event in events:
        assert event.actor_kind == SYSTEM_ACTOR_KIND
        assert event.actor_ref == REVIEW_GENERATION_ACTOR_REF
        assert event.actor_ref == "scheduler:review_generation"
        assert event.subject_type == SUBJECT_TYPE
        assert event.subject_id in reviews
        assert set(event.payload) == EXPECTED_PAYLOAD_KEYS
        assert event.payload["period"] == EXPECTED_PERIOD
    assert {event.subject_id for event in events} == set(reviews)
    assert {reviews[event.subject_id].system_id for event in events} == {None, system_id}


def test_open_review_with_an_opener_id_and_no_actor_kind_still_writes_the_user_actor(
    expiry_db_url: str,  # noqa: F811
) -> None:
    """Criterion 9, the default decision D turns on, called exactly the way
    `mcp_server/tools/reviews.py` calls it: an opener id, no `actor_kind`, no
    `actor_ref`.

    Catches the widening done with the default flipped — the risk plan §5
    names. `open_review` is 12c's most-covered engine and no existing case
    reads `actor_kind` on its event, so a `SYSTEM_ACTOR_KIND` default would
    leave the whole suite green while every manager-opened review in the audit
    log was relabelled as something the machine decided. `actor_ref` is
    asserted as the opener's id for the same reason: it is the only field
    naming WHO asked for the review.
    """
    tenant = _tenant(expiry_db_url)
    opener_id = _plant_user(expiry_db_url, tenant)
    _set_manager(expiry_db_url, tenant, manager_user_id=opener_id)

    _open_review_as_the_tool_does(
        expiry_db_url,
        tenant.tenant_id,
        department_id=tenant.department_id,
        opened_by_user_id=opener_id,
    )

    events = _events(expiry_db_url, tenant.tenant_id, REVIEW_OPENED_EVENT_TYPE)
    assert len(events) == 1
    assert events[0].actor_kind == USER_ACTOR_KIND
    assert events[0].actor_ref == str(opener_id)


def test_open_review_with_neither_an_opener_id_nor_an_actor_ref_is_refused(
    expiry_db_url: str,  # noqa: F811
) -> None:
    """Criterion 10, and the price of widening the opener to `uuid.UUID | None`.

    Catches the widening done WITHOUT `resolve_actor_ref`'s check: `str(None)`
    renders the literal `"None"` into `actor_ref`, and a review would then be
    opened under a `system` kind with nobody named at all — unattributable,
    and indistinguishable in a query from a real actor called `None`.
    `VALIDATION` is asserted as the code, not just "an exception", because an
    `AttributeError` from an unrendered id would also stop the write while
    telling the runner nothing.

    The scope HAS an owner, so the reviewer fallback cannot be what fails, and
    nothing may be written first: both the review rows and the events are
    re-read empty.
    """
    tenant = _tenant(expiry_db_url)
    manager_id = _plant_user(expiry_db_url, tenant)
    _set_manager(expiry_db_url, tenant, manager_user_id=manager_id)

    with pytest.raises(ToolError) as excinfo:
        _open_review_as_the_tool_does(
            expiry_db_url,
            tenant.tenant_id,
            department_id=tenant.department_id,
            opened_by_user_id=None,
        )

    assert excinfo.value.code is ErrorCode.VALIDATION
    assert _reviews(expiry_db_url, tenant.tenant_id) == []
    assert _events(expiry_db_url, tenant.tenant_id, REVIEW_OPENED_EVENT_TYPE) == []


# --- the unhappy path (criterion 12, 21a decision C) --------------------------


def test_a_failure_other_than_already_open_propagates_out_of_generate_reviews(
    expiry_db_url: str,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Criterion 12: only `ReviewAlreadyOpenError` is caught, and everything
    else belongs to the runner.

    Catches a blanket `except Exception: continue` around the per-scope call.
    The runner deletes the job's lock key when a job raises and retries on the
    next tick (21a decision C); a generator that swallowed a failure would
    report a clean run, leave the key in place for a whole day, and the
    quarter would silently be short a review with nothing in any log to say
    which scope it was.

    The scope read SUCCEEDS and the failure is raised from `open_review`
    itself — the shape criterion 12 names — through a proxy installed on both
    modules that could hold the name, the same technique
    `test_access_leavers.py::_sweep_with_a_concurrent_departure` uses. The
    error is a `NotFoundError`, which is a `ToolError` and therefore exactly
    the class a too-wide `except ToolError` would swallow.
    """
    tenant = _tenant(expiry_db_url)
    manager_id = _plant_user(expiry_db_url, tenant)
    _set_manager(expiry_db_url, tenant, manager_user_id=manager_id)
    _plant_system(expiry_db_url, tenant, sensitivity=CRITICAL, owner_user_id=manager_id)

    async def proxy(
        session: AsyncSession,
        directory: DirectoryReader,
        *,
        department_id: uuid.UUID | None,
        system_id: uuid.UUID | None,
        period: str,
        opened_by_user_id: uuid.UUID | None,
        due_at: datetime | None = None,
        actor_ref: str | None = None,
        actor_kind: str = USER_ACTOR_KIND,
        session_id: uuid.UUID | None = None,
        now: datetime,
    ) -> OpenReviewResult:
        raise NotFoundError("planted failure: this scope went away mid-run")

    assert hasattr(review_schedule_module, "open_review") or hasattr(
        reviews_module, "open_review"
    ), "premise: one of the two modules exposes open_review to patch"
    for module in (review_schedule_module, reviews_module):
        monkeypatch.setattr(module, "open_review", proxy, raising=False)

    with pytest.raises(NotFoundError):
        _generate(expiry_db_url, tenant.tenant_id)

    assert _reviews(expiry_db_url, tenant.tenant_id) == []


# --- the constants and the exports --------------------------


def test_the_review_generation_constants_are_exactly_the_planned_values() -> None:
    """a project rule on everything this half of the plan introduces.

    Catches a due horizon quietly moved: every case above is written against
    14 days, so a module constant of 7 or 30 would leave them green while the
    service promised reviewers a different fortnight from the one §1 fixes —
    and the reminder job's overdue band is measured from exactly this value.
    Catches the sensitivity set widened to include `medium` (which would put
    every low-risk system in front of a reviewer every quarter) or narrowed to
    `critical` alone. Catches `REVIEW_GENERATION_ACTOR_REF` drifting from the
    string an auditor greps `actor_ref` for to separate the quarterly sweep
    from a person's review — the event cases read it as a constant, so a drift
    to `"review_generation"` would leave them green and the log unqueryable by
    the one name the runbook prints. And it pins the pattern itself, which the
    rejection table is written against.

    The exports are part of the surface: `scheduler.jobs` imports the engines
    from `helpdesk.access`, the way every other caller does.
    """
    assert REVIEW_DUE_DAYS == 14
    assert frozenset({"high", "critical"}) == GENERATED_SYSTEM_SENSITIVITIES
    assert REVIEW_GENERATION_ACTOR_REF == "scheduler:review_generation"
    assert REVIEW_PERIOD_PATTERN == r"[0-9]{4}-Q[1-4]"
    assert access_package.generate_reviews is generate_reviews
