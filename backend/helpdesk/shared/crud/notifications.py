"""Notification reads: page and unread count.

Every statement against `notifications` for the list and mark-read paths lives
here, so the service modules in `mcp_server/services/notifications/` build no
statement of their own.  The tenant guard in `helpdesk.core.db` scopes the ORM
read to the bound session; the count writes the tenant clause explicitly for the
same reason `fetch_item_counts` in `crud/review_queries.py` does.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.models.notification import Notification


async def fetch_notification_page(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    unread_only: bool,
    limit: int,
) -> list[Notification]:
    """This user's notifications, newest first, at most `limit` of them.

    Ordered by `created_at` descending then id descending: the expiry sweep
    writes a holder's row and their manager's inside one transaction, so two
    rows sharing an instant is the normal case rather than a coincidence.
    `unread_only` narrows only the page rows, never the count (the caller
    reads both and must keep them consistent).  Pinned by
    `test_notifications.py::test_the_page_is_newest_first_with_the_notification_id_breaking_a_tie`.
    """
    statement = select(Notification).where(Notification.user_id == user_id)
    if unread_only:
        statement = statement.where(Notification.read_at.is_(None))
    result = await session.execute(
        statement.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit)
    )
    return list(result.scalars().all())


async def count_unread_notifications(session: AsyncSession, user_id: uuid.UUID) -> int:
    """How many unread notifications this user holds in the bound tenant.

    The tenant clause is written out rather than left to the ORM guard because
    the guard's `with_loader_criteria` applies to entities a statement LOADS and
    a COUNT aggregate loads none.  `bound_tenant_id` raises with no tenant bound,
    so the unbound case stays a loud failure rather than a full-table count.
    Pinned by
    `test_notifications.py::test_list_for_user_answers_nothing_at_all_for_another_tenants_user`.
    """
    result = await session.execute(
        select(func.count(Notification.id)).where(
            Notification.tenant_id == bound_tenant_id(session),
            Notification.user_id == user_id,
            Notification.read_at.is_(None),
        )
    )
    return int(result.scalar_one())


async def fetch_notification_for_user(
    session: AsyncSession,
    *,
    notification_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Notification | None:
    """This user's own notification with the given id, or None.

    Filtered by both `notification_id` and `user_id` so a row belonging to
    another user of the same tenant reads as absent, and the id is not a
    capability.  The session guard adds the tenant filter.  Pinned by
    `test_notifications.py::test_mark_read_refuses_a_colleagues_notification_and_stamps_nothing`.
    """
    result = await session.execute(
        select(Notification).where(
            Notification.id == notification_id, Notification.user_id == user_id
        )
    )
    return result.scalars().one_or_none()
