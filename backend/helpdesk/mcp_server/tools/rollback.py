"""The `rollback_request` tool: undo an executed request within the window.

The rules and refusals live in the rollback service; this module holds the
wire contract, the tenant-scoped lookup and the clock. Only `it_admin` and
`platform_admin` may call it, both tenant-wide, so the pipeline's role check
is the whole permission check and existence is tested first.
"""

import uuid

from pydantic import BaseModel, Field

from helpdesk.mcp_server.services.access_requests.rollback import (
    WINDOW_HOURS as WINDOW_HOURS,  # re-exported for the registry
)
from helpdesk.mcp_server.services.access_requests.rollback import (
    rollback_request as rollback,
)
from helpdesk.mcp_server.tools.access import REASON_MAX_LENGTH
from helpdesk.mcp_server.tools.base import MutatingToolInput
from helpdesk.mcp_server.tools.context import ToolContext
from helpdesk.shared.access.approvals import lock_request
from helpdesk.shared.core.errors import NotFoundError
from helpdesk.shared.models import RequestStatus


class RollbackRequestInput(MutatingToolInput):
    """The rollback input. The request id is the only thing the caller picks.

    What it produced is read off the `grants` rows, never named by the caller.
    """

    request_id: uuid.UUID
    reason: str = Field(min_length=1, max_length=REASON_MAX_LENGTH)
    """Uses `revoke_access`'s cap, so a reason one tool takes the other never refuses."""


class RollbackRequestOutput(BaseModel):
    """What the undo ended, and where the request now stands."""

    request_id: uuid.UUID
    status: RequestStatus
    revoked_grant_ids: list[uuid.UUID]
    """Every live grant the request produced: onboarding can grant a whole
    template through one request.
    """

    revoked_count: int


async def rollback_request(ctx: ToolContext, args: RollbackRequestInput) -> RollbackRequestOutput:
    """Undo an executed request within the rollback window.

    Every refusal about the request itself is raised by the service, beside the
    writes it guards. `NOT_FOUND` for a missing or foreign request. The lookup
    also locks the row, so of two rollbacks the second waits, sees the request
    already rolled back and is refused, never revoking twice. `now` is read once
    and all writes share one transaction.
    """
    request = await lock_request(ctx.session, args.request_id)
    if request is None:
        raise NotFoundError("no such access request in this tenant")

    result = await rollback(
        ctx.session,
        ctx.directory,
        request=request,
        reason=args.reason,
        rolled_back_by_user_id=ctx.identity.user_id,
        actor_ref=str(ctx.identity.user_id),
        session_id=ctx.identity.session_id,
        now=ctx.now,
    )
    return RollbackRequestOutput(
        request_id=result.request.id,
        status=RequestStatus(result.request.status),
        revoked_grant_ids=[grant.id for grant in result.revoked_grants],
        revoked_count=len(result.revoked_grants),
    )


__all__ = [
    "WINDOW_HOURS",
    "RollbackRequestInput",
    "RollbackRequestOutput",
    "rollback_request",
]
