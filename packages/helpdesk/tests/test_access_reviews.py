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
from helpdesk.access.lifecycle import OFFBOARD_REVOKE_REASON, TRANSFER_REVOKE_REASON
from helpdesk.access.reviews import (
    REVIEW_ITEM_DECIDED_EVENT_TYPE,
    REVIEW_OPENED_EVENT_TYPE,
    REVIEW_REVOKE_REASON,
    STALE_WINDOW,
    ItemAlreadyDecidedError,
    ReviewAlreadyOpenError,
    decide_item,
    find_item,
    list_items,
    list_items_with_context,
    list_reviews_for_scope,
    open_review,
)
from helpdesk.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.core.repository import ACTIVE_GRANT_STATUS, REVOKED_GRANT_STATUS, Repository
from helpdesk.errors import ErrorCode
from helpdesk.models import (
    Department,
    Grant,
    ReviewDecision,
    ReviewFlag,
    ReviewItem,
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


def test_a_scope_with_no_active_grants_opens_a_review_already_completed(
    access_db_url: str,  # noqa: F811
) -> None:
    """An earlier change decision F, rewritten from an earlier change's `open` expectation. Catches an item-less review left `open`.

    Completion is only ever written by deciding the LAST undecided item, so a
    review with zero items has no path to `completed` at all: left `open` it
    sits on row 21's reminder list for ever, and no call any principal can
    make will ever clear it. The status AND `completed_at` are both asserted,
    because a status flipped without the timestamp records a completion that
    happened at no time. Decision J is unchanged and still pinned here: the
    empty scope is opened rather than refused, with its department set, so a
    handler that returned early without writing anything fails too.
    """
    scope = plant_scope(access_db_url, members=1)

    review = open_one(access_db_url, department_id=scope.department_id, now=OPENED_AT)

    assert items_of(access_db_url, review.review_id) == {}
    rows = reviews_for(access_db_url, department_id=scope.department_id)
    assert [str(row[0]) for row in rows] == [str(review.review_id)]
    assert str(rows[0][2]) == ReviewStatus.COMPLETED.value
    assert review.status == ReviewStatus.COMPLETED.value
    assert review_row(access_db_url, review.review_id).completed_at == OPENED_AT


def test_a_scope_with_one_active_grant_still_opens_open(
    access_db_url: str,  # noqa: F811
) -> None:
    """The other direction of decision F, without which the case above is
    satisfied by a handler that writes every review `completed`.

    One undecided item exists, so the review has something to answer and must
    stay `open` with no `completed_at`. Deleting the "no items" condition and
    completing every review is the single change that flips this.
    """
    scope = plant_scope(access_db_url, members=1)
    plant_grant(access_db_url, user_id=scope.members[0], role_id=scope.template_role_id)

    review = open_one(access_db_url, department_id=scope.department_id)

    assert review.status == ReviewStatus.OPEN.value
    row = review_row(access_db_url, review.review_id)
    assert row.status == ReviewStatus.OPEN.value
    assert row.completed_at is None


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


# =================================================================================
# An earlier change, sections 1 and 6: `decide_item`, the keep-or-revoke half.
#
# Surface under test (written before it exists):
#
# ``helpdesk.access.reviews``
#     ``REVIEW_ITEM_DECIDED_EVENT_TYPE = "review.item_decided"`` (the design,
#     decision G).
#     ``REVIEW_REVOKE_REASON`` — the one fixed sentence a review's revocation
#     writes to `grants.revoke_reason` (decision E).
#     ``ItemAlreadyDecidedError(ToolError)`` — `code = POLICY_BLOCKED`
#     (decision C).
#     ``async def find_item(session, item_id) -> ReviewItem | None`` — the
#     bound tenant's item, through the ORM.
#     ``async def decide_item(session, directory, *, item, decision,
#     decided_by_user_id, actor_ref=None, session_id=None, now)
#     -> DecideItemResult`` — locks the item's GRANT row
#     (`core.repository.lock_grant_row`, decision I), refuses a second
#     decision, writes `decision` and `decided_at`, revokes that ONE row
#     through `access.revoke.revoke_grant_row` for `revoke`, completes the
#     review when nothing is left undecided, and appends one
#     `review.item_decided`. All in the caller's transaction.
#
# The result carries `.item`, `.review`, `.revoked_grant_count` and
# `.review_complete`.
#
# Namespace: the same seeded session-scoped database, the
# same freshly planted scope per case, and unique periods; nothing here is
# rebuilt.
# =================================================================================

# The decider, an IT user no other case in this file opens a review as.
DECIDER = ROWS.users_by_department["IT"][6]

# The revocation event's name as a LITERAL: this file must fail when the
# constant it is written against is renamed.
GRANT_REVOKED_EVENT = "grant.revoked"


@dataclass(frozen=True)
class DecisionFacts:
    """What one `decide_item` returned, copied out before its session closes."""

    item_id: uuid.UUID
    decision: str | None
    decided_at: datetime | None
    revoked_grant_count: int
    review_complete: bool
    review_status: str


@dataclass(frozen=True)
class ReviewRow:
    status: str
    completed_at: datetime | None


@dataclass(frozen=True)
class ItemRow:
    item_id: uuid.UUID
    grant_id: uuid.UUID
    flag: str
    decision: str | None
    decided_at: datetime | None


@dataclass(frozen=True)
class GrantRow:
    status: str
    revoked_at: datetime | None
    revoke_reason: str | None


def _execute(url: str, sql: str, params: dict[str, Any]) -> None:
    """One committed statement written AROUND the surface, never through it."""

    async def scenario() -> None:
        engine = create_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(text(sql), params)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def review_row(url: str, review_id: uuid.UUID) -> ReviewRow:
    rows = _rows(
        url,
        "SELECT status, completed_at FROM access_reviews WHERE id = :review",
        {"review": review_id},
    )
    assert len(rows) == 1, f"expected exactly one review row, got {len(rows)}"
    return ReviewRow(status=str(rows[0][0]), completed_at=rows[0][1])


def item_rows(url: str, review_id: uuid.UUID) -> list[ItemRow]:
    """Every item of one review, in the order they were written."""
    return [
        ItemRow(
            item_id=uuid.UUID(str(row[0])),
            grant_id=uuid.UUID(str(row[1])),
            flag=str(row[2]),
            decision=None if row[3] is None else str(row[3]),
            decided_at=row[4],
        )
        for row in _rows(
            url,
            "SELECT id, grant_id, flag, decision, decided_at FROM review_items"
            " WHERE review_id = :review ORDER BY created_at, id",
            {"review": review_id},
        )
    ]


def grant_row(url: str, grant_id: uuid.UUID) -> GrantRow:
    rows = _rows(
        url,
        "SELECT status, revoked_at, revoke_reason FROM grants WHERE id = :grant",
        {"grant": grant_id},
    )
    assert len(rows) == 1, f"expected exactly one grant row, got {len(rows)}"
    return GrantRow(status=str(rows[0][0]), revoked_at=rows[0][1], revoke_reason=rows[0][2])


def decided_events(url: str) -> list[Any]:
    return _rows(
        url,
        "SELECT subject_type, subject_id, payload FROM audit_events"
        " WHERE tenant_id = :tenant AND event_type = :event_type ORDER BY seq",
        {"tenant": ROWS.tenant_id, "event_type": REVIEW_ITEM_DECIDED_EVENT_TYPE},
    )


def revoked_events(url: str) -> list[Any]:
    return _rows(
        url,
        "SELECT subject_id, payload FROM audit_events"
        " WHERE tenant_id = :tenant AND event_type = :event_type ORDER BY seq",
        {"tenant": ROWS.tenant_id, "event_type": GRANT_REVOKED_EVENT},
    )


def decide_one(
    url: str,
    *,
    item_id: uuid.UUID,
    decision: ReviewDecision,
    now: datetime = OPENED_AT,
) -> DecisionFacts:
    """One `decide_item` in one committed transaction, item read by `find_item`."""

    async def work(session: AsyncSession) -> DecisionFacts:
        item = await find_item(session, item_id)
        assert item is not None, "premise: the item resolves in the bound tenant"
        result = await decide_item(
            session,
            SimulatedDirectory(session),
            item=item,
            decision=decision,
            decided_by_user_id=DECIDER.id,
            actor_ref=str(DECIDER.id),
            session_id=None,
            now=now,
        )
        return DecisionFacts(
            item_id=result.item.id,
            decision=result.item.decision,
            decided_at=result.item.decided_at,
            revoked_grant_count=result.revoked_grant_count,
            review_complete=result.review_complete,
            review_status=result.review.status,
        )

    return in_tenant(url, work)


def decide_then_raise(url: str, *, item_id: uuid.UUID) -> None:
    """Decide `revoke`, then fail the enclosing transaction."""

    async def work(session: AsyncSession) -> None:
        item = await find_item(session, item_id)
        assert item is not None, "premise: the item resolves in the bound tenant"
        result = await decide_item(
            session,
            SimulatedDirectory(session),
            item=item,
            decision=ReviewDecision.REVOKE,
            decided_by_user_id=DECIDER.id,
            actor_ref=str(DECIDER.id),
            session_id=None,
            now=OPENED_AT,
        )
        assert result.revoked_grant_count == 1, (
            "premise: the grant was revoked before the caller failed"
        )
        raise _Boom("the caller failed after the decision was written")

    with contextlib.suppress(_Boom):
        in_tenant(url, work)


def open_with_grants(
    url: str, *, expires_at: datetime | None = FAR_EXPIRY, count: int = 1
) -> tuple[Scope, ReviewFacts, list[ItemRow]]:
    """A planted scope holding `count` active grants, and the review over it."""
    scope = plant_scope(url, members=1)
    roles = (scope.template_role_id, scope.extra_role_id, scope.other_system_role_id)
    assert count <= len(roles), "this helper plants at most one grant per planted role"
    for role_id in roles[:count]:
        plant_grant(url, user_id=scope.members[0], role_id=role_id, expires_at=expires_at)
    review = open_one(url, department_id=scope.department_id)
    items = item_rows(url, review.review_id)
    assert len(items) == count, "premise: one item per planted grant"
    return scope, review, items


# --- the constants --------------------------------------------


def test_the_decide_constants_and_error_code_are_exactly_the_spec_values() -> None:
    """Pins every 12e value this file otherwise reads as a symbol.

    Catches the event type renamed, which leaves every query in this file
    green (they all read the constant) and the audit log unqueryable.
    Catches `ItemAlreadyDecidedError` carrying `NOT_FOUND` — telling a caller
    who legitimately reached the item that it does not exist — or inheriting
    `ToolError`'s default `INTERNAL`, filing a refused re-decision as an
    outage.

    `REVIEW_REVOKE_REASON` is asserted byte for byte AND against the other
    two revocation reasons in the codebase (decision E): the reason is the
    only thing on the row and on the event that tells an auditor a review
    ended this access rather than a transfer, an offboarding or a manual
    `revoke_access`, so a reason equal to (or containing) one of the others
    destroys exactly the distinction it exists for.
    """
    assert REVIEW_ITEM_DECIDED_EVENT_TYPE == "review.item_decided"
    assert REVIEW_REVOKE_REASON == "revoked by an access review decision"
    assert REVIEW_REVOKE_REASON not in {TRANSFER_REVOKE_REASON, OFFBOARD_REVOKE_REASON}
    assert ItemAlreadyDecidedError.code is ErrorCode.POLICY_BLOCKED
    assert ItemAlreadyDecidedError.code.value == "POLICY_BLOCKED"


# --- `keep`: a record and nothing else (criterion 3, decision D) -----------------


def test_keep_records_the_decision_and_touches_no_grant_and_no_other_item(
    access_db_url: str,  # noqa: F811
) -> None:
    """Decision D. Catches a `keep` that extends the grant's expiry, revokes
    anything, or stamps every item of the review instead of the one named.

    The review holds TWO items, and only the first is decided: the second is
    re-read and must still carry a null `decision` and a null `decided_at`,
    so an update keyed on the REVIEW rather than on the item fails here while
    a single-item case would pass it. Both grant rows are re-read whole
    (status, `revoked_at`, `revoke_reason`) and the tenant's `grant.revoked`
    count is unchanged, so "keep changes no access at all" is asserted about
    the store and not about the return value. `decided_at` is the exact
    instant the caller passed, so a decider reading its own clock fails.
    """
    _scope, review, items = open_with_grants(access_db_url, count=2)
    revocations_before = len(revoked_events(access_db_url))

    facts = decide_one(access_db_url, item_id=items[0].item_id, decision=ReviewDecision.KEEP)

    assert facts.decision == ReviewDecision.KEEP.value
    assert facts.decided_at == OPENED_AT
    assert facts.revoked_grant_count == 0
    after = item_rows(access_db_url, review.review_id)
    assert after[0].decision == ReviewDecision.KEEP.value
    assert after[0].decided_at == OPENED_AT
    assert after[1].decision is None, "the item nobody decided must stay undecided"
    assert after[1].decided_at is None
    for item in after:
        row = grant_row(access_db_url, item.grant_id)
        assert row.status == ACTIVE_GRANT_STATUS
        assert row.revoked_at is None
        assert row.revoke_reason is None
    assert len(revoked_events(access_db_url)) == revocations_before


# --- `revoke`: the one path from a review to a change (criteria 2 and 4) ---------


def test_revoke_ends_the_items_grant_with_the_review_reason_and_one_event(
    access_db_url: str,  # noqa: F811
) -> None:
    """Criterion 2. Catches a `revoke` that records the decision and leaves
    the access alive, and one that revokes with a reason an auditor cannot
    tell from a manual `revoke_access`.

    Every column that says a grant is gone is asserted as an exact value —
    `status`, `revoked_at` against the instant the caller passed, and
    `revoke_reason` byte for byte against `REVIEW_REVOKE_REASON` — because
    `list_active_grants_for_user` reads two of the three, so a revocation
    writing only `revoked_at` looks correct to every reader while the row
    still says `active`. Exactly ONE `grant.revoked` is appended, naming that
    grant as its subject: the review's unit is one row (decision A), so a
    decide path that fanned out over the (user, role) live set would end a
    neighbouring grant nobody reviewed. The SECOND item's grant is re-read
    unchanged, which is what pins that fan-out.
    """
    _scope, review, items = open_with_grants(access_db_url, count=2)
    revocations_before = len(revoked_events(access_db_url))

    facts = decide_one(access_db_url, item_id=items[0].item_id, decision=ReviewDecision.REVOKE)

    assert facts.decision == ReviewDecision.REVOKE.value
    assert facts.revoked_grant_count == 1
    revoked = grant_row(access_db_url, items[0].grant_id)
    assert revoked.status == REVOKED_GRANT_STATUS
    assert revoked.revoked_at == OPENED_AT
    assert revoked.revoke_reason == REVIEW_REVOKE_REASON
    untouched = grant_row(access_db_url, items[1].grant_id)
    assert untouched.status == ACTIVE_GRANT_STATUS
    assert untouched.revoked_at is None
    events = revoked_events(access_db_url)
    assert len(events) == revocations_before + 1, "one grant.revoked, for one row"
    assert str(events[-1][0]) == str(items[0].grant_id)
    assert dict(events[-1][1])["reason"] == REVIEW_REVOKE_REASON
    assert item_rows(access_db_url, review.review_id)[1].decision is None


def test_a_stale_items_grant_is_revoked_although_no_live_set_holds_it(
    access_db_url: str,  # noqa: F811
) -> None:
    """Criterion 4 and decision A, the case the whole extraction exists for.

    The grant is `active` but its expiry has already gone by, so an earlier change's
    `lock_live_grants_for_role` returns nothing for its (user, role) and
    `revoke_role_access` would refuse it `POLICY_BLOCKED` — leaving the most
    reviewable item in the tenant the one item a reviewer cannot revoke, and
    a review nobody can complete. Routing the decide path through 11d
    unchanged (decision A option ii) is the single change that fails this:
    every other case here plants a grant with a distant expiry and passes
    either way.

    The item's flag is asserted `stale` as a premise, so a fixture that
    stopped producing the case cannot leave this green.
    """
    _scope, review, items = open_with_grants(access_db_url, expires_at=PAST_EXPIRY, count=1)
    assert items[0].flag == ReviewFlag.STALE.value, "premise: this is decision A's row"
    revocations_before = len(revoked_events(access_db_url))

    facts = decide_one(access_db_url, item_id=items[0].item_id, decision=ReviewDecision.REVOKE)

    assert facts.revoked_grant_count == 1
    row = grant_row(access_db_url, items[0].grant_id)
    assert row.status == REVOKED_GRANT_STATUS
    assert row.revoke_reason == REVIEW_REVOKE_REASON
    assert len(revoked_events(access_db_url)) == revocations_before + 1
    assert review_row(access_db_url, review.review_id).status == ReviewStatus.COMPLETED.value


# --- decision B: the grant went away while the review waited ---------------------


def test_a_grant_revoked_before_the_decision_is_recorded_and_revokes_nothing(
    access_db_url: str,  # noqa: F811
) -> None:
    """Decision B. Catches a decide path that refuses an item whose grant is
    already gone, and one that revokes it a second time.

    Refusing leaves an item nobody can decide and therefore a review nobody
    can complete, and the reviewer's only remaining move would be `keep`,
    which would record a lie. Revoking again would rewrite `revoke_reason`
    and `revoked_at` and erase the record of what actually ended the access —
    both are re-read against the HISTORIC values written around the tool, so
    a second revocation fails on the reason rather than on a count. The
    reported `revoked_grant_count` is `0`, which is the only thing that keeps
    the audit record honest about a decision that ended nothing.
    """
    _scope, review, items = open_with_grants(access_db_url, count=1)
    historic_at = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
    historic_reason = "fake historical revocation, must not be rewritten"
    _execute(
        access_db_url,
        "UPDATE grants SET status = :status, revoked_at = :at, revoke_reason = :reason"
        " WHERE id = :grant",
        {
            "status": REVOKED_GRANT_STATUS,
            "at": historic_at,
            "reason": historic_reason,
            "grant": items[0].grant_id,
        },
    )
    revocations_before = len(revoked_events(access_db_url))

    facts = decide_one(access_db_url, item_id=items[0].item_id, decision=ReviewDecision.REVOKE)

    assert facts.decision == ReviewDecision.REVOKE.value
    assert facts.revoked_grant_count == 0
    assert item_rows(access_db_url, review.review_id)[0].decision == ReviewDecision.REVOKE.value
    row = grant_row(access_db_url, items[0].grant_id)
    assert row.revoked_at == historic_at
    assert row.revoke_reason == historic_reason
    assert len(revoked_events(access_db_url)) == revocations_before


# --- decision C: a decision is written once --------------------------------------


def test_a_second_decision_on_a_decided_item_is_refused_and_writes_nothing(
    access_db_url: str,  # noqa: F811
) -> None:
    """Decision C. Catches an overwrite, which destroys the only thing the
    table is for: what a named person decided at a named instant.

    The second call names the OPPOSITE decision, so an implementation that
    overwrites is visible in the stored word and not only in a timestamp, and
    the grant is re-read `active` afterwards, so an overwrite that also
    revoked the access fails on the row as well as on the item. The refusal
    is `ItemAlreadyDecidedError` (`POLICY_BLOCKED`) rather than a silent
    no-op: a reviewer who believes their revoke landed must be told it did
    not.
    """
    _scope, review, items = open_with_grants(access_db_url, count=1)
    first = decide_one(access_db_url, item_id=items[0].item_id, decision=ReviewDecision.KEEP)
    assert first.decision == ReviewDecision.KEEP.value
    revocations_before = len(revoked_events(access_db_url))
    events_before = len(decided_events(access_db_url))

    with pytest.raises(ItemAlreadyDecidedError):
        decide_one(access_db_url, item_id=items[0].item_id, decision=ReviewDecision.REVOKE)

    stored = item_rows(access_db_url, review.review_id)[0]
    assert stored.decision == ReviewDecision.KEEP.value
    assert stored.decided_at == OPENED_AT
    assert grant_row(access_db_url, items[0].grant_id).status == ACTIVE_GRANT_STATUS
    assert len(revoked_events(access_db_url)) == revocations_before
    assert len(decided_events(access_db_url)) == events_before


def test_two_concurrent_deciders_leave_one_decided_item_and_one_refusal(
    access_db_url: str,  # noqa: F811
) -> None:
    """Decision I's lock, on two real connections.

    Both callers ask to REVOKE the same item and neither sends an idempotency
    key at this layer, so nothing but `SELECT … FOR UPDATE` on the item's
    grant row can serialise them: without it both read an undecided item,
    both pass decision C's check, and the grant is revoked twice — two
    `grant.revoked` events for one row, the second rewriting the first's
    `revoked_at`. Exactly one success, exactly one `POLICY_BLOCKED` and
    exactly one revocation event are asserted, so a lock taken AFTER the
    already-decided check (which serialises nothing) fails here.
    """
    _scope, review, items = open_with_grants(access_db_url, count=1)
    revocations_before = len(revoked_events(access_db_url))

    def attempt() -> str:
        try:
            decide_one(access_db_url, item_id=items[0].item_id, decision=ReviewDecision.REVOKE)
        except ItemAlreadyDecidedError:
            return "refused"
        return "decided"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [future.result() for future in [pool.submit(attempt), pool.submit(attempt)]]

    assert sorted(outcomes) == ["decided", "refused"], outcomes
    assert item_rows(access_db_url, review.review_id)[0].decision == ReviewDecision.REVOKE.value
    assert len(revoked_events(access_db_url)) == revocations_before + 1
    assert grant_row(access_db_url, items[0].grant_id).revoked_at == OPENED_AT


# --- completion (criterion 6, decision F) ----------------------------------------


def test_the_last_undecided_item_completes_the_review_and_an_earlier_one_does_not(
    access_db_url: str,  # noqa: F811
) -> None:
    """Criterion 6, both directions in one review.

    Catches a decide that completes on the FIRST item — which would close a
    reviewer's worklist while items they never saw are still undecided — and
    one that never completes at all, leaving every review on row 21's
    reminder list for ever. `review_complete` is asserted on BOTH results,
    because it is the only place the flag is observable before the event, and
    `completed_at` is asserted null after the first decision and exactly the
    caller's instant after the second: a status flipped without the timestamp
    records a completion that happened at no time.
    """
    _scope, review, items = open_with_grants(access_db_url, count=2)

    first = decide_one(access_db_url, item_id=items[0].item_id, decision=ReviewDecision.KEEP)

    assert first.review_complete is False
    assert first.review_status == ReviewStatus.OPEN.value
    midway = review_row(access_db_url, review.review_id)
    assert midway.status == ReviewStatus.OPEN.value
    assert midway.completed_at is None

    last = decide_one(access_db_url, item_id=items[1].item_id, decision=ReviewDecision.KEEP)

    assert last.review_complete is True
    assert last.review_status == ReviewStatus.COMPLETED.value
    closed = review_row(access_db_url, review.review_id)
    assert closed.status == ReviewStatus.COMPLETED.value
    assert closed.completed_at == OPENED_AT


def test_a_neighbouring_reviews_undecided_item_does_not_hold_this_one_open(
    access_db_url: str,  # noqa: F811
) -> None:
    """The completion check is per REVIEW, not per tenant.

    A second review over a different scope is opened first and left entirely
    undecided, so a completion rule that asked "are any items in this tenant
    undecided" would leave this review `open` after its last item was
    answered — and would complete the neighbour the moment somebody else
    finished theirs. Nothing else in this file plants two live reviews at
    once, so no other case can fail on it.
    """
    _neighbour, neighbour_review, neighbour_items = open_with_grants(access_db_url, count=1)
    _scope, review, items = open_with_grants(access_db_url, count=1)

    last = decide_one(access_db_url, item_id=items[0].item_id, decision=ReviewDecision.KEEP)

    assert last.review_complete is True
    assert review_row(access_db_url, review.review_id).status == ReviewStatus.COMPLETED.value
    assert review_row(access_db_url, neighbour_review.review_id).status == ReviewStatus.OPEN.value
    assert item_rows(access_db_url, neighbour_review.review_id)[0].decision is None
    assert neighbour_items[0].decision is None


# --- the audit record (decision G, a project rule) -----------------------------


def test_review_item_decided_names_the_item_and_carries_decision_gs_keys(
    access_db_url: str,  # noqa: F811
) -> None:
    """a project rule. Catches an event whose subject is the review, the
    grant or the tool rather than the ITEM, and one whose payload cannot
    answer what the reviewer did.

    An auditor asking "what happened to item X" queries `subject_id`; an
    event naming the review answers that question for no item in the tenant
    while a count-only assertion still passes. The payload keys are asserted
    as an exact set and every value is checked, because each one answers a
    question nothing else in the log can: `grant_id`, `user_id` and `role_id`
    say WHOSE access this was (the item row alone names only a grant),
    `flag` says why it was in front of a reviewer, `revoked_grant_count`
    distinguishes decision B's "revoke that ended nothing" from a real
    revocation, and `review_complete` is the ONLY record that this decision
    closed the review — there is no `review.completed` event.
    """
    _scope, review, items = open_with_grants(access_db_url, count=2)
    events_before = len(decided_events(access_db_url))
    scope_item = item_rows(access_db_url, review.review_id)[0]

    decide_one(access_db_url, item_id=items[0].item_id, decision=ReviewDecision.REVOKE)

    events = decided_events(access_db_url)
    assert len(events) == events_before + 1, "exactly one review.item_decided per decision"
    subject_type, subject_id, payload = events[-1]
    assert str(subject_type) == "review_item"
    assert str(subject_id) == str(items[0].item_id)
    payload = dict(payload)
    assert set(payload) == {
        "review_id",
        "item_id",
        "grant_id",
        "user_id",
        "role_id",
        "flag",
        "decision",
        "revoked_grant_count",
        "review_complete",
    }
    assert payload["review_id"] == str(review.review_id)
    assert payload["item_id"] == str(items[0].item_id)
    assert payload["grant_id"] == str(items[0].grant_id)
    assert payload["flag"] == scope_item.flag
    assert payload["decision"] == ReviewDecision.REVOKE.value
    assert payload["revoked_grant_count"] == 1
    assert payload["review_complete"] is False, "one item is still undecided"

    decide_one(access_db_url, item_id=items[1].item_id, decision=ReviewDecision.KEEP)

    closing = dict(decided_events(access_db_url)[-1][2])
    assert closing["review_complete"] is True
    assert closing["revoked_grant_count"] == 0


def test_the_decision_the_revocation_and_the_event_roll_back_together(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches a decide path that commits (or opens its own session for) the
    item update, the revocation or the event.

    The caller fails AFTER `decide_item` returned, so anything committed in
    its own transaction survives a rollback that must take it — an item
    recorded as revoked whose access is still live, a revoked grant nobody
    can point at a decision for, or a `review.item_decided` describing a
    decision that does not exist. Each of the three is re-read; the two event
    counts are safe to assert as unchanged because the cases above prove both
    are normally written, so an implementation that never writes them fails
    there rather than silently here.
    """
    _scope, review, items = open_with_grants(access_db_url, count=1)
    decisions_before = len(decided_events(access_db_url))
    revocations_before = len(revoked_events(access_db_url))

    decide_then_raise(access_db_url, item_id=items[0].item_id)

    stored = item_rows(access_db_url, review.review_id)[0]
    assert stored.decision is None
    assert stored.decided_at is None
    grant = grant_row(access_db_url, items[0].grant_id)
    assert grant.status == ACTIVE_GRANT_STATUS
    assert grant.revoked_at is None
    assert review_row(access_db_url, review.review_id).status == ReviewStatus.OPEN.value
    assert len(decided_events(access_db_url)) == decisions_before
    assert len(revoked_events(access_db_url)) == revocations_before


# --- `find_item` (the handler's existence half) ----------------------------------


def test_find_item_answers_nothing_for_another_tenants_item(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches `find_item` reading `review_items` outside the tenant guard.

    The id alone must not be a capability: a session bound to another tenant
    must answer exactly as it does for an id that never existed, which is
    what lets the caller turn both into ONE `NOT_FOUND`. The same read is
    proved non-empty in the owning tenant first, so an implementation that
    answers None to everybody cannot pass.
    """
    _scope, _review, items = open_with_grants(access_db_url, count=1)

    async def work(session: AsyncSession) -> uuid.UUID | None:
        found = await find_item(session, items[0].item_id)
        return None if found is None else found.id

    async def missing(session: AsyncSession) -> uuid.UUID | None:
        found = await find_item(session, uuid.uuid4())
        return None if found is None else found.id

    assert in_tenant(access_db_url, work) == items[0].item_id
    assert in_tenant(access_db_url, work, tenant_id=OTHER_TENANT_ROWS.tenant_id) is None
    assert in_tenant(access_db_url, missing) is None


# =================================================================================
# An earlier change, sections 1 and 6: the queue read and the widened item read.
#
# Surface under test (written before it exists):
#
# ``helpdesk.access.reviews``
#     ``async def list_reviews_for_scope(session, *, department_id, system_ids,
#     any_scope, status, limit) -> Sequence[ReviewWithCounts]`` — the reviews one
#     caller's the design scope reaches, `due_at` oldest first with the review id
#     breaking a tie, at most `limit` of them. The scope arrives DECIDED (a
#     department id, a set of owned system ids, or `any_scope=True`): nothing in
#     `helpdesk/access/` reads a token, exactly as `list_items` takes no scope.
#     Each answered row carries ``.review`` (the ORM row), ``.department_name``,
#     ``.system_name``, ``.reviewer_display_name``, ``.item_count`` and
#     ``.decided_count``, the last two from ONE grouped aggregate over
#     `review_items` (decision D).
#     ``async def list_items_with_context(session, review_id, *, limit) ->
#     Sequence[ReviewItemWithContext]`` — 23c's widened twin: ``.item`` plus the
#     grant's ``.target_display_name``, ``.role_name``, ``.system_name``,
#     ``.privilege``, ``.granted_at`` and ``.expires_at``, off one joined read.
#     ``list_items`` becomes a pure PROJECTION of it, so the
#     cases above this line keep unpacking `Sequence[ReviewItem]`.
#
# a project rule: the same session-scoped seeded database, the same per-test
# namespace (a freshly planted department, system, cast and period). The rows
# these cases read are planted with RAW SQL rather than opened through
# `open_review`, because the queue's contract is about `due_at`, `status` and
# the item counts — three columns no tool sets today, and ids the ordering case
# has to choose rather than discover.
# =================================================================================


DUE_EARLY = datetime(2026, 4, 1, 9, 0, tzinfo=UTC)
DUE_LATE = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
COMPLETED_AT = datetime(2026, 5, 5, 9, 0, tzinfo=UTC)


def plant_review(
    url: str,
    *,
    reviewer_user_id: uuid.UUID,
    review_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    department_id: uuid.UUID | None = None,
    system_id: uuid.UUID | None = None,
    status: str = ReviewStatus.OPEN.value,
    due_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> uuid.UUID:
    """One committed `access_reviews` row, written around the engine."""
    new_id = uuid.uuid4() if review_id is None else review_id
    _execute(
        url,
        "INSERT INTO access_reviews (id, tenant_id, reviewer_user_id, department_id, system_id,"
        " period, status, due_at, completed_at, created_at, updated_at)"
        " VALUES (:id, :tenant, :reviewer, :department, :system, :period, :status, :due_at,"
        " :completed_at, now(), now())",
        {
            "id": new_id,
            "tenant": ROWS.tenant_id if tenant_id is None else tenant_id,
            "reviewer": reviewer_user_id,
            "department": department_id,
            "system": system_id,
            "period": period(),
            "status": status,
            # `datetime` objects, never strings: asyncpg binds by type.
            "due_at": due_at,
            "completed_at": completed_at,
        },
    )
    return new_id


def plant_item(
    url: str,
    *,
    review_id: uuid.UUID,
    grant_id: uuid.UUID,
    tenant_id: uuid.UUID | None = None,
    decision: str | None = None,
) -> uuid.UUID:
    """One committed `review_items` row, written around the engine."""
    item_id = uuid.uuid4()
    _execute(
        url,
        "INSERT INTO review_items (id, tenant_id, review_id, grant_id, flag, decision,"
        " decided_at, created_at, updated_at)"
        " VALUES (:id, :tenant, :review, :grant, :flag, :decision, :decided_at, now(), now())",
        {
            "id": item_id,
            "tenant": ROWS.tenant_id if tenant_id is None else tenant_id,
            "review": review_id,
            "grant": grant_id,
            "flag": ReviewFlag.OK.value,
            "decision": decision,
            "decided_at": None if decision is None else OPENED_AT,
        },
    )
    return item_id


def _other_tenant_grant_id(url: str) -> uuid.UUID:
    """An active grant the seed already wrote in the OTHER tenant."""
    rows = _rows(
        url,
        "SELECT id FROM grants WHERE tenant_id = :tenant AND status = :status ORDER BY id LIMIT 1",
        {"tenant": OTHER_TENANT_ROWS.tenant_id, "status": ACTIVE_GRANT_STATUS},
    )
    assert rows, "premise: the seed leaves at least one live grant in the other tenant"
    return uuid.UUID(str(rows[0][0]))


@dataclass(frozen=True)
class QueueRow:
    """One answered queue row, copied out before its session closes."""

    review_id: uuid.UUID
    status: str
    due_at: datetime | None
    completed_at: datetime | None
    department_id: uuid.UUID | None
    department_name: str | None
    system_id: uuid.UUID | None
    system_name: str | None
    reviewer_user_id: uuid.UUID
    reviewer_display_name: str
    item_count: int
    decided_count: int


def read_queue(
    url: str,
    *,
    department_id: uuid.UUID | None = None,
    system_ids: frozenset[uuid.UUID] = frozenset(),
    any_scope: bool = False,
    status: ReviewStatus | None = None,
    limit: int = 50,
    tenant_id: uuid.UUID | None = None,
) -> list[QueueRow]:
    """The queue one resolved scope answers, as plain values."""

    async def work(session: AsyncSession) -> list[QueueRow]:
        rows = await list_reviews_for_scope(
            session,
            department_id=department_id,
            system_ids=system_ids,
            any_scope=any_scope,
            status=status,
            limit=limit,
        )
        return [
            QueueRow(
                review_id=row.review.id,
                status=row.review.status,
                due_at=row.review.due_at,
                completed_at=row.review.completed_at,
                department_id=row.review.department_id,
                department_name=row.department_name,
                system_id=row.review.system_id,
                system_name=row.system_name,
                reviewer_user_id=row.review.reviewer_user_id,
                reviewer_display_name=row.reviewer_display_name,
                item_count=row.item_count,
                decided_count=row.decided_count,
            )
            for row in rows
        ]

    return in_tenant(url, work, tenant_id=tenant_id)


# --- what a resolved scope answers (criterion 2) ---------------------------------


def test_a_department_scope_answers_its_own_reviews_and_not_another_departments(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches a read that ignores the department it was handed and answers the
    tenant's reviews.

    Both departments are real rows in the CALLER's own tenant and both reviews
    are `open`, so neither the tenant guard nor the status filter can hide the
    second one: dropping the department predicate is the only change that puts
    it in the answer. The own-department review is asserted PRESENT in the same
    case, so a read that answers nothing to everybody fails too.
    """
    scope = plant_scope(access_db_url, members=1)
    other = plant_scope(access_db_url, members=1)
    mine = plant_review(
        access_db_url, reviewer_user_id=scope.manager_id, department_id=scope.department_id
    )
    theirs = plant_review(
        access_db_url, reviewer_user_id=other.manager_id, department_id=other.department_id
    )

    answered = [
        row.review_id for row in read_queue(access_db_url, department_id=scope.department_id)
    ]

    assert mine in answered
    assert theirs not in answered


def test_a_system_scope_answers_only_the_owned_systems_reviews(
    access_db_url: str,  # noqa: F811
) -> None:
    """The `own_systems` half, which the department case cannot reach: catches a
    read that applies the department predicate and leaves the system one
    answering True for every system.

    Both systems exist in the caller's own tenant and both reviews are `open`,
    so only the owned-system predicate can separate them. The owned review is
    asserted present in the same case, so a read answering nothing cannot pass.
    """
    scope = plant_scope(access_db_url, members=1)
    other = plant_scope(access_db_url, members=1)
    mine = plant_review(access_db_url, reviewer_user_id=scope.owner_id, system_id=scope.system_id)
    theirs = plant_review(access_db_url, reviewer_user_id=other.owner_id, system_id=other.system_id)

    answered = [
        row.review_id for row in read_queue(access_db_url, system_ids=frozenset({scope.system_id}))
    ]

    assert mine in answered
    assert theirs not in answered


def test_an_any_scope_answers_both_the_department_and_the_system_review(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches the `any` caller falling through the two narrow predicates into
    an empty answer — the `if not predicates: return []` shape an earlier change's queue
    read uses for a caller whose scopes resolve to nothing.

    `any_scope` is handed no department and no system ids at all, so a read that
    builds its WHERE clause only from those two arguments answers nothing here
    while passing both cases above; a read that honours `any_scope` answers both
    reviews, which are deliberately of DIFFERENT scope kinds.
    """
    scope = plant_scope(access_db_url, members=1)
    # An explicit `due_at` on both: they then sort ahead of every NULL-`due_at`
    # review the rest of this file opens, so an `any`-scope page cannot depend
    # on how many reviews the suite happened to write first.
    departmental = plant_review(
        access_db_url,
        reviewer_user_id=scope.manager_id,
        department_id=scope.department_id,
        due_at=DUE_EARLY,
    )
    systemic = plant_review(
        access_db_url,
        reviewer_user_id=scope.owner_id,
        system_id=scope.system_id,
        due_at=DUE_EARLY,
    )

    answered = [row.review_id for row in read_queue(access_db_url, any_scope=True, limit=200)]

    assert departmental in answered
    assert systemic in answered


def test_the_queue_orders_by_due_at_oldest_first_with_the_review_id_breaking_the_tie(
    access_db_url: str,  # noqa: F811
) -> None:
    """Criterion 2's order as an exact sequence, both halves of it.

    The two tied reviews share one `due_at` and are INSERTED highest-id first,
    so a read ordered by `created_at` (which is what `list_items` orders by, the
    obvious copy) answers them the other way round and fails; the third review
    is inserted LAST with the earliest `due_at`, so a read ordered by the id
    alone — or by insertion — puts it in the wrong place. Only "due_at, then
    id" answers this sequence.
    """
    scope = plant_scope(access_db_url, members=1)
    low, high = sorted([uuid.uuid4(), uuid.uuid4()])
    plant_review(
        access_db_url,
        review_id=high,
        reviewer_user_id=scope.manager_id,
        department_id=scope.department_id,
        due_at=DUE_LATE,
    )
    plant_review(
        access_db_url,
        review_id=low,
        reviewer_user_id=scope.manager_id,
        department_id=scope.department_id,
        due_at=DUE_LATE,
    )
    earliest = plant_review(
        access_db_url,
        reviewer_user_id=scope.manager_id,
        department_id=scope.department_id,
        due_at=DUE_EARLY,
    )

    answered = [
        row.review_id for row in read_queue(access_db_url, department_id=scope.department_id)
    ]

    assert answered == [earliest, low, high]


# --- the two counts (criterion 4, decision D) ------------------------------------


def test_a_review_with_three_items_one_decided_reads_three_and_one(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches `decided_count` counting every item (or every decision equal to
    `revoke`), and `item_count` counting the reviews rather than their items.

    Three items, exactly one of them carrying a decision, so a count that
    answers 3 and 3, 3 and 0, or 1 and 1 each fails on its own number. The
    neighbouring review in the same department holds a decided item of its own,
    so an aggregate that forgets to group by review answers 4 and 2 here.
    """
    scope = plant_scope(access_db_url, members=1)
    member = scope.members[0]
    grants = [
        plant_grant(access_db_url, user_id=member, role_id=role_id)
        for role_id in (scope.template_role_id, scope.extra_role_id, scope.conflict_role_id)
    ]
    review = plant_review(
        access_db_url, reviewer_user_id=scope.manager_id, department_id=scope.department_id
    )
    plant_item(access_db_url, review_id=review, grant_id=grants[0])
    plant_item(access_db_url, review_id=review, grant_id=grants[1])
    plant_item(
        access_db_url, review_id=review, grant_id=grants[2], decision=ReviewDecision.KEEP.value
    )
    neighbour = plant_review(
        access_db_url, reviewer_user_id=scope.manager_id, department_id=scope.department_id
    )
    plant_item(
        access_db_url,
        review_id=neighbour,
        grant_id=plant_grant(access_db_url, user_id=member, role_id=scope.partner_role_id),
        decision=ReviewDecision.REVOKE.value,
    )

    answered = {
        row.review_id: row for row in read_queue(access_db_url, department_id=scope.department_id)
    }

    assert answered[review].item_count == 3
    assert answered[review].decided_count == 1
    assert answered[neighbour].item_count == 1
    assert answered[neighbour].decided_count == 1


def test_a_review_with_no_items_reads_zero_and_zero_rather_than_being_absent(
    access_db_url: str,  # noqa: F811
) -> None:
    """Decision D's join direction: catches the counts arriving through an INNER
    join, which drops exactly the review that has nothing to show.

    A review with no items is the one `open_review` writes for a clean scope
    (decision J), so a queue that hides it hides every completed-on-arrival
    review in the product. The review with one item is planted in the same
    department, so a read that answers nothing at all cannot pass instead.
    """
    scope = plant_scope(access_db_url, members=1)
    empty = plant_review(
        access_db_url,
        reviewer_user_id=scope.manager_id,
        department_id=scope.department_id,
        status=ReviewStatus.COMPLETED.value,
        completed_at=COMPLETED_AT,
    )
    populated = plant_review(
        access_db_url, reviewer_user_id=scope.manager_id, department_id=scope.department_id
    )
    plant_item(
        access_db_url,
        review_id=populated,
        grant_id=plant_grant(
            access_db_url, user_id=scope.members[0], role_id=scope.template_role_id
        ),
    )

    answered = {
        row.review_id: row for row in read_queue(access_db_url, department_id=scope.department_id)
    }

    assert empty in answered
    assert answered[empty].item_count == 0
    assert answered[empty].decided_count == 0
    assert answered[empty].completed_at == COMPLETED_AT
    assert answered[populated].item_count == 1


def test_another_tenants_review_contributes_neither_a_row_nor_a_count(
    access_db_url: str,  # noqa: F811
) -> None:
    """An earlier change §5's leak, both halves: catches a queue read outside the tenant
    guard, and an aggregate written as a hand-rolled `count()` that the ORM
    guard never filters.

    The foreign review is a REAL row with its own item, so its absence has to
    come from the guard rather than from an id nobody could resolve. The second
    half is the sharper one: a `review_items` row belonging to the OTHER tenant
    but pointing at THIS tenant's review is what an unscoped aggregate adds to
    this caller's `item_count` and `decided_count`, and nothing about the row
    set would show it. The own review is asserted present with the counts its
    own items justify, so a read answering nothing to everybody cannot pass.
    """
    scope = plant_scope(access_db_url, members=1)
    # Both carry a `due_at`, so both sort ahead of the NULL-`due_at` tail and
    # the foreign row's absence is the guard's doing rather than the page size.
    mine = plant_review(
        access_db_url,
        reviewer_user_id=scope.manager_id,
        department_id=scope.department_id,
        due_at=DUE_EARLY,
    )
    plant_item(
        access_db_url,
        review_id=mine,
        grant_id=plant_grant(
            access_db_url, user_id=scope.members[0], role_id=scope.template_role_id
        ),
    )
    foreign_review = plant_review(
        access_db_url,
        tenant_id=OTHER_TENANT_ROWS.tenant_id,
        reviewer_user_id=OTHER_TENANT_ROWS.users_by_department["Engineering"][0].id,
        department_id=OTHER_TENANT_ROWS.department_ids["Engineering"],
        due_at=DUE_EARLY,
    )
    foreign_grant = _other_tenant_grant_id(access_db_url)
    plant_item(
        access_db_url,
        review_id=foreign_review,
        grant_id=foreign_grant,
        tenant_id=OTHER_TENANT_ROWS.tenant_id,
    )
    # The rogue child row: another tenant's item hanging off THIS tenant's
    # review, which only a tenant-scoped aggregate refuses to count.
    plant_item(
        access_db_url,
        review_id=mine,
        grant_id=foreign_grant,
        tenant_id=OTHER_TENANT_ROWS.tenant_id,
        decision=ReviewDecision.KEEP.value,
    )

    answered = {row.review_id: row for row in read_queue(access_db_url, any_scope=True, limit=200)}

    assert foreign_review not in answered
    assert answered[mine].item_count == 1
    assert answered[mine].decided_count == 0


# --- the widened item read and its projection (criterion 7) ----------------------


def test_list_items_with_context_carries_the_grants_six_context_fields(
    access_db_url: str,  # noqa: F811
) -> None:
    """Decision B. Catches a read that answers the item alone, or one that
    echoes the item's own columns under context names.

    A reviewer cannot decide a `grant_id`: the six fields name the PERSON, the
    role, the system, the privilege and the two dates, each read from the row
    its id points at. Every value asserted here is planted by this case and by
    nothing else in the file, so a join to the wrong row (the reviewer instead
    of the grant holder, the review's system instead of the role's) answers a
    different name rather than a plausible one.
    """
    scope = plant_scope(access_db_url, members=1)
    plant_grant(
        access_db_url,
        user_id=scope.members[0],
        role_id=scope.template_role_id,
        expires_at=FAR_EXPIRY,
    )
    review = open_one(access_db_url, department_id=scope.department_id)

    async def work(session: AsyncSession) -> list[dict[str, Any]]:
        rows = await list_items_with_context(session, review.review_id, limit=50)
        return [
            {
                "item_id": row.item.id,
                "grant_id": row.item.grant_id,
                "target_display_name": row.target_display_name,
                "role_name": row.role_name,
                "system_name": row.system_name,
                "privilege": row.privilege,
                "granted_at": row.granted_at,
                "expires_at": row.expires_at,
            }
            for row in rows
        ]

    answered = in_tenant(access_db_url, work)

    assert len(answered) == 1
    assert answered[0]["item_id"] in item_ids_of(access_db_url, review.review_id)
    assert answered[0]["target_display_name"] == "Fake Reviewee"
    assert answered[0]["role_name"] == "Fake Review Role"
    assert answered[0]["system_name"] == "Fake Review System"
    assert answered[0]["privilege"] == "read"
    assert answered[0]["granted_at"] == GRANTED_AT
    assert answered[0]["expires_at"] == FAR_EXPIRY


def test_list_items_answers_the_same_orm_rows_as_the_widened_read(
    access_db_url: str,  # noqa: F811
) -> None:
    """a project rule's projection, pinned from the narrow side.

    Catches `list_items` rewritten to answer the widened rows (its existing
    callers unpack `Sequence[ReviewItem]` and would break on `.flag`), and
    catches the projection re-querying with its own order or its own limit
    instead of projecting: the two reads are compared as ORDERED sequences over
    a review with three items, so a projection that sorts differently fails
    while a set comparison would not.
    """
    scope = plant_scope(access_db_url, members=1)
    member = scope.members[0]
    for role_id in (scope.template_role_id, scope.extra_role_id, scope.conflict_role_id):
        plant_grant(access_db_url, user_id=member, role_id=role_id)
    review = open_one(access_db_url, department_id=scope.department_id)

    async def work(session: AsyncSession) -> tuple[list[uuid.UUID], list[uuid.UUID], list[bool]]:
        narrow = list(await list_items(session, review.review_id, limit=50))
        widened = await list_items_with_context(session, review.review_id, limit=50)
        return (
            [row.id for row in narrow],
            [row.item.id for row in widened],
            [isinstance(row, ReviewItem) for row in narrow],
        )

    narrow_ids, widened_ids, are_orm_rows = in_tenant(access_db_url, work)

    assert len(narrow_ids) == 3
    assert narrow_ids == widened_ids
    assert are_orm_rows == [True, True, True]
