"""Pydantic argument shapes for the notification routes."""

import uuid

from pydantic import BaseModel


class ListNotificationsArgs(BaseModel):
    """`list_notifications` with neither the unread filter nor a page size.

    An absent `unread_only` means the tool's own `False` — read and unread
    alike — and an absent limit means its own `LIMIT_DEFAULT`. Repeating either
    here would be a second place for them to drift from,
    which is why the three narrower shapes below are SUBCLASSES rather than
    nullable fields: `model_dump` carries every declared field, so a
    `limit: int | None = None` would send `limit: null` into a bound of 1..200
    and answer `VALIDATION`, and an `unread_only: bool | None = None` would
    send `unread_only: null` into a `bool`. Pinned shape by shape by
    `test_notification_routes.py::test_the_page_route_sends_each_query_parameter_only_when_it_is_asked_for`
    and
    `::test_the_page_route_sends_neither_unread_only_nor_limit_when_neither_is_asked_for`.
    """


class ListNotificationsArgsWithUnreadOnly(ListNotificationsArgs):
    """The page filtered to the unread set, at the tool's own page size.

    The bell's own view: 23ga put this filter on the tool because the unread
    set is what a bell renders, and a proxy that could not ask for it would
    make the filter unreachable from the only caller there is (decision D).
    """

    unread_only: bool


class ListNotificationsArgsWithLimit(ListNotificationsArgs):
    """The page at the caller's own size, read and unread alike."""

    limit: int


class ListNotificationsArgsWithUnreadOnlyAndLimit(ListNotificationsArgs):
    """Both, the shape 23gc's filtered paging sends."""

    unread_only: bool
    limit: int


class MarkNotificationReadArgs(BaseModel):
    """`mark_notification_read`'s the design arguments, assembled by this router.

    `idempotency_key` is DERIVED here and never accepted from the browser
    (decision B): a client-supplied key is an identifier the client is free to
    reuse across two different notifications, which is `IDEMPOTENCY_CONFLICT`
    taken by accident. There is no reader field, by a project rule — the tool
    reads the reader from the same bearer this router forwards. These two keys
    and nothing else are asserted as an exact set by
    `test_notification_routes.py::test_the_stamp_route_answers_the_tools_output_under_the_callers_own_bearer`.
    """

    notification_id: uuid.UUID
    idempotency_key: str
