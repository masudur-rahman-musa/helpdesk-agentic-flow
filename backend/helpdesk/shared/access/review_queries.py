"""Reading access reviews back: one review or item by id, and lists for a scope."""

import uuid
from collections.abc import Sequence

from sqlalchemy import ColumnElement, true
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.access.review_types import ReviewItemWithContext, ReviewWithCounts
from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.review_queries import (
    fetch_item_counts,
    fetch_items_with_context,
    fetch_owned_system_ids,
    fetch_reviews_page,
)
from helpdesk.shared.models import (
    AccessReview,
    ReviewItem,
    ReviewStatus,
)

__all__ = [
    "find_review",
    "find_item",
    "list_owned_system_ids",
    "list_reviews_for_scope",
    "list_items_with_context",
    "list_items",
]


async def find_review(session: AsyncSession, review_id: uuid.UUID) -> AccessReview | None:
    """The bound tenant's review, or None.

    Another tenant's id reads as missing; the caller answers both with one
    `NOT_FOUND`, after its scope check, so the id leaks nothing.
    """
    return await Repository(session, AccessReview).get(review_id)


async def find_item(session: AsyncSession, item_id: uuid.UUID) -> ReviewItem | None:
    """The bound tenant's review item, or None. Same rules as `find_review`."""
    return await Repository(session, ReviewItem).get(item_id)


async def list_owned_system_ids(
    session: AsyncSession, owner_user_id: uuid.UUID
) -> frozenset[uuid.UUID]:
    """The systems this user owns, as ids. An empty set reaches no review."""
    return await fetch_owned_system_ids(session, owner_user_id)


async def list_reviews_for_scope(
    session: AsyncSession,
    *,
    department_id: uuid.UUID | None,
    system_ids: frozenset[uuid.UUID],
    any_scope: bool,
    status: ReviewStatus | None,
    limit: int,
) -> Sequence[ReviewWithCounts]:
    """The reviews one caller's scope reaches, oldest due first.

    The scope comes in already decided, since nothing here reads a token. A
    scope that reaches nothing gives an empty list, not a refusal. The
    review id breaks ties, so the order is total; undated reviews go last.

    Item counts come from one grouped query for the whole page, not one
    per review. That query names its tenant itself rather than trusting the
    ORM guard, because a count over a filtered select is the kind of query
    that can slip past it. Both scope joins are outer: each review has only
    one of the two ids.
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

    rows = await fetch_reviews_page(
        session,
        predicates,
        status_value=status.value if status is not None else None,
        limit=limit,
    )

    counts = await fetch_item_counts(session, [review.id for review, _, _, _ in rows])
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


async def list_items_with_context(
    session: AsyncSession, review_id: uuid.UUID, *, limit: int
) -> Sequence[ReviewItemWithContext]:
    """One review's items with the grant behind each, in `list_items`' order.

    One row per item; every join is inner, since every item has a grant and
    every grant has a holder, role and system. The caller has already
    checked scope.
    """
    rows = await fetch_items_with_context(session, review_id, limit=limit)
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
        ) in rows
    ]


async def list_items(
    session: AsyncSession, review_id: uuid.UUID, *, limit: int
) -> Sequence[ReviewItem]:
    """One review's items, oldest first, at most `limit` of them.

    A narrow view of `list_items_with_context`, so the order and limit live
    in one place. No scope argument: the caller has already checked it.
    """
    return [row.item for row in await list_items_with_context(session, review_id, limit=limit)]
