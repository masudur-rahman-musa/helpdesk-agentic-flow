"""Access-request reads for the tools.

Lives here rather than in `core/repository.py` so the access domain owns
its own queries; the sessions come from `tenant_session`, so the guard
scopes every read to the bound tenant.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.models import AccessRequest, RequestStatus


async def list_requests_for_requester(
    session: AsyncSession,
    requester_user_id: uuid.UUID,
    status: RequestStatus | None = None,
) -> Sequence[AccessRequest]:
    """The bound tenant's requests filed BY this user, oldest first.

    Filtered by requester, not target: `list_my_requests` answers "what did
    I ask for", and an on-behalf request belongs to the manager
    who filed it, not to the report it names. `status` is already a
    `RequestStatus` member — the boundary validated it — so the query never
    sees free text. The order (`created_at`, then id for a stable tie) is a
    reading convenience, not a promise callers may rely on.
    """
    statement = select(AccessRequest).where(AccessRequest.requester_user_id == requester_user_id)
    if status is not None:
        statement = statement.where(AccessRequest.status == status.value)
    result = await session.execute(statement.order_by(AccessRequest.created_at, AccessRequest.id))
    return result.scalars().all()


async def list_approved_role_ids_for_target(
    session: AsyncSession, target_user_id: uuid.UUID
) -> frozenset[uuid.UUID]:
    """Role ids approved for this user but not yet executed (the design).

    Exactly the rows whose status is `approved`: an executed request shows
    up as a grant instead, and every other status never conferred anything.
    The status constant is read off `RequestStatus`, so a renamed member
    fails loudly here rather than silently emptying the set.
    """
    result = await session.execute(
        select(AccessRequest).where(
            AccessRequest.target_user_id == target_user_id,
            AccessRequest.status == RequestStatus.APPROVED.value,
        )
    )
    return frozenset(request.role_id for request in result.scalars())
