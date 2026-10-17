"""`mark_read`: the one writer of `notifications.read_at`.

It sits in the shared service, not the tool, so any later caller stamps
through the same code.
"""

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.core.errors import NotFoundError
from helpdesk.shared.crud.notifications import fetch_notification_for_user
from helpdesk.shared.models.notification import Notification


async def mark_read(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    notification_id: uuid.UUID,
    now: datetime,
) -> Notification:
    """Stamp this user's own unread notification, or answer the stamp it has.

    The clock is passed in. A second call with a later clock returns the
    first stamp, so "when was this first seen" never drifts. This holds
    whatever the idempotency key, not only on a replay.

    A missing row, a colleague's row and another tenant's row all answer
    `NOT_FOUND`, and nothing is stamped. `FORBIDDEN` would confirm that the
    id names a real notification somewhere.
    """
    row = await fetch_notification_for_user(
        session, notification_id=notification_id, user_id=user_id
    )
    if row is None:
        raise NotFoundError(f"no notification {notification_id} for this user")
    if row.read_at is not None:
        return row

    row.read_at = now
    await session.flush()
    return row
