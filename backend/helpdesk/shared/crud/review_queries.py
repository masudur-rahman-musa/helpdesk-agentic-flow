"""Review-queue reads for the listing and item tools."""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import ColumnElement, func, literal, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.models import (
    AccessReview,
    Department,
    Grant,
    ReviewFlag,
    ReviewItem,
    Role,
    System,
    User,
)


async def fetch_owned_system_ids(
    session: AsyncSession, owner_user_id: uuid.UUID
) -> frozenset[uuid.UUID]:
    """Systems this user owns. Empty means the scope reaches no review, not every review."""
    result = await session.execute(select(System.id).where(System.owner_user_id == owner_user_id))
    return frozenset(result.scalars().all())


async def fetch_reviews_page(
    session: AsyncSession,
    predicates: Sequence[ColumnElement[bool]],
    *,
    status_value: str | None,
    limit: int,
) -> list[Any]:
    """Reviews matching any predicate, with scope and reviewer names, oldest due first.

    `predicates` must not be empty. The scope joins are outer because a review
    has either a department or a system, never both.
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
    """(total, decided) items per review, in one grouped query.

    The tenant filter is written out: the guard skips a bare count.
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


_PLAIN_LAST = ReviewItem.flag == ReviewFlag.OK.value
"""Sorts flagged items (false) before plain ones (true)."""


async def fetch_items_with_context(
    session: AsyncSession,
    review_id: uuid.UUID,
    *,
    limit: int,
    after_item_id: uuid.UUID | None = None,
) -> list[Any]:
    """One review's items with the grant, holder, role and system behind each.

    Flagged items come first, so the few that need a look are on the first
    page of a big review. Then creation time, then id: a review's items are
    written together and share a creation time, so the id keeps the order
    fixed. With `after_item_id`, the page starts just after that item of this
    review.
    """
    statement = (
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
        .order_by(_PLAIN_LAST, ReviewItem.created_at, ReviewItem.id)
        .limit(limit)
    )
    if after_item_id is not None:
        # An id from another review finds no row, so the page comes back empty.
        after = select(ReviewItem).where(
            ReviewItem.review_id == review_id, ReviewItem.id == after_item_id
        )
        statement = statement.where(
            tuple_(_PLAIN_LAST, ReviewItem.created_at, ReviewItem.id)
            > tuple_(
                after.with_only_columns(_PLAIN_LAST).scalar_subquery(),
                after.with_only_columns(ReviewItem.created_at).scalar_subquery(),
                literal(after_item_id),
            )
        )
    result = await session.execute(statement)
    return list(result.all())
