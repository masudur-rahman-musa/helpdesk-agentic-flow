"""Access-request reads that carry a request's context.

The widened twin of `helpdesk.access.requests`: the same request rows, with
the role, system, target and any produced grant named in one statement so a
page of requests stays one round trip.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.models import AccessRequest, Grant, Role, System, User


@dataclass(frozen=True)
class RequestWithContext:
    """One `access_requests` row with everything a reader needs named.

    A frozen dataclass around the ORM row rather than a Pydantic model
    duplicating its nine columns: the wire shape is
    `mcp_server.tools.access.AccessRequestSummary`, which the design owns, and
    a second nineteen-field model here would be two contracts to keep in step.
    What this carries beside the row is exactly what the JOIN answered and the
    row cannot: the role's and system's names, the target's display name, and
    the grant the request produced.

    `grant_id` and `expires_at` are `None` for a request nothing granted
    (the join is a LEFT one), pinned by
    `test_mcp_access_read_tools.py::test_the_grant_join_is_null_when_pending_and_carries_the_grant_when_executed`.
    """

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
    """The bound tenant's requests filed BY this user, named and with their grant.

    The widened twin of `helpdesk.access.list_requests_for_requester`, and
    filtered the same way — by REQUESTER, so an on-behalf request belongs to
    the manager who filed it. `status` arrives as the already-validated
    `RequestStatus` value, so the query never sees free text.

    Four joins in ONE statement, so a page of N requests stays one round trip
    rather than N directory reads: the role, the role's system and the target
    user are inner joins (every request row names all three), and the grant is
    a LEFT join on `grants.request_id` because most requests have none. The
    LEFT half survives the tenant guard because `with_loader_criteria` appends
    its filter to the join's ON clause rather than to the WHERE, so an
    ungranted request is not silently dropped.

    Whole entities are selected, not columns, for the same reason
    `list_active_grants_for_user` does it: the guard in `helpdesk.core.db`
    injects its filter per mapped entity, so all five tables are scoped by the
    session rather than by this `where`.

    One row per request, never multiplied by the join: a request can have at
    most one grant pointing back at it, pinned by
    `test_mcp_access_read_tools.py::test_several_requests_come_back_once_each_despite_the_grant_join`.

    `limit` is a bound the CALLER validated (`REQUESTS_LIMIT_MIN`/`MAX`), and
    it truncates the OLDEST-first order below, so a caller holding more than
    `limit` requests sees their earliest ones. Stated rather than assumed:
    the order is `created_at` then id, the same stable pair the narrow twin
    uses.
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
