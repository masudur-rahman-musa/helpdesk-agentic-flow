"""Review-state reads for opening and deciding reviews."""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.models import AccessReview, ReviewItem, ReviewStatus


async def find_open_review_for_scope(
    session: AsyncSession,
    *,
    department_id: uuid.UUID | None,
    system_id: uuid.UUID | None,
    period: str,
    open_statuses: Sequence[str],
) -> AccessReview | None:
    """An open review for this scope and period, or None. Read after the scope lock is held."""
    result = await session.execute(
        select(AccessReview).where(
            AccessReview.department_id == department_id,
            AccessReview.system_id == system_id,
            AccessReview.period == period,
            AccessReview.status.in_(open_statuses),
        )
    )
    return result.scalars().first()


async def reread_review_item(session: AsyncSession, item: ReviewItem) -> ReviewItem:
    """The item re-read from the database once the lock is held.

    The cached copy predates the lock, so the race loser would overwrite the winner.
    """
    result = await session.execute(
        select(ReviewItem).where(ReviewItem.id == item.id).execution_options(populate_existing=True)
    )
    return result.scalars().one()


async def find_first_undecided_item_id(
    session: AsyncSession, review_id: uuid.UUID
) -> uuid.UUID | None:
    """An undecided item of this review, or None when the review can close."""
    result = await session.execute(
        select(ReviewItem.id).where(
            ReviewItem.review_id == review_id, ReviewItem.decision.is_(None)
        )
    )
    return result.scalars().first()


async def fetch_reviewed_scopes_for_period(
    session: AsyncSession, period: str
) -> set[tuple[uuid.UUID | None, uuid.UUID | None]]:
    """Scopes already reviewed this period, in any status.

    A completed review must also block a repeat, or empty scopes would reopen daily.
    """
    result = await session.execute(select(AccessReview).where(AccessReview.period == period))
    return {(review.department_id, review.system_id) for review in result.scalars().all()}


async def fetch_overdue_reviews(session: AsyncSession, now: datetime) -> list[AccessReview]:
    """Open reviews past their due date. One due exactly now is not yet overdue."""
    result = await session.execute(
        select(AccessReview)
        .where(
            AccessReview.status == ReviewStatus.OPEN.value,
            AccessReview.due_at.is_not(None),
            AccessReview.due_at < now,
        )
        .order_by(AccessReview.due_at, AccessReview.id)
    )
    return list(result.scalars().all())
