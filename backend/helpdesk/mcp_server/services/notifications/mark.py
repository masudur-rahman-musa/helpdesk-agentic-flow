"""`mark_read`: the one writer of `notifications.read_at`.

`read_at` is the only mutable field of that table, and this is the only thing
that moves it. The column's own docstring in `models/notification.py` says row
23's route stamps it; this is that stamp, in the shared library rather than in
the tool, so the scheduler or a later route writes through the same branch.
"""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.errors import NotFoundError
from helpdesk.shared.models.notification import Notification


async def mark_read(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    notification_id: uuid.UUID,
    now: datetime,
) -> Notification:
    """Stamp this user's own unread notification, or answer the stamp it has.

    The clock is a PARAMETER and never called here, which is what makes the
    second half of the idempotence assertable: a second call handed a
    different clock answers the first timestamp, because the already-read
    branch returns before any write. Without that, "when did this person first
    see this message" would drift forward on every click of a bell. Pinned by
    `test_notifications.py::test_mark_read_stamps_the_clock_it_was_handed_and_a_second_clock_moves_nothing`,
    which passes two different clocks and asserts one timestamp.

    That branch is a distinct guarantee from the idempotency guard's replay:
    the guard answers a second call carrying the SAME key, and this answers a
    row already stamped whatever the key. Both are pinned, at the transport,
    by `test_mcp_notifications.py::test_the_same_idempotency_key_replays_the_stored_response`
    and `::test_two_different_keys_on_an_already_read_row_answer_the_same_read_at`
    — the second asserting two `tool.succeeded` events and no replay, so the
    guard cannot be what passes it.

    A row that is absent, belongs to another user of this tenant, or belongs to
    another tenant is ONE answer, `NOT_FOUND`, and nothing is stamped (plan
    23ga decision G): the read is filtered by `user_id` and the session guard
    filters the tenant, so the id stays a non-capability and `FORBIDDEN` would
    confirm that a given uuid names a real notification somewhere. The refusal
    precedes any write, which the cases prove by re-reading the row and finding
    `read_at` still null —
    `::test_mark_read_refuses_a_colleagues_notification_and_stamps_nothing` and
    `::test_mark_read_refuses_an_unknown_notification_id_with_not_found`.
    """
    result = await session.execute(
        select(Notification).where(
            Notification.id == notification_id, Notification.user_id == user_id
        )
    )
    row = result.scalars().one_or_none()
    if row is None:
        raise NotFoundError(f"no notification {notification_id} for this user")
    if row.read_at is not None:
        return row

    row.read_at = now
    await session.flush()
    return row
