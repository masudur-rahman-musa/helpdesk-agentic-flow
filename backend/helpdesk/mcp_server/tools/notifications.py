"""The notification tools: `list_notifications` and `mark_notification_read`.

A notification is addressed to one person, so both tools act only for the
token's user and neither input has a user field. Neither is offered to the
agent: the web page calls them directly. The logic lives in the
notification services; these are transport.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from helpdesk.mcp_server.services.notifications.list import list_for_user
from helpdesk.mcp_server.services.notifications.mark import mark_read
from helpdesk.mcp_server.tools.base import MutatingToolInput
from helpdesk.mcp_server.tools.context import ToolContext

_IGNORE_EXTRAS = ConfigDict(extra="ignore")
"""The read tool ignores spare arguments; identity comes from the token."""

LIMIT_MIN = 1
LIMIT_DEFAULT = 50
LIMIT_MAX = 200
"""Bounds on `list_notifications`. Without a cap a response has no size limit."""


class ListNotificationsInput(BaseModel):
    """A page size and a filter. No user and no tenant argument.

    `unread_only` narrows the rows only; `unread_count` is the same either way.
    """

    model_config = _IGNORE_EXTRAS

    unread_only: bool = False
    limit: int = Field(default=LIMIT_DEFAULT, ge=LIMIT_MIN, le=LIMIT_MAX)


class NotificationRow(BaseModel):
    """One notification as the person it names reads it. `read_at` is null while unread."""

    notification_id: uuid.UUID
    kind: str
    title: str
    body: str
    subject_type: str | None
    subject_id: str | None
    created_at: datetime
    read_at: datetime | None


class ListNotificationsOutput(BaseModel):
    """The page and the bell count in one answer.

    `unread_count` counts every unread row, not just the page, so the bell does
    not go quiet past the page size.
    """

    notifications: list[NotificationRow]
    unread_count: int


class MarkNotificationReadInput(MutatingToolInput):
    """One notification id, plus the idempotency key the base adds."""

    notification_id: uuid.UUID


class MarkNotificationReadOutput(BaseModel):
    """What the bell needs back: the entry, and when it was first seen."""

    notification_id: uuid.UUID
    kind: str
    title: str
    read_at: datetime


async def list_notifications(
    ctx: ToolContext, args: ListNotificationsInput
) -> ListNotificationsOutput:
    """The token user's own notifications, newest first, with their unread total.

    An empty bell is an empty page and zero, never `NOT_FOUND`: the tool takes
    no id, so nothing can be missing.
    """
    page = await list_for_user(
        ctx.session,
        user_id=ctx.identity.user_id,
        unread_only=args.unread_only,
        limit=args.limit,
    )
    return ListNotificationsOutput(
        notifications=[
            NotificationRow(
                notification_id=row.id,
                kind=row.kind,
                title=row.title,
                body=row.body,
                subject_type=row.subject_type,
                subject_id=row.subject_id,
                created_at=row.created_at,
                read_at=row.read_at,
            )
            for row in page.notifications
        ],
        unread_count=page.unread_count,
    )


async def mark_notification_read(
    ctx: ToolContext, args: MarkNotificationReadInput
) -> MarkNotificationReadOutput:
    """Stamp one of the token user's own notifications read.

    The time is set here, not by the caller. An already-read row keeps its
    stored time, so a second call cannot move when the person first saw it. A
    colleague's, another tenant's and an unknown id all give `NOT_FOUND` and
    stamp nothing.
    """
    row = await mark_read(
        ctx.session,
        user_id=ctx.identity.user_id,
        notification_id=args.notification_id,
        now=ctx.now,
    )
    assert row.read_at is not None, "mark_read either stamped this row or found it stamped"
    return MarkNotificationReadOutput(
        notification_id=row.id,
        kind=row.kind,
        title=row.title,
        read_at=row.read_at,
    )


__all__ = [
    "LIMIT_DEFAULT",
    "LIMIT_MAX",
    "LIMIT_MIN",
    "ListNotificationsInput",
    "ListNotificationsOutput",
    "MarkNotificationReadInput",
    "MarkNotificationReadOutput",
    "NotificationRow",
    "list_notifications",
    "mark_notification_read",
]
