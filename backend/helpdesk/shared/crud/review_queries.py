"""Review-queue reads for the listing and item tools.

Every statement that reads `access_reviews` or `review_items` in a list or
aggregate context lives here, so `shared/access/review_queries.py` builds no
statement of its own.  The tenant guard in `helpdesk.core.db` scopes every
read to the bound session.
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.models import (
    AccessReview,
    Department,
    Grant,
    ReviewItem,
    Role,
    System,
    User,
)


async def fetch_owned_system_ids(
    session: AsyncSession, owner_user_id: uuid.UUID
) -> frozenset[uuid.UUID]:
    """The ids of every system this user owns inside the bound tenant.

    Answers an empty set for a user who owns nothing, which the caller treats
    as a scope reaching no review rather than as a scope reaching every one.
    Pinned by the system-scope rows of
    `test_access_reviews.py::test_the_queue_orders_by_due_at_oldest_first_with_the_review_id_breaking_the_tie`.
    """
    result = await session.execute(select(System.id).where(System.owner_user_id == owner_user_id))
    return frozenset(result.scalars().all())


async def fetch_reviews_page(
    session: AsyncSession,
    predicates: Sequence[ColumnElement[bool]],
    *,
    status_value: str | None,
    limit: int,
) -> list[Any]:
    """The scope-filtered reviews with their scope names and the reviewer's display
    name, as a list of raw rows.

    The caller (`list_reviews_for_scope`) builds the predicates from the resolved
    scope — a department id, a set of owned system ids or the `any_scope` flag —
    and passes a non-empty list here.  An empty `predicates` must never reach this
    function; the caller returns early in that case.

    Both scope joins are OUTER because exactly one of the two ids is set on any
    review.  The reviewer join is inner since `reviewer_user_id` is not nullable.
    Ordered by `due_at` oldest first with the review id breaking ties, so the
    order is total and two calls return the same page.  Pinned by
    `test_access_reviews.py::test_the_queue_orders_by_due_at_oldest_first_with_the_review_id_breaking_the_tie`.
    """
    statement = (
        select(AccessReview, Department.name, System.name, User.display_name)
        .outerjoin(Department, Department.id == AccessReview.department_id)
        .outerjoin(System, System.id == AccessReview.system_id)
        .join(User, User.id == AccessReview.reviewer_user_id)
        .where(or_(*predicates))
        .order_by(AccessReview.due_at, AccessReview.id)
        .limit(limit)
    )
    if status_value is not None:
        statement = statement.where(AccessReview.status == status_value)
    return list((await session.execute(statement)).all())


async def fetch_item_counts(
    session: AsyncSession, review_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, tuple[int, int]]:
    """(total items, decided items) per review, for the listed reviews only.

    One grouped aggregate over the given reviews — never a query per review —
    so a queue of fifty is one read plus one aggregate rather than fifty-one.
    `decided_count` counts the items carrying a decision, which `func.count`
    over a nullable column answers directly.

    The tenant clause is written out rather than left to the ORM guard because
    a `func.count` aggregate loads no entity and the guard's `with_loader_criteria`
    would not apply.  Pinned by
    `test_access_reviews.py::test_another_tenants_review_contributes_neither_a_row_nor_a_count`.
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


async def fetch_items_with_context(
    session: AsyncSession, review_id: uuid.UUID, *, limit: int
) -> list[Any]:
    """One review's items with the grant behind each, as raw rows.

    FOUR joins in one statement — grant, holder, role, system — returning one
    row per item.  All four are inner joins since every item names a grant and
    every grant names all three of the others.  Ordered `created_at` then `id`,
    the stable pair the narrow twin (`list_items`) uses.  Pinned by the ordering
    test in
    `test_access_reviews.py::test_list_items_answers_the_same_orm_rows_as_the_widened_read`.
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
    return list(result.all())
