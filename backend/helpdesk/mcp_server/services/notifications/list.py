"""`list_for_user`: one person's own bell, with the unread total beside it.

The first read of the `notifications` table. `notify` has been
filling it since an earlier change and four scheduler jobs write into it; nothing until
now read a row back.

The page and the count come from ONE call because they are one thing a caller
needs: a bell showing a number and a list showing rows
are two renderings of the same answer, and a second tool for the integer would
be a second RBAC cell and a second scope check to keep in step. `limit` bounds
the ROWS only — `unread_count` is every unread row that person holds, which is
what stops the bell going quiet at fifty-one. Pinned by
`test_notifications.py::test_the_unread_count_counts_beyond_the_page`.

No token is read here and no tenant is passed: the tenant comes off the
session, the way it does in `notify` and everywhere else in this package.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.models.notification import Notification


@dataclass(frozen=True)
class NotificationPage:
    """One person's rows and their whole unread total.

    A frozen dataclass around the ORM rows rather than a Pydantic model
    repeating the eight columns, exactly as `access/reviews.py`'s
    `ReviewWithCounts` is: the design's wire shape is
    `mcp_server.tools.notifications.NotificationRow`, and a second model here
    would be two contracts to keep in step.

    `unread_count` is NOT `len(notifications)` and is not derivable from it:
    the rows are a page and the count is the person's whole unread set, which
    is the distinction
    `test_notifications.py::test_the_unread_count_counts_beyond_the_page`
    exists to hold.
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

    Ordered by `created_at` DESCENDING with the notification id descending
    breaking a tie, so the order is TOTAL and two calls answer the same page:
    the expiry sweep writes a holder's row and their manager's inside one
    transaction, so two rows sharing an instant is the normal case rather than
    a coincidence. Pinned as an exact id sequence by
    `test_notifications.py::test_the_page_is_newest_first_with_the_notification_id_breaking_a_tie`,
    whose tied pair fails an untied order.

    `unread_only` narrows the ROWS and never the count: both answers carry the
    same `unread_count`, because a bell that changed its number when the page
    was filtered would contradict the page it sits on. Pinned by
    `::test_unread_only_narrows_the_rows_and_leaves_the_unread_count_alone`.

    A person with nothing answers no rows and a zero count — never a refusal.
    The read takes no notification id, so there is nothing here that could be
    missing, and an empty bell is a fact about the person.
    Pinned by `::test_a_user_with_no_notifications_answers_no_rows_and_a_zero_count`.

    The row read is filtered by `user_id` and left to the session guard for the
    tenant, since it loads the mapped entity. The COUNT cannot be: the guard
    injects its criteria through `with_loader_criteria`, which applies to the
    entities a statement loads, and an aggregate loads none — so the tenant
    clause is written out, the shape `core/repository.py::count_live_grants`
    already uses for the same reason. `bound_tenant_id` raises with no tenant
    bound, so the unbound case stays a loud failure rather than a full-table
    count. Pinned by
    `::test_list_for_user_answers_nothing_at_all_for_another_tenants_user`,
    which asserts the COUNT as well as the row set over a real foreign row.
    """
    rows = select(Notification).where(Notification.user_id == user_id)
    if unread_only:
        rows = rows.where(Notification.read_at.is_(None))
    page = await session.execute(
        rows.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit)
    )

    unread = await session.execute(
        select(func.count(Notification.id)).where(
            Notification.tenant_id == bound_tenant_id(session),
            Notification.user_id == user_id,
            Notification.read_at.is_(None),
        )
    )
    return NotificationPage(
        notifications=list(page.scalars().all()),
        unread_count=int(unread.scalar_one()),
    )
