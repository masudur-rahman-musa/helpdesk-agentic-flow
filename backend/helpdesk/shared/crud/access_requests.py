"""Access-request reads, plain and with names joined in so a page is one round trip."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.models import AccessRequest, Grant, RequestStatus, Role, System, User


@dataclass(frozen=True)
class RequestWithContext:
    """One request row plus the names the join found. Grant fields are None if not granted."""

    request: AccessRequest
    role_slug: str
    role_name: str
    system_slug: str
    system_name: str
    target_display_name: str
    grant_id: uuid.UUID | None
    expires_at: datetime | None


async def list_requests_with_context_for_requester(
    session: AsyncSession,
    requester_user_id: uuid.UUID,
    status: str | None,
    limit: int,
) -> Sequence[RequestWithContext]:
    """Requests filed by this user, with names and any grant, oldest first.

    Filtered by requester, so an on-behalf request belongs to the manager.
    Whole rows are selected so the tenant guard scopes all five tables. The
    grant join is a LEFT join, so ungranted requests are kept.
    """
    statement = (
        select(AccessRequest, Role, System, User, Grant)
        .join(Role, Role.id == AccessRequest.role_id)
        .join(System, System.id == Role.system_id)
        .join(User, User.id == AccessRequest.target_user_id)
        .outerjoin(Grant, Grant.request_id == AccessRequest.id)
        .where(AccessRequest.requester_user_id == requester_user_id)
    )
    if status is not None:
        statement = statement.where(AccessRequest.status == status)
    result = await session.execute(
        statement.order_by(AccessRequest.created_at, AccessRequest.id).limit(limit)
    )
    return [
        RequestWithContext(
            request=request,
            role_slug=role.slug,
            role_name=role.name,
            system_slug=system.slug,
            system_name=system.name,
            target_display_name=target.display_name,
            grant_id=None if grant is None else grant.id,
            expires_at=None if grant is None else grant.expires_at,
        )
        for request, role, system, target, grant in result.all()
    ]


async def fetch_requests_for_requester(
    session: AsyncSession,
    requester_user_id: uuid.UUID,
    status: str | None,
) -> list[AccessRequest]:
    """Requests filed by this user, oldest first."""
    statement = select(AccessRequest).where(AccessRequest.requester_user_id == requester_user_id)
    if status is not None:
        statement = statement.where(AccessRequest.status == status)
    result = await session.execute(statement.order_by(AccessRequest.created_at, AccessRequest.id))
    return list(result.scalars().all())


async def fetch_approved_role_ids_for_target(
    session: AsyncSession, target_user_id: uuid.UUID
) -> frozenset[uuid.UUID]:
    """Role ids approved for this user but not yet granted; a granted one shows as a grant."""
    result = await session.execute(
        select(AccessRequest).where(
            AccessRequest.target_user_id == target_user_id,
            AccessRequest.status == RequestStatus.APPROVED.value,
        )
    )
    return frozenset(request.role_id for request in result.scalars())


async def fetch_open_request(
    session: AsyncSession, target_user_id: uuid.UUID, role_id: uuid.UUID
) -> AccessRequest | None:
    """The oldest pending or approved request for this user and role, if any.

    Executed, rejected and failed ones are over, so asking again is a new
    request (`test_open_request_lookup.py`).
    """
    result = await session.execute(
        select(AccessRequest)
        .where(
            AccessRequest.target_user_id == target_user_id,
            AccessRequest.role_id == role_id,
            AccessRequest.status.in_((RequestStatus.PENDING.value, RequestStatus.APPROVED.value)),
        )
        .order_by(AccessRequest.created_at, AccessRequest.id)
        .limit(1)
    )
    return result.scalars().one_or_none()


async def lock_open_requests_for_target(
    session: AsyncSession, target_user_id: uuid.UUID
) -> list[AccessRequest]:
    """Pending or approved requests for this user, locked until the transaction ends.

    A fixed order, so two runs write their closing events in the same order.
    """
    result = await session.execute(
        select(AccessRequest)
        .where(
            AccessRequest.target_user_id == target_user_id,
            AccessRequest.status.in_((RequestStatus.PENDING.value, RequestStatus.APPROVED.value)),
        )
        .order_by(AccessRequest.created_at, AccessRequest.id)
        .with_for_update()
    )
    return list(result.scalars().all())
