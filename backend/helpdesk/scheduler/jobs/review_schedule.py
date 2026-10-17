"""The two review jobs: open this quarter's reviews, and chase late ones.

`open_review` does the writing, so reviews have one writer. Reminders write
only notifications, so there is no audit event.
"""

import logging
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.access.directory import DirectoryReader
from helpdesk.shared.access.review_types import check_period, quarter_period
from helpdesk.shared.access.reviews import (
    ReviewAlreadyOpenError,
    open_review,
)
from helpdesk.shared.access.revoke import SYSTEM_ACTOR_KIND
from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.reviews import fetch_overdue_reviews, fetch_reviewed_scopes_for_period
from helpdesk.shared.crud.users import (
    fetch_active_it_admins,
    fetch_all_departments,
    fetch_systems_by_sensitivity,
)
from helpdesk.shared.models import AccessReview, Department, System, User
from helpdesk.shared.models.notification import NotificationKind
from helpdesk.shared.notifications.notify import notify

logger = logging.getLogger(__name__)

REVIEW_DUE_DAYS = 14
"""Without a due date a review could never be overdue."""

GENERATED_SYSTEM_SENSITIVITIES = frozenset({"high", "critical"})
"""Every department gets a quarterly review; a system only when it is sensitive."""

REVIEW_GENERATION_ACTOR_REF = "scheduler:review_generation"
"""Lets an auditor tell the quarterly sweep from a manager."""

REVIEW_ESCALATION_DAYS = 14
"""Inclusive: at exactly 14 days late, every `it_admin` is told."""

_SUBJECT_TYPE = "access_review"

_REMINDER_TITLE = "An access review is overdue"
"""Fixed, so a long department name can never break `notify`'s title cap."""

_ESCALATION_TITLE = "An access review is 14 days overdue"


def _when(moment: datetime) -> str:
    """The due instant as a person reads it, in UTC."""
    return moment.strftime("%Y-%m-%d %H:%M UTC")


async def _departments_to_review(session: AsyncSession) -> Sequence[Department]:
    """Every department of the bound tenant, ordered so two runs agree."""
    return await fetch_all_departments(session)


async def _systems_to_review(session: AsyncSession) -> Sequence[System]:
    """The bound tenant's sensitive systems, filtered in SQL."""
    return await fetch_systems_by_sensitivity(session, GENERATED_SYSTEM_SENSITIVITIES)


async def _scopes_already_reviewed(
    session: AsyncSession, period: str
) -> set[tuple[uuid.UUID | None, uuid.UUID | None]]:
    """Every (department, system) already reviewed this period, in any status.

    A scope with no grants opens an already `completed` review, so checking only
    open ones would reopen an empty department every day.
    """
    return await fetch_reviewed_scopes_for_period(session, period)


async def generate_reviews(
    session: AsyncSession, directory: DirectoryReader, *, now: datetime
) -> list[AccessReview]:
    """Open this quarter's reviews for every due scope. The caller commits.

    Only `ReviewAlreadyOpenError` is caught per scope; anything else goes to the
    runner to retry. A scope with no owner is skipped and logged, never given a
    made-up reviewer.
    """
    period = check_period(quarter_period(now))
    due_at = now + timedelta(days=REVIEW_DUE_DAYS)
    already = await _scopes_already_reviewed(session, period)

    opened: list[AccessReview] = []
    for department in await _departments_to_review(session):
        result = await _open_one(
            session,
            directory,
            department_id=department.id,
            system_id=None,
            owner_user_id=department.manager_user_id,
            period=period,
            due_at=due_at,
            already=already,
            now=now,
        )
        if result is not None:
            opened.append(result)

    for system in await _systems_to_review(session):
        result = await _open_one(
            session,
            directory,
            department_id=None,
            system_id=system.id,
            owner_user_id=system.owner_user_id,
            period=period,
            due_at=due_at,
            already=already,
            now=now,
        )
        if result is not None:
            opened.append(result)

    return opened


async def _open_one(
    session: AsyncSession,
    directory: DirectoryReader,
    *,
    department_id: uuid.UUID | None,
    system_id: uuid.UUID | None,
    owner_user_id: uuid.UUID | None,
    period: str,
    due_at: datetime,
    already: set[tuple[uuid.UUID | None, uuid.UUID | None]],
    now: datetime,
) -> AccessReview | None:
    """One scope's review, or None when the scope is skipped."""
    if owner_user_id is None:
        logger.info(
            "review generation skipped a scope with no owner",
            extra={"department_id": department_id, "system_id": system_id, "period": period},
        )
        return None
    if (department_id, system_id) in already:
        return None

    try:
        result = await open_review(
            session,
            directory,
            department_id=department_id,
            system_id=system_id,
            period=period,
            opened_by_user_id=None,
            due_at=due_at,
            actor_ref=REVIEW_GENERATION_ACTOR_REF,
            actor_kind=SYSTEM_ACTOR_KIND,
            session_id=None,
            now=now,
        )
    except ReviewAlreadyOpenError:
        # Opened since the read above, by another runner or a manager. Skip
        # this scope, not the whole quarter.
        return None
    return result.review


async def _overdue_reviews(session: AsyncSession, now: datetime) -> Sequence[AccessReview]:
    """The bound tenant's open reviews past due. No lock: the daily cadence is the dedupe."""
    return await fetch_overdue_reviews(session, now)


async def _active_it_admins(session: AsyncSession) -> Sequence[User]:
    """Every active `it_admin`. A leaver keeps their principals, so status filters them out."""
    return await fetch_active_it_admins(session)


async def _scope_name(session: AsyncSession, review: AccessReview) -> str:
    """What the review is of. Falls back to the id: a worse name beats no reminder."""
    if review.department_id is not None:
        department = await Repository(session, Department).get(review.department_id)
        return str(review.department_id) if department is None else department.name
    if review.system_id is not None:
        system = await Repository(session, System).get(review.system_id)
        return str(review.system_id) if system is None else system.name
    return "an unnamed scope"  # pragma: no cover - a review always names one scope


async def send_review_reminders(session: AsyncSession, *, now: datetime) -> list[AccessReview]:
    """Remind every late reviewer, and escalate the very late. The caller commits.

    Escalation tells every active `it_admin` as well as the reviewer, not
    instead. The admin list is read at most once per run.
    """
    overdue = await _overdue_reviews(session, now)
    escalation_boundary = now - timedelta(days=REVIEW_ESCALATION_DAYS)
    admins: Sequence[User] | None = None

    for review in overdue:
        due_at = review.due_at
        if due_at is None:  # pragma: no cover - the filter excludes these
            continue
        scope_name = await _scope_name(session, review)
        await notify(
            session,
            user_id=review.reviewer_user_id,
            kind=NotificationKind.REVIEW_OVERDUE,
            title=_REMINDER_TITLE,
            body=(
                f"The {review.period} access review of {scope_name} was due on "
                f"{_when(due_at)} and is still open. Decide its remaining items."
            ),
            subject_type=_SUBJECT_TYPE,
            subject_id=str(review.id),
        )

        if due_at > escalation_boundary:
            continue
        if admins is None:
            admins = await _active_it_admins(session)
        for admin in admins:
            await notify(
                session,
                user_id=admin.id,
                kind=NotificationKind.REVIEW_ESCALATED,
                title=_ESCALATION_TITLE,
                body=(
                    f"The {review.period} access review of {scope_name} was due on "
                    f"{_when(due_at)} and has been open for {REVIEW_ESCALATION_DAYS} days "
                    "past its date."
                ),
                subject_type=_SUBJECT_TYPE,
                subject_id=str(review.id),
            )

    return list(overdue)


__all__ = [
    "GENERATED_SYSTEM_SENSITIVITIES",
    "REVIEW_DUE_DAYS",
    "REVIEW_ESCALATION_DAYS",
    "REVIEW_GENERATION_ACTOR_REF",
    "generate_reviews",
    "send_review_reminders",
]
