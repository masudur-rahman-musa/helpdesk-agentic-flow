"""An earlier change, sections 1 and 6: `open_review` and `list_items` over the seed.

Surface under test (written before it exists):

``helpdesk.access.reviews``
    ``REVIEW_OPENED_EVENT_TYPE = "review.opened"`` (the design, decision K).
    ``STALE_WINDOW = timedelta(days=14)`` — decision C's expiry half.
    ``ReviewAlreadyOpenError(ToolError)`` — `code = POLICY_BLOCKED`
    (decision G).
    ``async def open_review(session, directory, *, department_id, system_id,
    period, opened_by_user_id, due_at=None, actor_ref=None, session_id=None,
    now) -> OpenReviewResult`` — a frozen result carrying ``.review``,
    ``.items`` and ``.flag_counts``. It resolves the scope, locks the scope row
    (`core.repository.lock_department` / `lock_system`), refuses a second OPEN
    review for the same (scope, period), writes one `access_reviews` row plus
    one `review_items` row per ACTIVE grant in scope with decision E's flag,
    and appends one `review.opened` naming the REVIEW. All in the caller's
    transaction.
    ``async def list_items(session, review_id, *, limit) -> Sequence[ReviewItem]``
    — the items of one review, in the bound tenant, at most `limit` of them.

"Active grant in scope" is `status = 'active'` and `revoked_at IS NULL`, and
deliberately NOT "live": decision C counts an ALREADY-PAST expiry as stale, so
a filter that also required an unexpired grant would drop exactly the rows the
review exists to surface. Pinned by
`test_department_scope_collects_that_departments_grants_and_nothing_else`,
which plants one row per excluded state.

a project rule: the seeded database is `test_access_snapshot.py`'s
session-scoped `access_db_url` fixture, imported rather than rebuilt (argon2
seeding is the slow part of this suite). Per-test isolation inside it is a
namespace: every case plants its OWN department, system, roles, users and
grants (uuid4-suffixed names, so nothing collides with the seed or with
another module), and every review carries a period no other case uses.
Identities of seeded rows are recomputed from `scripts.seed.generate_tenant`.

No pytest-asyncio: each case is a sync function driving one `asyncio.run`
(the race case drives two, one per thread).
"""

import asyncio
import contextlib
import itertools
import uuid
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from helpdesk.access.directory import SimulatedDirectory
from helpdesk.access.reviews import (
    REVIEW_OPENED_EVENT_TYPE,
    STALE_WINDOW,
    ReviewAlreadyOpenError,
    list_items,
    open_review,
)
from helpdesk.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.core.repository import ACTIVE_GRANT_STATUS, REVOKED_GRANT_STATUS, Repository
from helpdesk.errors import ErrorCode
from helpdesk.models import (
    Department,
    Grant,
    ReviewFlag,
    ReviewStatus,
    Role,
    RoleTemplate,
    SodRule,
    System,
    User,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Imported, not redefined: one seeded database for the whole session, shared
# with `test_access_snapshot.py` and its siblings.
from test_access_snapshot import (
    NOW,
    ROWS,
    access_db_url,  # noqa: F401
)

from scripts.seed import CUAN, generate_tenant

OTHER_TENANT_ROWS = generate_tenant(CUAN)

# The instant every review is opened at, fixed so a flag never depends on when
# the suite runs. `NOW` comes from the snapshot module, so every file in this
# suite answers for the same instant.
OPENED_AT = NOW

GRANTED_AT = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
# Far beyond `OPENED_AT + STALE_WINDOW`: an expiry that cannot be stale.
FAR_EXPIRY = datetime(2099, 6, 1, 12, 0, tzinfo=UTC)
# Strictly before `OPENED_AT`: decision C counts an already-past expiry stale.
PAST_EXPIRY = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)

PLANTED_START = date(2019, 3, 7)
PLANTED_PASSWORD_HASH = "not-a-real-hash-this-user-never-logs-in"

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

# Unique periods, one per opened review: half the per-test namespace (the
# other half is the freshly planted scope). The year is what varies, because
# decision H's shape admits exactly four quarters.
_PERIOD_YEARS = itertools.count(2101)


def period() -> str:
    """A `YYYY-Q1` no other review in this run uses."""
    return f"{next(_PERIOD_YEARS)}-Q1"


# --- running one scenario in one transaction -----------------------------------


def in_tenant[T](
    url: str,
    work: Callable[[AsyncSession], Awaitable[T]],
    *,
    tenant_id: uuid.UUID | None = None,
) -> T:
    """Run `work` in one committed tenant-scoped transaction."""

    async def scenario() -> T:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(
                factory, ROWS.tenant_id if tenant_id is None else tenant_id
            ) as session:
                return await work(session)
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


# --- the planted scope ---------------------------------------------------------


@dataclass(frozen=True)
class Scope:
    """One freshly planted department, system and cast, owned by one test.

    Everything here is obviously fake and unique, so the only grants any
    review over this scope can see are the ones the case plants.
    """

    department_id: uuid.UUID
    manager_id: uuid.UUID
    system_id: uuid.UUID
    owner_id: uuid.UUID
    template_role_id: uuid.UUID
    extra_role_id: uuid.UUID
    conflict_role_id: uuid.UUID
    partner_role_id: uuid.UUID
    other_system_id: uuid.UUID
    other_system_role_id: uuid.UUID
    members: tuple[uuid.UUID, ...]
    outsider_id: uuid.UUID
    outsider_department_id: uuid.UUID


async def _add_user(session: AsyncSession, department_id: uuid.UUID | None) -> uuid.UUID:
    row = await Repository(session, User).create(
        department_id=department_id,
        email=f"fake.reviewee.{uuid.uuid4().hex}@fake.invalid",
        display_name="Fake Reviewee",
        password_hash=PLANTED_PASSWORD_HASH,
        principals=["employee"],
        status="active",
        start_date=PLANTED_START,
        end_date=None,
    )
    return row.id


async def _add_role(session: AsyncSession, system_id: uuid.UUID) -> uuid.UUID:
    row = await Repository(session, Role).create(
        system_id=system_id,
        slug=f"fake-role-{uuid.uuid4().hex[:12]}",
        name="Fake Review Role",
        privilege="read",
        default_expiry_days=365,
    )
    return row.id


async def _add_system(session: AsyncSession, owner_user_id: uuid.UUID | None) -> uuid.UUID:
    row = await Repository(session, System).create(
        slug=f"fake-sys-{uuid.uuid4().hex[:12]}",
        name="Fake Review System",
        sensitivity="low",
        category="other",
        owner_user_id=owner_user_id,
    )
    return row.id


def plant_scope(url: str, *, members: int = 1) -> Scope:
    """A whole department, its system, its roles and its people, in one go."""

    async def work(session: AsyncSession) -> Scope:
        department = await Repository(session, Department).create(
            name=f"Fake Review Dept {uuid.uuid4().hex[:12]}", manager_user_id=None
        )
        outsider_department = await Repository(session, Department).create(
            name=f"Fake Other Dept {uuid.uuid4().hex[:12]}", manager_user_id=None
        )
        manager_id = await _add_user(session, department.id)
        await Repository(session, Department).update(department.id, manager_user_id=manager_id)
        owner_id = await _add_user(session, department.id)

        system_id = await _add_system(session, owner_id)
        other_system_id = await _add_system(session, None)
        template_role_id = await _add_role(session, system_id)
        extra_role_id = await _add_role(session, system_id)
        conflict_role_id = await _add_role(session, system_id)
        partner_role_id = await _add_role(session, system_id)
        other_system_role_id = await _add_role(session, other_system_id)

        await Repository(session, RoleTemplate).create(
            department_id=department.id, role_id=template_role_id
        )
        await Repository(session, SodRule).create(
            role_a_id=conflict_role_id,
            role_b_id=partner_role_id,
            rationale="fake pairing planted by the review tests",
            policy_ref="SoD-FAKE §1",
        )

        member_ids = tuple([await _add_user(session, department.id) for _ in range(members)])
        outsider_id = await _add_user(session, outsider_department.id)
        return Scope(
            department_id=department.id,
            manager_id=manager_id,
            system_id=system_id,
            owner_id=owner_id,
            template_role_id=template_role_id,
            extra_role_id=extra_role_id,
            conflict_role_id=conflict_role_id,
            partner_role_id=partner_role_id,
            other_system_id=other_system_id,
            other_system_role_id=other_system_role_id,
            members=member_ids,
            outsider_id=outsider_id,
            outsider_department_id=outsider_department.id,
        )

    return in_tenant(url, work)


def plant_grant(
    url: str,
    *,
    user_id: uuid.UUID,
    role_id: uuid.UUID,
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
    status: str = ACTIVE_GRANT_STATUS,
) -> uuid.UUID:
    """One committed `grants` row, written around the reviewer."""

    async def work(session: AsyncSession) -> uuid.UUID:
        row = await Repository(session, Grant).create(
            user_id=user_id,
            role_id=role_id,
            request_id=None,
            granted_by_user_id=None,
            granted_at=GRANTED_AT,
            expires_at=expires_at,
            revoked_at=revoked_at,
            revoke_reason=None,
            status=status,
        )
        return row.id

    return in_tenant(url, work)


# --- driving the surface -------------------------------------------------------


@dataclass(frozen=True)
class ReviewFacts:
    """What one `access_reviews` row says, copied out before its session closes."""

    review_id: uuid.UUID
    department_id: uuid.UUID | None
    system_id: uuid.UUID | None
    period: str
    status: str
    reviewer_user_id: uuid.UUID


def _facts(review: Any) -> ReviewFacts:
    return ReviewFacts(
        review_id=review.id,
        department_id=review.department_id,
        system_id=review.system_id,
        period=review.period,
        status=review.status,
        reviewer_user_id=review.reviewer_user_id,
    )


def open_one(
    url: str,
    *,
    department_id: uuid.UUID | None = None,
    system_id: uuid.UUID | None = None,
    review_period: str | None = None,
    opened_by_user_id: uuid.UUID | None = None,
    now: datetime = OPENED_AT,
) -> ReviewFacts:
    """One `open_review` in one committed transaction."""
    opener = (
        ROWS.users_by_department["IT"][3].id if opened_by_user_id is None else opened_by_user_id
    )

    async def work(session: AsyncSession) -> ReviewFacts:
        result = await open_review(
            session,
            SimulatedDirectory(session),
            department_id=department_id,
            system_id=system_id,
            period=period() if review_period is None else review_period,
            opened_by_user_id=opener,
            actor_ref=str(opener),
            session_id=None,
            now=now,
        )
        return _facts(result.review)

    return in_tenant(url, work)


class _Boom(Exception):
    """Raised by the caller AFTER `open_review` returned, to roll it back."""


def open_then_raise(url: str, *, department_id: uuid.UUID, review_period: str) -> None:
    """Open a review, then fail the enclosing transaction."""
    opener = ROWS.users_by_department["IT"][4].id

    async def work(session: AsyncSession) -> None:
        result = await open_review(
            session,
            SimulatedDirectory(session),
            department_id=department_id,
            system_id=None,
            period=review_period,
            opened_by_user_id=opener,
            actor_ref=str(opener),
            session_id=None,
            now=OPENED_AT,
        )
        assert result.review.id is not None, (
            "premise: the review was written before the caller failed"
        )
        raise _Boom("the caller failed after the review was written")

    with contextlib.suppress(_Boom):
        in_tenant(url, work)


# --- probes around the surface, never through it -------------------------------


def _rows(url: str, sql: str, params: dict[str, Any]) -> list[Any]:
    """One read with NO tenant filter at all: the probe must see everything."""

    async def scenario() -> list[Any]:
        engine = create_engine(url)
        try:
            async with engine.connect() as conn:
                return list(await conn.execute(text(sql), params))
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def reviews_for(url: str, *, department_id: uuid.UUID | None = None) -> list[Any]:
    return _rows(
        url,
        "SELECT id, period, status, reviewer_user_id FROM access_reviews"
        " WHERE department_id = :department ORDER BY created_at, id",
        {"department": department_id},
    )


def reviews_with_period(url: str, review_period: str) -> list[Any]:
    return _rows(
        url,
        "SELECT id, department_id, system_id FROM access_reviews WHERE period = :period",
        {"period": review_period},
    )


def items_of(url: str, review_id: uuid.UUID) -> dict[uuid.UUID, str]:
    """Every `review_items` row of one review, as {grant_id: flag}."""
    rows = _rows(
        url,
        "SELECT grant_id, flag FROM review_items WHERE review_id = :review",
        {"review": review_id},
    )
    return {uuid.UUID(str(row[0])): str(row[1]) for row in rows}


def item_ids_of(url: str, review_id: uuid.UUID) -> list[uuid.UUID]:
    rows = _rows(
        url,
        "SELECT id FROM review_items WHERE review_id = :review ORDER BY created_at, id",
        {"review": review_id},
    )
    return [uuid.UUID(str(row[0])) for row in rows]


def opened_events(url: str, tenant_id: uuid.UUID | None = None) -> list[Any]:
    return _rows(
        url,
        "SELECT subject_type, subject_id, payload FROM audit_events"
        " WHERE tenant_id = :tenant AND event_type = :event_type ORDER BY seq",
        {
            "tenant": ROWS.tenant_id if tenant_id is None else tenant_id,
            "event_type": REVIEW_OPENED_EVENT_TYPE,
        },
    )


# --- the constants -------------------------------------------


def test_the_review_constants_and_error_code_are_exactly_the_spec_values() -> None:
    """Pins every value this file otherwise reads as a symbol.

    Catches `STALE_WINDOW` widened to 30 days or narrowed to 7: the boundary
    cases below compute their expiries FROM the constant, so they would move
    with it and stay green while every reviewer in the product was shown a
    different set of grants. Catches the event type renamed, which leaves the
    audit log unqueryable with every query in this file still green for the
    same reason. Catches `ReviewAlreadyOpenError` carrying `NOT_FOUND` — which
    would tell a caller who legitimately reached the scope that it does not
    exist — or inheriting `ToolError`'s default `INTERNAL`, filing a refused
    duplicate as an outage.
    """
    assert timedelta(days=14) == STALE_WINDOW
    assert REVIEW_OPENED_EVENT_TYPE == "review.opened"
    assert ReviewAlreadyOpenError.code is ErrorCode.POLICY_BLOCKED
    assert ReviewAlreadyOpenError.code.value == "POLICY_BLOCKED"


# --- what a scope collects (criterion 1) ---------------------------------------


def test_department_scope_collects_that_departments_grants_and_nothing_else(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches a department review keyed on anything but "grants held by users
    of this department", and one that filters the grants by liveness.

    Three rows are planted that must NOT become items — one held by a user of
    another department, one revoked, one whose `status` is not `active` — and
    each violates exactly one conjunct, so a dropped clause fails a named row
    rather than hiding behind the others. The EXPIRED-but-active row must be
    an item: decision C counts an already-past expiry as stale, so a scope
    filter built from `is_live_grant` (the obvious reuse) drops precisely the
    grants the review exists to surface, and passes every other case here.
    """
    scope = plant_scope(access_db_url, members=1)
    member = scope.members[0]
    kept = plant_grant(access_db_url, user_id=member, role_id=scope.template_role_id)
    expired_but_active = plant_grant(
        access_db_url,
        user_id=member,
        role_id=scope.extra_role_id,
        expires_at=PAST_EXPIRY,
    )
    plant_grant(access_db_url, user_id=scope.outsider_id, role_id=scope.template_role_id)
    plant_grant(
        access_db_url,
        user_id=member,
        role_id=scope.other_system_role_id,
        revoked_at=GRANTED_AT,
        status=REVOKED_GRANT_STATUS,
    )
    plant_grant(access_db_url, user_id=member, role_id=scope.partner_role_id, status="expired")

    review = open_one(access_db_url, department_id=scope.department_id)

    assert review.department_id == scope.department_id
    assert review.system_id is None
    assert review.status == ReviewStatus.OPEN.value
    assert set(items_of(access_db_url, review.review_id)) == {kept, expired_but_active}


def test_system_scope_collects_that_systems_grants_and_nothing_else(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches a system review that collects by DEPARTMENT (the other half of
    the same handler) or that ignores the system entirely.

    The same person holds one grant on the reviewed system and one on another
    system, and a user in a completely different department holds a grant on
    the reviewed system: only "grants for roles of this system" produces the
    asserted set. A handler keyed on the department of whoever opened it, or
    one collecting every grant in the tenant, fails on a different row each.
    """
    scope = plant_scope(access_db_url, members=1)
    member = scope.members[0]
    on_system = plant_grant(access_db_url, user_id=member, role_id=scope.template_role_id)
    outsider_on_system = plant_grant(
        access_db_url, user_id=scope.outsider_id, role_id=scope.extra_role_id
    )
    plant_grant(access_db_url, user_id=member, role_id=scope.other_system_role_id)

    review = open_one(access_db_url, system_id=scope.system_id)

    assert review.system_id == scope.system_id
    assert review.department_id is None
    assert set(items_of(access_db_url, review.review_id)) == {on_system, outsider_on_system}


# --- the four flags and their precedence (criterion 2, decision E) --------------


def test_a_grant_inside_the_template_with_a_distant_expiry_reads_ok(
    access_db_url: str,  # noqa: F811
) -> None:
    """The baseline every other flag case is measured against.

    Without it a flagger that answered `sod_conflict` for everything would
    satisfy each of the three positive cases below, and nothing would show
    that the flag ever discriminates.
    """
    scope = plant_scope(access_db_url, members=1)
    grant_id = plant_grant(
        access_db_url,
        user_id=scope.members[0],
        role_id=scope.template_role_id,
        expires_at=FAR_EXPIRY,
    )

    review = open_one(access_db_url, department_id=scope.department_id)

    assert items_of(access_db_url, review.review_id) == {grant_id: ReviewFlag.OK.value}


def test_a_role_outside_the_holders_template_reads_non_standard(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches a `non_standard` test that asks the wrong template.

    Both grants belong to the same person: one role IS in their department's
    template and one is not, and only the second is flagged. A flagger reading
    "every role of the system" or "every role in the tenant" as the template
    marks both or neither. The expiry is far away on both, so staleness cannot
    produce this answer.
    """
    scope = plant_scope(access_db_url, members=1)
    member = scope.members[0]
    standard = plant_grant(
        access_db_url, user_id=member, role_id=scope.template_role_id, expires_at=FAR_EXPIRY
    )
    outside = plant_grant(
        access_db_url, user_id=member, role_id=scope.extra_role_id, expires_at=FAR_EXPIRY
    )

    review = open_one(access_db_url, department_id=scope.department_id)

    assert items_of(access_db_url, review.review_id) == {
        standard: ReviewFlag.OK.value,
        outside: ReviewFlag.NON_STANDARD.value,
    }


def test_the_non_standard_template_is_the_one_the_transfer_diff_reads(
    access_db_url: str,  # noqa: F811
) -> None:
    """An earlier change §5's divergence risk: the template question is now asked by
    an earlier change's transfer diff AND by this flag.

    Catches the two answers drifting apart — a review calling a grant
    `non_standard` that `transfer_user` would RETAIN as a template role, which
    reads as two different words for one grant and is invisible to every other
    case here, since they only ever ask one of the two. The directory's
    `list_membership_role_ids` is what the transfer diff reads, so it is what
    the expectation is built from rather than from the planted ids.
    """
    scope = plant_scope(access_db_url, members=1)
    member = scope.members[0]
    standard = plant_grant(
        access_db_url, user_id=member, role_id=scope.template_role_id, expires_at=FAR_EXPIRY
    )
    outside = plant_grant(
        access_db_url, user_id=member, role_id=scope.extra_role_id, expires_at=FAR_EXPIRY
    )

    async def membership(session: AsyncSession) -> frozenset[uuid.UUID]:
        return await SimulatedDirectory(session).list_membership_role_ids(member)

    template_role_ids = in_tenant(access_db_url, membership)
    review = open_one(access_db_url, department_id=scope.department_id)

    assert template_role_ids == frozenset({scope.template_role_id})
    flags = items_of(access_db_url, review.review_id)
    assert flags[standard] == ReviewFlag.OK.value
    assert flags[outside] == ReviewFlag.NON_STANDARD.value
    assert scope.extra_role_id not in template_role_ids


def test_a_grant_half_of_a_held_sod_pair_reads_sod_conflict(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches a conflict flag that marks a role merely NAMED by an SoD rule.

    Both halves of the planted pair are held live by one person, so both items
    read `sod_conflict`; a third person holds only one half of the same pair
    and must not be flagged, which is the case that separates "this role is in
    a rule" from "this holder actually has both".
    """
    scope = plant_scope(access_db_url, members=2)
    conflicted, innocent = scope.members
    first = plant_grant(
        access_db_url,
        user_id=conflicted,
        role_id=scope.conflict_role_id,
        expires_at=FAR_EXPIRY,
    )
    second = plant_grant(
        access_db_url,
        user_id=conflicted,
        role_id=scope.partner_role_id,
        expires_at=FAR_EXPIRY,
    )
    half = plant_grant(
        access_db_url,
        user_id=innocent,
        role_id=scope.conflict_role_id,
        expires_at=FAR_EXPIRY,
    )

    review = open_one(access_db_url, department_id=scope.department_id)

    assert items_of(access_db_url, review.review_id) == {
        first: ReviewFlag.SOD_CONFLICT.value,
        second: ReviewFlag.SOD_CONFLICT.value,
        half: ReviewFlag.NON_STANDARD.value,
    }


def test_a_grant_that_is_all_three_reads_sod_conflict(
    access_db_url: str,  # noqa: F811
) -> None:
    """Decision E's precedence, on the only grant that can pin it.

    The grant is stale (already expired), outside its holder's template AND
    half of a held SoD pair, so `review_items.flag` — one column, one word —
    must be the FIRST of the three. Any other precedence order (stale first,
    template first) passes every single-flag case above and fails only here,
    which is why one grant carries all three rather than three grants carrying
    one each.
    """
    scope = plant_scope(access_db_url, members=1)
    member = scope.members[0]
    all_three = plant_grant(
        access_db_url,
        user_id=member,
        role_id=scope.conflict_role_id,
        expires_at=PAST_EXPIRY,
    )
    partner = plant_grant(
        access_db_url,
        user_id=member,
        role_id=scope.partner_role_id,
        expires_at=PAST_EXPIRY,
    )

    review = open_one(access_db_url, department_id=scope.department_id)

    flags = items_of(access_db_url, review.review_id)
    assert flags[all_three] == ReviewFlag.SOD_CONFLICT.value
    assert flags[partner] == ReviewFlag.SOD_CONFLICT.value


def test_a_stale_grant_outside_the_template_reads_non_standard(
    access_db_url: str,  # noqa: F811
) -> None:
    """The second rung of decision E's ladder, which the all-three case above
    cannot reach on its own.

    A grant that is BOTH stale and outside the template reads `non_standard`,
    so an implementation that ordered `stale` above `non_standard` — and
    therefore still answered `sod_conflict` for the all-three grant — fails
    here. Without this case the precedence claim is only pinned at its top.
    """
    scope = plant_scope(access_db_url, members=1)
    grant_id = plant_grant(
        access_db_url,
        user_id=scope.members[0],
        role_id=scope.extra_role_id,
        expires_at=PAST_EXPIRY,
    )

    review = open_one(access_db_url, department_id=scope.department_id)

    assert items_of(access_db_url, review.review_id) == {grant_id: ReviewFlag.NON_STANDARD.value}


# --- decision C's boundary, exactly (criterion 2) -------------------------------


def test_an_expiry_exactly_at_the_stale_window_is_stale(
    access_db_url: str,  # noqa: F811
) -> None:
    """The inclusive edge: `expires_at == now + STALE_WINDOW` is stale.

    Catches a strict `<` comparison, which is invisible everywhere else — a
    grant expiring the very day the window closes is exactly the one a
    reviewer must be shown, and every other stale case in this file sits well
    inside the window and passes either way. The expiry is computed FROM
    `STALE_WINDOW`, so this stays the boundary if the constant ever moves.
    """
    scope = plant_scope(access_db_url, members=1)
    grant_id = plant_grant(
        access_db_url,
        user_id=scope.members[0],
        role_id=scope.template_role_id,
        expires_at=OPENED_AT + STALE_WINDOW,
    )

    review = open_one(access_db_url, department_id=scope.department_id)

    assert items_of(access_db_url, review.review_id) == {grant_id: ReviewFlag.STALE.value}


def test_an_expiry_one_second_past_the_stale_window_is_not_stale(
    access_db_url: str,  # noqa: F811
) -> None:
    """The other side of the same edge, one second out.

    Catches a `<=` that was written against a DATE rather than an instant, or
    a window widened by a day: without this row an implementation flagging
    everything with an expiry at all would pass every stale case above.
    """
    scope = plant_scope(access_db_url, members=1)
    grant_id = plant_grant(
        access_db_url,
        user_id=scope.members[0],
        role_id=scope.template_role_id,
        expires_at=OPENED_AT + STALE_WINDOW + timedelta(seconds=1),
    )

    review = open_one(access_db_url, department_id=scope.department_id)

    assert items_of(access_db_url, review.review_id) == {grant_id: ReviewFlag.OK.value}


def test_an_already_past_expiry_is_stale(
    access_db_url: str,  # noqa: F811
) -> None:
    """Decision C's "already-past expiry included".

    Catches a window implemented as the half-open interval
    `now < expires_at <= now + 14d`, which is the natural reading of "expiry
    within 14 days" and would silently drop every grant that has ALREADY
    lapsed — the seed plants exactly that state, and it is the oldest thing a
    reviewer needs to see.
    """
    scope = plant_scope(access_db_url, members=1)
    grant_id = plant_grant(
        access_db_url,
        user_id=scope.members[0],
        role_id=scope.template_role_id,
        expires_at=PAST_EXPIRY,
    )

    review = open_one(access_db_url, department_id=scope.department_id)

    assert items_of(access_db_url, review.review_id) == {grant_id: ReviewFlag.STALE.value}


def test_a_renewal_makes_neither_row_stale(
    access_db_url: str,  # noqa: F811
) -> None:
    """Decision C's second half: "no other live grant for the same (user, role)
    expires later".

    An earlier change leaves two live rows for one (user, role) reachable, which is how
    a renewal looks in this schema. A flagger that asks only "does THIS row
    expire soon" marks the old row stale and sends a reviewer to revoke access
    that was already renewed — the one false positive this rule exists to
    prevent. Both rows are asserted `ok`, so an implementation that clears the
    expiring row and forgets the renewing one fails too.
    """
    scope = plant_scope(access_db_url, members=1)
    member = scope.members[0]
    expiring = plant_grant(
        access_db_url,
        user_id=member,
        role_id=scope.template_role_id,
        expires_at=PAST_EXPIRY,
    )
    renewal = plant_grant(
        access_db_url,
        user_id=member,
        role_id=scope.template_role_id,
        expires_at=FAR_EXPIRY,
    )

    review = open_one(access_db_url, department_id=scope.department_id)

    assert items_of(access_db_url, review.review_id) == {
        expiring: ReviewFlag.OK.value,
        renewal: ReviewFlag.OK.value,
    }


# --- decision G: one open review per (scope, period) ----------------------------


def test_a_second_review_for_the_same_scope_and_period_is_refused_and_writes_nothing(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches a second open review being written, and a refusal that writes
    half of one.

    Two open reviews over one scope and period split a reviewer's worklist in
    two and let the same grant be decided twice, in opposite directions. The
    refusal is `ReviewAlreadyOpenError` (`POLICY_BLOCKED`), and the review
    count, the item count and the `review.opened` count are all re-read after
    it, so a handler that inserted the row and then noticed the duplicate
    fails here. The SAME scope with a DIFFERENT period is opened afterwards
    and must succeed, so a guard that refuses every second review of a scope
    (period ignored) cannot pass.
    """
    scope = plant_scope(access_db_url, members=1)
    plant_grant(access_db_url, user_id=scope.members[0], role_id=scope.template_role_id)
    repeated = period()
    first = open_one(access_db_url, department_id=scope.department_id, review_period=repeated)
    items_before = len(items_of(access_db_url, first.review_id))
    events_before = len(opened_events(access_db_url))

    with pytest.raises(ReviewAlreadyOpenError):
        open_one(access_db_url, department_id=scope.department_id, review_period=repeated)

    assert [str(row[0]) for row in reviews_with_period(access_db_url, repeated)] == [
        str(first.review_id)
    ]
    assert len(items_of(access_db_url, first.review_id)) == items_before
    assert len(opened_events(access_db_url)) == events_before

    later = open_one(access_db_url, department_id=scope.department_id)
    assert later.review_id != first.review_id


def test_two_concurrent_openers_leave_exactly_one_review(
    access_db_url: str,  # noqa: F811
) -> None:
    """Decision G's lock, on two real connections.

    The two callers send no idempotency key at this layer at all, so nothing
    but `SELECT … FOR UPDATE` on the department row can serialise them: without
    it both transactions read "no open review for this period", both insert,
    and the scope ends with two reviews and two `review.opened` events — a
    worklist split in two that no later call can repair. Exactly one success,
    exactly one `POLICY_BLOCKED`, and exactly one row are asserted, so a lock
    taken AFTER the duplicate check (which serialises nothing) fails here.
    """
    scope = plant_scope(access_db_url, members=1)
    plant_grant(access_db_url, user_id=scope.members[0], role_id=scope.template_role_id)
    contested = period()

    def attempt() -> str:
        try:
            open_one(access_db_url, department_id=scope.department_id, review_period=contested)
        except ReviewAlreadyOpenError:
            return "refused"
        return "opened"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [future.result() for future in [pool.submit(attempt), pool.submit(attempt)]]

    assert sorted(outcomes) == ["opened", "refused"], outcomes
    assert len(reviews_with_period(access_db_url, contested)) == 1
    assert len(reviews_for(access_db_url, department_id=scope.department_id)) == 1


# --- decision J: an empty scope -------------------------------------------------


def test_a_scope_with_no_active_grants_opens_a_review_with_zero_items(
    access_db_url: str,  # noqa: F811
) -> None:
    """Decision J: catches an empty scope being refused.

    "Nobody holds anything" is a true answer to a review, and refusing it
    would leave a department unreviewable precisely when it is clean — and
    would make the emptiness itself unrecordable, since decision A's scope
    columns are the only trace an item-less review leaves. The review row is
    re-read and asserted `open` with its department set, so a handler that
    returned early without writing anything fails too.
    """
    scope = plant_scope(access_db_url, members=1)

    review = open_one(access_db_url, department_id=scope.department_id)

    assert items_of(access_db_url, review.review_id) == {}
    rows = reviews_for(access_db_url, department_id=scope.department_id)
    assert [str(row[0]) for row in rows] == [str(review.review_id)]
    assert str(rows[0][2]) == ReviewStatus.OPEN.value


# --- the audit record (criterion 3, a project rule) ---------------------------


def test_review_opened_names_the_review_and_carries_exactly_seven_keys(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches an event whose subject is the department, the caller or the
    tool rather than the REVIEW, and one whose payload cannot answer what was
    reviewed.

    An auditor asking "what did review X cover" queries `subject_id`; an event
    subject naming the tool answers that question for no review in the tenant
    while a count-only assertion still passes. The payload
    keys are asserted as an exact set and every value is checked: the scope
    pair (one set, one null) is what tells a system review from a department
    one, `item_count` and `flag_counts` are the only record of what the
    reviewer was shown at the moment it was opened, and `reviewer_user_id`
    pins decision F — the reviewer is the DEPARTMENT'S MANAGER, not whoever
    called the tool, so a handler that stamped the caller fails here.
    """
    scope = plant_scope(access_db_url, members=1)
    member = scope.members[0]
    plant_grant(
        access_db_url, user_id=member, role_id=scope.template_role_id, expires_at=FAR_EXPIRY
    )
    plant_grant(access_db_url, user_id=member, role_id=scope.extra_role_id, expires_at=FAR_EXPIRY)
    plant_grant(
        access_db_url, user_id=member, role_id=scope.conflict_role_id, expires_at=PAST_EXPIRY
    )
    plant_grant(access_db_url, user_id=member, role_id=scope.partner_role_id, expires_at=FAR_EXPIRY)
    opener = ROWS.users_by_department["IT"][5].id
    used_period = period()
    events_before = len(opened_events(access_db_url))

    review = open_one(
        access_db_url,
        department_id=scope.department_id,
        review_period=used_period,
        opened_by_user_id=opener,
    )

    events = opened_events(access_db_url)
    assert len(events) == events_before + 1, "exactly one review.opened per review"
    subject_type, subject_id, payload = events[-1]
    assert str(subject_type) == SUBJECT_TYPE
    assert str(subject_id) == str(review.review_id)
    payload = dict(payload)
    assert set(payload) == EXPECTED_PAYLOAD_KEYS
    assert payload["review_id"] == str(review.review_id)
    assert payload["department_id"] == str(scope.department_id)
    assert payload["system_id"] is None
    assert payload["period"] == used_period
    assert payload["reviewer_user_id"] == str(scope.manager_id)
    assert review.reviewer_user_id == scope.manager_id, "decision F: the scope's own manager"
    assert opener != scope.manager_id, "premise: the caller is not the manager"
    assert payload["item_count"] == 4
    assert {flag: count for flag, count in dict(payload["flag_counts"]).items() if count} == {
        ReviewFlag.OK.value: 1,
        ReviewFlag.NON_STANDARD.value: 1,
        ReviewFlag.SOD_CONFLICT.value: 2,
    }


def test_the_review_its_items_and_its_event_roll_back_together(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches a handler that commits (or opens its own session for) the
    review, the items or the event.

    The caller fails AFTER `open_review` returned, so anything committed in
    its own transaction survives a rollback that must take it — leaving an
    open review nobody asked for, items pointing at it, or a `review.opened`
    recording a review that does not exist. The event is counted rather than
    assumed absent: the case above proves one is normally written, so a
    handler that never writes it fails there, not silently here.
    """
    scope = plant_scope(access_db_url, members=1)
    plant_grant(access_db_url, user_id=scope.members[0], role_id=scope.template_role_id)
    doomed = period()
    events_before = len(opened_events(access_db_url))

    open_then_raise(access_db_url, department_id=scope.department_id, review_period=doomed)

    assert reviews_with_period(access_db_url, doomed) == []
    assert reviews_for(access_db_url, department_id=scope.department_id) == []
    assert len(opened_events(access_db_url)) == events_before
    assert (
        _rows(
            access_db_url,
            "SELECT count(*) FROM review_items ri JOIN access_reviews ar ON ar.id = ri.review_id"
            " WHERE ar.period = :period",
            {"period": doomed},
        )[0][0]
        == 0
    )


# --- the item read (criterion 4) ------------------------------------------------


def read_items(
    url: str, *, review_id: uuid.UUID, limit: int, tenant_id: uuid.UUID | None = None
) -> list[uuid.UUID]:
    async def work(session: AsyncSession) -> list[uuid.UUID]:
        rows = await list_items(session, review_id, limit=limit)
        return [row.id for row in rows]

    return in_tenant(url, work, tenant_id=tenant_id)


def test_list_items_returns_this_reviews_items_and_honours_the_limit(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches a read that ignores `review_id` and returns the tenant's items,
    and one that ignores `limit`.

    A neighbouring review over a different scope is opened first, so a read
    keyed on the tenant alone comes back with too many rows; `limit=1` proves
    the bound is applied at all, since every other case in this file reads the
    items with SQL rather than through the tool.
    """
    scope = plant_scope(access_db_url, members=1)
    member = scope.members[0]
    plant_grant(access_db_url, user_id=member, role_id=scope.template_role_id)
    plant_grant(access_db_url, user_id=member, role_id=scope.extra_role_id)
    neighbour = plant_scope(access_db_url, members=1)
    plant_grant(access_db_url, user_id=neighbour.members[0], role_id=neighbour.template_role_id)
    other_review = open_one(access_db_url, department_id=neighbour.department_id)

    review = open_one(access_db_url, department_id=scope.department_id)

    expected = item_ids_of(access_db_url, review.review_id)
    assert len(expected) == 2
    assert set(read_items(access_db_url, review_id=review.review_id, limit=50)) == set(expected)
    assert len(read_items(access_db_url, review_id=review.review_id, limit=1)) == 1
    assert len(read_items(access_db_url, review_id=other_review.review_id, limit=50)) == 1


def test_another_tenants_review_items_are_invisible(
    access_db_url: str,  # noqa: F811
) -> None:
    """Decision B: catches `review_items` left a pure child of the review.

    Without its own `tenant_id` the model is not `TenantScoped`, so the ORM
    guard in `helpdesk.core.db` never filters it and the only protection is
    that every query happens to join its parent. This reads the items of a
    tenant-A review through a session bound to tenant B, where a passing
    implementation answers nothing: the id alone must not be a capability.
    The read is proved non-empty in the owning tenant first, so an
    implementation that returns nothing to anybody cannot pass.
    """
    scope = plant_scope(access_db_url, members=1)
    plant_grant(access_db_url, user_id=scope.members[0], role_id=scope.template_role_id)
    review = open_one(access_db_url, department_id=scope.department_id)

    assert len(read_items(access_db_url, review_id=review.review_id, limit=50)) == 1
    assert (
        read_items(
            access_db_url,
            review_id=review.review_id,
            limit=50,
            tenant_id=OTHER_TENANT_ROWS.tenant_id,
        )
        == []
    )
