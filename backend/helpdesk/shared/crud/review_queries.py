"""Review-queue reads for the listing and item tools."""

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


async def fetch_items_with_context(
    session: AsyncSession, review_id: uuid.UUID, *, limit: int
) -> list[Any]:
    """One review's items with the grant, holder, role and system behind each."""
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
