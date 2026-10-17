"""Pydantic argument shapes for the notification routes."""

import uuid

from pydantic import BaseModel


class ListNotificationsArgs(BaseModel):
    """`list_notifications` with neither the unread filter nor a page size.

    The tool's defaults then apply. The narrower shapes are subclasses, not
    nullable fields, because `model_dump` would send `null` and the tool
    refuses it.
    """


class ListNotificationsArgsWithUnreadOnly(ListNotificationsArgs):
    """Only unread ones, at the tool's page size. This is the bell's view."""

    unread_only: bool


class ListNotificationsArgsWithLimit(ListNotificationsArgs):
    """The page at the caller's own size, read and unread alike."""

    limit: int


class ListNotificationsArgsWithUnreadOnlyAndLimit(ListNotificationsArgs):
    """Both the unread filter and a page size."""

    unread_only: bool
    limit: int


class MarkNotificationReadArgs(BaseModel):
    """`mark_notification_read`'s arguments, built by the route.

    The route makes the idempotency key, so a reused browser key cannot clash.
    No reader field: the tool reads the reader from the token.
    """

    notification_id: uuid.UUID
    idempotency_key: str
