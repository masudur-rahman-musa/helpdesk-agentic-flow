"""The `rollback_request` tool: the design's Admin group, the design's undo.

An executed request, within the domain's `WINDOW_HOURS` of the grant, taken
back in full. Everything it decides lives in `helpdesk.access.rollback`,
refusals included: what lives here is the wire contract, the tenant-scoped
lookup and the single clock read. `WINDOW_HOURS` is re-exported so the
registry's description says the same number the refusal does.

There is NO scope helper on this tool, unlike `revoke_access` and the review
tools. the design gives the row `it_admin` and `platform_admin` only, both at
`Scope.ANY`, so there is no narrower scope for a helper to compare and the
pipeline's matrix stage IS the whole permission check. That makes the denial
case its own kill: the handler never re-runs
`rbac.authorize`, so deleting the pipeline stage changes the answer for the
five principals the design gives no cell, pinned by
`test_mcp_rollback_request.py::test_the_five_denied_principals_roll_nothing_back`.

Because permission is settled before the handler runs, this tool tests
EXISTENCE first and belongs in `test_mcp_rbac_matrix.py::ALLOW_ANSWERS_NOT_FOUND`
rather than `SCOPE_BEFORE_EXISTENCE_TOOLS`: a permitted
caller sending an id that does not exist must read `NOT_FOUND`.

The registry declares the generic `tool.succeeded`: the handler's own domain
call appends one `grant.revoked` and one `grant.rolled_back` per revoked row
(decision E), each naming the GRANT, and the pipeline's event lands last as
the summary — the same shape as an earlier change's `grant.revoked`.

No function here opens a session or touches the transport: the pipeline binds
the session to the token's tenant before the handler is called.
"""

import uuid

from pydantic import BaseModel, Field

from helpdesk.mcp_server.services.access_requests.rollback import (
    WINDOW_HOURS as WINDOW_HOURS,  # re-exported for the registry
)
from helpdesk.mcp_server.services.access_requests.rollback import (
    RequestNotExecutedError as RequestNotExecutedError,  # re-exported for the tests
)
from helpdesk.mcp_server.services.access_requests.rollback import (
    RollbackWindowExpiredError as RollbackWindowExpiredError,  # re-exported for the tests
)
from helpdesk.mcp_server.services.access_requests.rollback import (
    rollback_request as rollback,
)
from helpdesk.mcp_server.tools.access import REASON_MAX_LENGTH
from helpdesk.mcp_server.tools.base import MutatingToolInput
from helpdesk.mcp_server.tools.context import ToolContext
from helpdesk.shared.core.errors import NotFoundError
from helpdesk.shared.models import RequestStatus
from helpdesk.shared.services.access.approvals import lock_request


class RollbackRequestInput(MutatingToolInput):
    """the design's signature. `extra="forbid"` comes from `MutatingToolInput`.

    No tenant and no user argument: both are the token's. The
    request id is the only thing the caller chooses, and what it produced is
    read off the `grants` rows rather than named.
    """

    request_id: uuid.UUID
    reason: str = Field(min_length=1, max_length=REASON_MAX_LENGTH)
    """Bounded against `revoke_access`'s own constant, IMPORTED rather than
    redeclared: both tools write a caller's sentence about
    ending access, and two spellings of one bound would let a reason that one
    tool accepts be refused by the other. Pre-checked by the boundary before
    any effect runs, inclusively at 1 and 2000, pinned by
    `test_mcp_rollback_request.py::test_the_reason_boundary_is_inclusive_at_1_and_2000`,
    which reads the number off `mcp_server.tools.access.REASON_MAX_LENGTH`."""


class RollbackRequestOutput(BaseModel):
    """What the undo ended, and where the request now stands."""

    request_id: uuid.UUID
    status: RequestStatus
    revoked_grant_ids: list[uuid.UUID]
    """EVERY live row the request produced, not just the first: an earlier change's
    onboarding executes a whole template through one request, so a shorter
    output would misreport how much access was taken back."""

    revoked_count: int


async def rollback_request(ctx: ToolContext, args: RollbackRequestInput) -> RollbackRequestOutput:
    """Undo an executed request within the design's 24 hours.

    This handler owns exactly two things: the lookup and the context's clock.
    Every
    refusal about the request itself — its status, the 24h window, whether
    anything live is left — is raised by `helpdesk.access.rollback`, beside
    the writes it guards, so a second caller of that exported function cannot
    get the writes without the guards.

    `NOT_FOUND` — no such request, or one in another tenant. `lock_request`
    runs through the tenant-bound session, so the two are indistinguishable
    and a request id is not an existence oracle
    (`test_mcp_rollback_request.py::test_a_missing_and_another_tenants_request_answer_the_same_not_found`).
    It takes the row's lock at the same time, which is what serialises two
    rollbacks of one request: the loser waits, then re-reads a row the winner
    has already moved to `rolled_back` and is refused `POLICY_BLOCKED` by the
    domain's status test — never `FORBIDDEN`, and never a second revocation.
    Two callers send two idempotency keys, so the guard cannot serialise them.
    `::test_two_callers_racing_the_same_rollback_undo_it_once` pins that
    outcome but does NOT kill the lock (it passes with `with_for_update()`
    deleted, because two client calls rarely overlap); its docstring carries
    the reason.

    The three `POLICY_BLOCKED` refusals and the case that separates each are
    documented on `helpdesk.access.rollback.rollback_request`.

    `now` is read ONCE and passed down, so the window test, the locked
    liveness read and every `revoked_at` answer for one instant. Everything
    runs in the transaction the pipeline opened, so the revoked rows, both
    events per row and the status write commit or roll back together.
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
    "RequestNotExecutedError",
    "RollbackRequestInput",
    "RollbackRequestOutput",
    "RollbackWindowExpiredError",
    "rollback_request",
]
