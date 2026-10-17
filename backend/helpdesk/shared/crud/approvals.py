"""Approval-queue reads: locking a request and fetching the pending set.

Every statement against `access_requests` that the approval flow needs lives
here, so `shared/access/approvals.py` builds no statement of its own. The
tenant guard in `helpdesk.core.db` scopes every read to the bound session.
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from helpdesk.shared.models import AccessRequest, RequestStatus, Role, System, User


async def lock_request(session: AsyncSession, request_id: uuid.UUID) -> AccessRequest | None:
    """The bound tenant's request, locked for the rest of this transaction.

    `SELECT … FOR UPDATE` through the ORM, so the tenant guard still injects
    its filter: a row in another tenant reads exactly like a row that does
    not exist (both None), and the id is not an existence oracle. Pinned by
    `test_access_approvals.py::test_locking_answers_the_same_way_for_another_tenants_row_and_a_missing_one`.

    The lock is what serialises two approvers deciding the same last seat
    (decision F): the second waits, then re-reads a row that is no longer
    `pending`.
    """
    result = await session.execute(
        select(AccessRequest).where(AccessRequest.id == request_id).with_for_update()
    )
    return result.scalars().one_or_none()


async def fetch_pending_with_context(
    session: AsyncSession,
    predicates: Sequence[ColumnElement[bool]],
) -> list[Any]:
    """The scope-filtered pending requests with their role, system and both people.

    FOUR joins in one statement — role, system, target, and an alias of `users`
    for the requester — returning one tuple per pending request. The caller
    (``access.approvals.list_pending_with_context_for_approver``) applies the
    per-row seat filter and scope re-check in Python after this read, which is
    why the WHERE only narrows by status and the SQL scope predicates.

    `predicates` is the already-computed list of scope `ColumnElement`s: the
    caller builds them from scope information it holds so that this function
    stays a pure data accessor without scope logic in it.

    An empty `predicates` must never reach here (the caller returns early when
    none are produced), because an empty `or_()` would raise. Pinned indirectly
    by `test_mcp_approvals.py::test_several_pending_seats_come_back_once_each_despite_the_joins`.
    """
    requester = aliased(User)
    result = await session.execute(
        select(AccessRequest, Role, System, User, requester)
        .join(User, User.id == AccessRequest.target_user_id)
        .join(Role, Role.id == AccessRequest.role_id)
        .join(System, System.id == Role.system_id)
        .join(requester, requester.id == AccessRequest.requester_user_id)
        .where(AccessRequest.status == RequestStatus.PENDING.value, or_(*predicates))
        .order_by(AccessRequest.created_at, AccessRequest.id)
    )
    return list(result.all())
