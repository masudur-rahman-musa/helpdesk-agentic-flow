"""Approval-queue reads: lock one request, or fetch the pending set."""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from helpdesk.shared.models import AccessRequest, RequestStatus, Role, System, User


async def lock_request(session: AsyncSession, request_id: uuid.UUID) -> AccessRequest | None:
    """The request, locked for this transaction; None if missing or another tenant's.

    The lock makes two approvers on the same last seat take turns.
    """
    result = await session.execute(
        select(AccessRequest).where(AccessRequest.id == request_id).with_for_update()
    )
    return result.scalars().one_or_none()


async def fetch_pending_with_context(
    session: AsyncSession,
    predicates: Sequence[ColumnElement[bool]],
) -> list[Any]:
    """Pending requests matching any scope predicate, with role, system and both people.

    The caller filters seats afterwards. `predicates` must not be empty.
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
