"""Notification reads: a page, the unread count, and one row."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.models.notification import Notification


async def fetch_notification_page(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    unread_only: bool,
    limit: int,
) -> list[Notification]:
    """Newest first, with id breaking ties: one sweep writes several rows at one instant."""
    statement = select(Notification).where(Notification.user_id == user_id)
    if unread_only:
        statement = statement.where(Notification.read_at.is_(None))
    result = await session.execute(
        statement.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit)
    )
    return list(result.scalars().all())


async def count_unread_notifications(session: AsyncSession, user_id: uuid.UUID) -> int:
    """Unread count. The tenant filter is written out: the guard skips a bare count."""
    result = await session.execute(
        select(func.count(Notification.id)).where(
            Notification.tenant_id == bound_tenant_id(session),
            Notification.user_id == user_id,
            Notification.read_at.is_(None),
        )
    )
    return int(result.scalar_one())


async def fetch_notification_for_user(
    session: AsyncSession,
    *,
    notification_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Notification | None:
    """This user's own notification, or None; knowing a colleague's id is not enough."""
    result = await session.execute(
        select(Notification).where(
            Notification.id == notification_id, Notification.user_id == user_id
        )
    )
    return result.scalars().one_or_none()
