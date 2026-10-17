"""Reading access reviews back: finding one review or item by id, and listing reviews and items for a scope."""

import uuid
from collections.abc import Sequence

from sqlalchemy import ColumnElement, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.access.review_types import ReviewItemWithContext, ReviewWithCounts
from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.crud import Repository
from helpdesk.shared.models import (
    AccessReview,
    Department,
    Grant,
    ReviewItem,
    ReviewStatus,
    Role,
    System,
    User,
)

__all__ = ["find_review", "find_item", "list_owned_system_ids", "list_reviews_for_scope", "list_items_with_context", "list_items"]


async def find_review(session: AsyncSession, review_id: uuid.UUID) -> AccessReview | None:
    """The bound tenant's review, or None.

    Through the ORM, so the tenant guard injects its filter and a review id
    from another tenant reads exactly like one that does not exist — the
    caller turns both into one `NOT_FOUND`, and only AFTER its scope check,
    so a review id is neither an existence nor a state oracle.
    """
    return await Repository(session, AccessReview).get(review_id)


async def find_item(session: AsyncSession, item_id: uuid.UUID) -> ReviewItem | None:
    """The bound tenant's review item, or None.

    Through the ORM, so the tenant guard injects its filter and an item id
    from another tenant reads exactly like one that does not exist — the
    caller turns both into one `NOT_FOUND`, and only AFTER its scope check, so
    an item id is neither an existence nor a state oracle. That the guard
    really reaches this table (an earlier change decision B put `tenant_id` on the
    child for it) is pinned by
    `test_access_reviews.py::test_find_item_answers_nothing_for_another_tenants_item`.
    """
    return await Repository(session, ReviewItem).get(item_id)


async def list_owned_system_ids(
    session: AsyncSession, owner_user_id: uuid.UUID
) -> frozenset[uuid.UUID]:
    """The systems this user owns, as ids, inside the bound tenant.

    Lives here rather than in the tool because a service may not call
    `session.execute` at all; what the tool keeps is the DECISION to ask, which
    it makes from the design's `own_systems` scope. Answers an empty set for a
    user who owns nothing, which `list_reviews_for_scope` then treats as a
    scope reaching no review rather than as a scope reaching every one.
    """
    result = await session.execute(select(System.id).where(System.owner_user_id == owner_user_id))
    return frozenset(result.scalars().all())


async def list_reviews_for_scope(
    session: AsyncSession,
    *,
    department_id: uuid.UUID | None,
    system_ids: frozenset[uuid.UUID],
    any_scope: bool,
    status: ReviewStatus | None,
    limit: int,
) -> Sequence[ReviewWithCounts]:
    """The reviews one caller's the design scope reaches, oldest due first.

    The scope arrives DECIDED — a department id, a set of owned system ids, or
    `any_scope` — rather than as claims, for the reason `list_items` takes no
    scope argument at all: nothing in `helpdesk/access/` reads a token, and one
    scope authority answers for the whole review family. A caller whose scope
    resolves to none of the three reaches no review, which is an EMPTY answer
    and never a refusal: the queue takes no id, so
    there is nothing that could be missing.

    Ordered by `due_at` oldest first with the review id breaking a tie, so the
    order is total and two calls return the same page. A review with no
    `due_at` sorts last, Postgres's default for an ascending nullable column
    and the right end for a review nobody has dated. Pinned as an exact
    sequence by
    `test_access_reviews.py::test_the_queue_orders_by_due_at_oldest_first_with_the_review_id_breaking_the_tie`,
    whose tied pair is inserted highest-id first so the obvious copy of
    `list_items`' `created_at` order fails.

    The two counts come from ONE grouped aggregate over the page's reviews
    (decision D), never a query per review: a queue of fifty is one read plus
    one aggregate rather than fifty-one. `decided_count` counts the items
    carrying a decision, which `func.count` over a nullable column answers
    directly. The aggregate names its tenant explicitly rather than relying on
    the ORM guard, because a `func.count` over a filtered select is exactly the
    shape that slips past a criteria injection; pinned by
    `test_access_reviews.py::test_another_tenants_review_contributes_neither_a_row_nor_a_count`,
    which plants a foreign-tenant item hanging off this tenant's review.

    Both scope joins are OUTER: exactly one of the two ids is set on any
    review, so an inner join to either would answer nothing at all. The
    reviewer join is inner, because `reviewer_user_id` is not nullable.
    """
    predicates: list[ColumnElement[bool]] = []
    if any_scope:
        predicates.append(true())
    if department_id is not None:
        predicates.append(AccessReview.department_id == department_id)
    if system_ids:
        predicates.append(AccessReview.system_id.in_(system_ids))
    if not predicates:
        return []

    statement = (
        select(AccessReview, Department.name, System.name, User.display_name)
        .outerjoin(Department, Department.id == AccessReview.department_id)
        .outerjoin(System, System.id == AccessReview.system_id)
        .join(User, User.id == AccessReview.reviewer_user_id)
        .where(or_(*predicates))
        .order_by(AccessReview.due_at, AccessReview.id)
        .limit(limit)
    )
    if status is not None:
        statement = statement.where(AccessReview.status == status.value)
    rows = (await session.execute(statement)).all()

    counts = await _item_counts(session, [review.id for review, _, _, _ in rows])
    return [
        ReviewWithCounts(
            review=review,
            department_name=department_name,
            system_name=system_name,
            reviewer_display_name=reviewer_display_name,
            item_count=counts.get(review.id, (0, 0))[0],
            decided_count=counts.get(review.id, (0, 0))[1],
        )
        for review, department_name, system_name, reviewer_display_name in rows
    ]


async def _item_counts(
    session: AsyncSession, review_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, tuple[int, int]]:
    """(items, decided items) per review, for the page's reviews only.

    Absent from the answer means a review with no items at all, which the
    caller reads as `(0, 0)` — never as a review to hide. An inner join to
    `review_items` would drop exactly the review that has nothing to show, and
    that is the review `open_review` writes for a clean scope; pinned by
    `test_access_reviews.py::test_a_review_with_no_items_reads_zero_and_zero_rather_than_being_absent`.
    """
    if not review_ids:
        return {}
    result = await session.execute(
        select(
            ReviewItem.review_id,
            func.count(ReviewItem.id),
            func.count(ReviewItem.decision),
        )
        .where(
            ReviewItem.review_id.in_(review_ids),
            ReviewItem.tenant_id == bound_tenant_id(session),
        )
        .group_by(ReviewItem.review_id)
    )
    return {review_id: (total, decided) for review_id, total, decided in result.all()}


async def list_items_with_context(
    session: AsyncSession, review_id: uuid.UUID, *, limit: int
) -> Sequence[ReviewItemWithContext]:
    """One review's items with the grant behind each, in `list_items`' order.

    FOUR joins in one statement, the shape an earlier change's approval queue uses: the
    grant, its holder, its role and the role's system. All four are inner
    joins, since every item names a grant and every grant names all three of
    the others. One row per item, never multiplied by the joins.

    The order is `list_items`' own — `created_at` then `id` — because that
    function is now a projection of this one and its callers page through a
    stable list. The scope was checked by the caller, exactly as it is for
    `list_items`: one scope authority answers for approvals, revocation and
    reviews alike, and a second filter here would be a second place for it to
    drift.
    """
    result = await session.execute(
        select(
            ReviewItem,
            User.display_name,
            Role.name,
            System.name,
            Role.privilege,
            Grant.granted_at,
            Grant.expires_at,
        )
        .join(Grant, Grant.id == ReviewItem.grant_id)
        .join(User, User.id == Grant.user_id)
        .join(Role, Role.id == Grant.role_id)
        .join(System, System.id == Role.system_id)
        .where(ReviewItem.review_id == review_id)
        .order_by(ReviewItem.created_at, ReviewItem.id)
        .limit(limit)
    )
    return [
        ReviewItemWithContext(
            item=item,
            target_display_name=target_display_name,
            role_name=role_name,
            system_name=system_name,
            privilege=privilege,
            granted_at=granted_at,
            expires_at=expires_at,
        )
        for (
            item,
            target_display_name,
            role_name,
            system_name,
            privilege,
            granted_at,
            expires_at,
        ) in result.all()
    ]


async def list_items(
    session: AsyncSession, review_id: uuid.UUID, *, limit: int
) -> Sequence[ReviewItem]:
    """One review's items, oldest first, at most `limit` of them.

    Ordered by `created_at` then `id`, which is the order they were written
    and therefore the scope listing's own order (holder, then grant age): a
    reviewer works down a stable list, and two calls return the same page.

    The caller has already resolved the review and checked its scope, so
    there is deliberately no scope argument here — one scope authority
    (`request_in_scope`) answers for approvals, revocation and reviews alike,
    and a second filter in this query would be a second place for it to drift.

    A pure PROJECTION of `list_items_with_context` since an earlier change (standing
    rule 44): the join, the order and the limit live in exactly one place, and
    this narrow twin stays because its callers unpack `Sequence[ReviewItem]`
    and widening it in place would have been a change to closed plans' tests.
    Nothing here re-reads or re-orders. Pinned by
    `test_access_reviews.py::test_list_items_answers_the_same_orm_rows_as_the_widened_read`,
    which compares the two as ordered sequences.
    """
    return [row.item for row in await list_items_with_context(session, review_id, limit=limit)]
