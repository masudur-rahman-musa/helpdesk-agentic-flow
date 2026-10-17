"""`notify`: the one writer of the `notifications` table.

The tenant comes from the session, never an argument. Every check runs before
the row is added: the expiry sweep calls this after real work in the same
transaction, and a column error would undo that work.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.core.errors import ErrorCode, NotFoundError, ToolError
from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.users import find_user_by_id
from helpdesk.shared.models.notification import (
    NOTIFICATION_BODY_MAX_LENGTH,
    NOTIFICATION_TITLE_MAX_LENGTH,
    Notification,
    NotificationKind,
)


class InvalidNotificationError(ToolError):
    """The title, body or kind cannot be stored as given."""

    code = ErrorCode.VALIDATION


async def notify(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    kind: NotificationKind,
    title: str,
    body: str,
    subject_type: str | None,
    subject_id: str | None,
) -> Notification:
    """Store one unread notification and flush it; the caller commits."""
    try:
        stored_kind = NotificationKind(kind)
    except ValueError as exc:
        raise InvalidNotificationError(f"{kind!r} is not a notification kind") from exc
    if len(title) > NOTIFICATION_TITLE_MAX_LENGTH:
        raise InvalidNotificationError(
            f"a title longer than {NOTIFICATION_TITLE_MAX_LENGTH} characters "
            f"cannot be stored: {len(title)}"
        )
    if len(body) > NOTIFICATION_BODY_MAX_LENGTH:
        raise InvalidNotificationError(
            f"a body longer than {NOTIFICATION_BODY_MAX_LENGTH} characters "
            f"cannot be stored: {len(body)}"
        )

    # The read is tenant-filtered by the session guard, so this refuses a
    # foreign recipient and a missing one identically.
    if await find_user_by_id(session, user_id) is None:
        raise NotFoundError(f"no user {user_id} in this tenant to notify")

    return await Repository(session, Notification).create(
        user_id=user_id,
        kind=stored_kind.value,
        title=title,
        body=body,
        subject_type=subject_type,
        subject_id=subject_id,
        read_at=None,
    )
