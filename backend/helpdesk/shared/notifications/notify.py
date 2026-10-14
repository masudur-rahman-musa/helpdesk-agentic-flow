"""`notify`: the one writer of the `notifications` table (the design).

Shaped after `audit/writer.py::append_event`, deliberately: the tenant is read
off the SESSION with `bound_tenant_id` and there is no tenant parameter at all, the row is added inside the caller's transaction and never
committed here, and every input that reaches a store is checked before
anything is added.

The order of the three refusals is the order of their cost. The two length
caps and the kind are decided from the arguments alone; only then is the
recipient read, because that read is a round trip. All three run before
`session.add`, which is what the pending-count assertions in
`test_notifications.py` are for: the expiry sweep composes this text AFTER it
has marked a grant expired and appended its audit event, so a refusal that
arrived as a column error would abort a transaction that had already recorded
real work.

A recipient outside the bound tenant is `NOT_FOUND` rather than a stamped row
pointing at a stranger: the session guard makes the foreign user unreadable,
so "no such user here" is the honest answer and the id stays a non-capability.
Pinned by
`test_notifications.py::test_a_recipient_in_another_tenant_is_refused_not_found_and_writes_nothing`.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.core.repository import Repository, find_user_by_id
from helpdesk.shared.errors import ErrorCode, NotFoundError, ToolError
from helpdesk.shared.models.notification import (
    NOTIFICATION_BODY_MAX_LENGTH,
    NOTIFICATION_TITLE_MAX_LENGTH,
    Notification,
    NotificationKind,
)


class InvalidNotificationError(ToolError):
    """The title, the body or the kind cannot be stored as given.

    `VALIDATION`, the the design code for an input the caller can fix, and one
    type for all three because from a caller's side they are one answer. The
    same shape `models/conversation.py::MessageTooLongError` uses; pinned by
    the two boundary refusals and the unknown-kind case in
    `test_notifications.py`, each of which asserts `ErrorCode.VALIDATION`.
    """

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
    """Store one unread notification for one person of the bound tenant.

    Returns the flushed row; the caller commits. `read_at` is left null rather
    than defaulted to anything, because an unread row is what row 23's list
    reads and a row stored already read would be invisible from the moment it
    was written. Pinned by
    `test_notifications.py::test_notify_stores_one_unread_row_under_the_sessions_bound_tenant`,
    which also proves the `tenant_id` comes from the session: there is no
    argument it could come from instead.
    """
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
