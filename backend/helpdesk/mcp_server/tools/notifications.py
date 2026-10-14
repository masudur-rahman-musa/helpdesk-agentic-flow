"""The notification tools: `list_notifications` and `mark_notification_read`.

An earlier change, the first door on a table four scheduler jobs have been filling
since an earlier change. Two tools and nothing else: the person's own page, and the
stamp that marks one entry read.

Both are `self_only` for all seven principals (the design, a design note), which
answers the scope question by itself — a notification is addressed to ONE
person, so there is no wider reach for a principal to hold. Neither input
carries a user field: the caller is the token's user and nothing else (hard
rule 6), pinned by
`test_mcp_notifications.py::test_the_list_input_declares_no_user_field_at_all`,
`::test_the_stamp_input_declares_only_the_key_and_the_notification_id` and
`::test_a_user_id_in_the_arguments_is_ignored_and_the_token_decides_whose_bell`,
whose named colleague is in the CALLER'S OWN tenant so honouring the argument
returns their rows rather than being caught by tenant scoping (standing
rule 9).

Neither tool is in the agent's catalogue (decision E): a tool the model can
call is model-driven behaviour and owes golden cases before it merges, and
nothing in row 23 needs the agent to read or clear somebody's bell — the page
asks the wire directly.

The whole decision logic lives in `helpdesk.notifications`; these two
functions are transport, the way every other tool module here is.
"""

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.mcp_server.core.identity import Identity
from helpdesk.mcp_server.services.notifications.list import list_for_user
from helpdesk.mcp_server.services.notifications.mark import mark_read
from helpdesk.mcp_server.tools.base import MutatingToolInput

_IGNORE_EXTRAS = ConfigDict(extra="ignore")
"""The read tool's contract: identity arrives on the token, so a spare
argument is data to discard rather than a reason to fail the call. The
mutating input below FORBIDS extras instead, through `MutatingToolInput`."""

LIMIT_MIN = 1
LIMIT_DEFAULT = 50
LIMIT_MAX = 200
"""Bounds on `list_notifications`, the same three numbers an earlier change's queue and
an earlier change's before it declared.

Rows go over the wire and nothing downstream bounds them, so an absent cap is
an unbounded response. Pinned exactly, and the default read off the input
model, by
`test_mcp_notifications.py::test_the_limit_bounds_are_exactly_one_fifty_and_two_hundred`
and `::test_the_limit_boundary_is_inclusive_at_1_and_200`."""


class ListNotificationsInput(BaseModel):
    """A page size and a filter. No user and no tenant argument at all.

    Extras are ignored rather than forbidden, the read tool's contract: an
    argument naming a user is data to discard, not a reason to fail a read.

    `unread_only` is a `bool` and not a status enum (decision F): `read_at` is
    a nullable timestamp rather than a vocabulary, so two states need no
    words, and a third (`all`) would be the default expressed twice. It
    narrows the ROWS only — `unread_count` is the same integer either way,
    pinned by
    `test_mcp_notifications.py::test_unread_only_true_narrows_the_rows_without_changing_the_unread_count`.
    """

    model_config = _IGNORE_EXTRAS

    unread_only: bool = False
    limit: int = Field(default=LIMIT_DEFAULT, ge=LIMIT_MIN, le=LIMIT_MAX)


class NotificationRow(BaseModel):
    """One notification as the person it names reads it.

    Exactly the eight fields the design's table can answer for: what happened,
    the sentence a job composed, the row it is about, when it arrived and
    whether it has been opened. `read_at` is null on an unread row, which is
    what the bell counts. Pinned as a literal key set by
    `test_mcp_notifications.py::test_each_principal_reads_its_own_bell_as_the_eight_key_row_and_two_key_envelope`.
    """

    notification_id: uuid.UUID
    kind: str
    title: str
    body: str
    subject_type: str | None
    subject_id: str | None
    created_at: datetime
    read_at: datetime | None


class ListNotificationsOutput(BaseModel):
    """The page and the bell in one answer (decision B).

    `unread_count` is every unread row this person holds and NOT the page's
    own tally: a bell reading the page would go quiet at fifty-one. Pinned by
    `test_mcp_notifications.py::test_the_unread_count_counts_every_unread_row_and_not_only_the_page`.
    """

    notifications: list[NotificationRow]
    unread_count: int


class MarkNotificationReadInput(MutatingToolInput):
    """One notification id, and the idempotency key `MutatingToolInput` adds.

    Forbidding extras is that base class's contract and matters for the usual
    reason: a `user_id` in a MUTATION's arguments is a caller who believes
    they are acting on somebody else, and dropping it silently would stamp a
    row for a different person than the one they named.

    The key is pre-checked against `idempotency_keys.key`'s own width by
    `helpdesk.idempotency.guard.run_once` before any effect runs, the same as
    every other mutating tool; its inclusive boundary is pinned by
    `test_mcp_notifications.py::test_the_idempotency_key_boundary_is_inclusive_at_key_length`,
    whose refused arm re-reads the row and finds it still unread.
    """

    notification_id: uuid.UUID


class MarkNotificationReadOutput(BaseModel):
    """What the bell needs back: the entry, and the moment it was first seen.

    Exactly four fields — the body and the subject are what the caller already
    had on the page, so echoing them would be a second copy of a row nobody
    re-renders. `read_at` is non-null by construction: the stamp either wrote
    it or the row already carried one. Pinned as a literal key set by
    `test_mcp_notifications.py::test_each_principal_stamps_its_own_notification_with_the_four_key_output`.
    """

    notification_id: uuid.UUID
    kind: str
    title: str
    read_at: datetime


async def list_notifications(
    session: AsyncSession, identity: Identity, args: ListNotificationsInput
) -> ListNotificationsOutput:
    """The token user's own notifications, newest first, with their unread total.

    The user is `identity.user_id` — resolved by the pipeline from the
    verified claims — and there is no argument it could come from instead.
    A colleague's rows are invisible with no argument at all and with one;
    another tenant's are invisible to every principal, which the aggregate is
    filtered explicitly for (`helpdesk.notifications.list`'s docstring says
    why a `func.count` cannot lean on the ORM guard).

    An empty bell answers `{"notifications": [], "unread_count": 0}` and never
    `NOT_FOUND`: the tool takes no id, so nothing can be missing, and "nothing
    is waiting for you" is a fact about the person rather than a refusal.
    Pinned by
    `test_mcp_notifications.py::test_an_empty_bell_answers_an_empty_page_and_zero_rather_than_not_found`.
    """
    page = await list_for_user(
        session,
        user_id=identity.user_id,
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
    session: AsyncSession, identity: Identity, args: MarkNotificationReadInput
) -> MarkNotificationReadOutput:
    """Stamp one of the token user's own notifications read.

    "Now" is read HERE and handed to `mark_read`, so a caller cannot claim
    they saw a message at a time of their choosing, and the domain's
    already-read branch answers the STORED timestamp rather than this one: a
    second call, with a fresh key or the same one, cannot move the moment a
    person first saw the message. The two guarantees are pinned separately
    (`test_mcp_notifications.py::test_the_same_idempotency_key_replays_the_stored_response`
    is the guard; `::test_two_different_keys_on_an_already_read_row_answer_the_same_read_at`
    is the branch, asserting two `tool.succeeded` events and no replay).

    A stranger's id, another tenant's and an unknown one are one answer,
    `NOT_FOUND`, and stamp nothing (decision G). `rbac.authorize` has already
    run in the pipeline and permits all seven principals here, so there is no
    scope check in this handler to confuse with existence: the row read is
    filtered by the token's user and the session guard by the tenant. Pinned
    by `::test_a_colleagues_notification_id_is_refused_not_found_and_stamps_nothing`,
    `::test_another_tenants_notification_id_is_refused_not_found_and_stamps_nothing`
    and `::test_an_unknown_notification_id_is_refused_not_found`.

    `read_at` on the output is non-optional while the column is nullable: the
    row this returns has been stamped either by this call or by an earlier
    one, so the narrowing is what the branch above guarantees rather than an
    assumption.
    """
    row = await mark_read(
        session,
        user_id=identity.user_id,
        notification_id=args.notification_id,
        now=datetime.now(UTC),
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
