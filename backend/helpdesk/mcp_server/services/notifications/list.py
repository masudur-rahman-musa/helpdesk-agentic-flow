"""`list_for_user`: one person's own bell, with the unread total beside it.

The page and the count come from one call, since a bell number and a list are
two views of one answer. `limit` bounds the rows only; the count is every
unread row, so the bell never stops at the page size.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.crud.notifications import (
    count_unread_notifications,
    fetch_notification_page,
)
from helpdesk.shared.models.notification import Notification


@dataclass(frozen=True)
class NotificationPage:
    """One person's rows and their whole unread total.

    `unread_count` is not `len(notifications)`: the rows are one page.
    """

    notifications: Sequence[Notification]
    unread_count: int


async def list_for_user(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    unread_only: bool,
    limit: int,
) -> NotificationPage:
    """This user's notifications of the bound tenant, newest first.

    Ties on `created_at` break by id, so two calls answer the same page; the
    expiry sweep often writes two rows in the same instant. `unread_only`
    narrows the rows, never the count. Nobody's bell is a refusal: no rows
    and a zero count. The count query names the tenant itself, because the
    session's tenant filter does not reach aggregate queries.
    """
    notifications = await fetch_notification_page(
        session, user_id=user_id, unread_only=unread_only, limit=limit
    )
    unread_count = await count_unread_notifications(session, user_id)
    return NotificationPage(notifications=notifications, unread_count=unread_count)
