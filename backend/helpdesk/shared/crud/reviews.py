"""Review-state reads for the open and decide paths.

Every statement against `access_reviews` and `review_items` that the review
flow needs lives here, so the callers in `shared/access/reviews.py` build no
statement of their own. The tenant guard in `helpdesk.core.db` scopes every
read to the bound session.
"""

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
    """The first open review matching this scope and period, or None.

    Called by `_refuse_if_open` after the scope row is locked.  Two concurrent
    openers serialise on the scope lock and the loser re-reads the review the
    winner committed — it then sees a row here and raises `ReviewAlreadyOpenError`.
    Pinned indirectly by
    `test_mcp_reviews.py::test_two_callers_racing_the_same_review_leave_exactly_one`.
    """
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
    """The item's CURRENT row, re-read after the grant lock is held.

    `populate_existing=True` forces a fresh database read instead of returning
    the session's identity-map copy, which was loaded before the lock was
    waited for and therefore carries a stale `decision = None`. Without this
    the loser of a race would overwrite the winner's decision while every
    single-caller test still passed.  Pinned by
    `test_access_reviews.py::test_two_concurrent_deciders_leave_one_decided_item_and_one_refusal`.
    """
    result = await session.execute(
        select(ReviewItem).where(ReviewItem.id == item.id).execution_options(populate_existing=True)
    )
    return result.scalars().one()


async def find_first_undecided_item_id(
    session: AsyncSession, review_id: uuid.UUID
) -> uuid.UUID | None:
    """The id of the first undecided item in this review, or None when every
    item has been decided.

    Called by `_complete_if_finished` after the deciding item's own write is
    flushed, so the row just decided is counted as decided and the check is
    over the updated table.  A non-None result means the review stays open;
    None means it should be closed.  Pinned by
    `test_access_reviews.py::test_a_neighbouring_reviews_undecided_item_does_not_hold_this_one_open`,
    which checks this is scoped to the one review and not the tenant.
    """
    result = await session.execute(
        select(ReviewItem.id).where(
            ReviewItem.review_id == review_id, ReviewItem.decision.is_(None)
        )
    )
    return result.scalars().first()


async def fetch_reviewed_scopes_for_period(
    session: AsyncSession, period: str
) -> set[tuple[uuid.UUID | None, uuid.UUID | None]]:
    """Every (department_id, system_id) pair already reviewed this period.

    Any status counts, not just open: a completed review of a clean scope (one
    with no grants) must block a duplicate just as a still-open one would.
    Without the completed status this guard would open a second review for
    every empty department on the same day the first was written. Pinned
    indirectly by
    `test_access_review_generation.py::test_a_scope_with_an_open_review_is_skipped_and_the_other_scope_still_opens`.
    """
    result = await session.execute(
        select(AccessReview).where(AccessReview.period == period)
    )
    return {(review.department_id, review.system_id) for review in result.scalars().all()}


async def fetch_overdue_reviews(session: AsyncSession, now: datetime) -> list[AccessReview]:
    """The bound tenant's late open reviews, ordered by due_at then id.

    Three conjuncts: status `open` (a completed review is answered), `due_at IS
    NOT NULL` (an undated review is not overdue), and `due_at < now` (strict —
    a review due exactly now is due, not overdue).  Both halves of the boundary
    are pinned to the microsecond by
    `test_access_review_reminders.py::test_a_review_due_at_exactly_now_is_not_overdue`
    and `::test_a_review_one_microsecond_past_its_due_date_is_overdue`.
    """
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
