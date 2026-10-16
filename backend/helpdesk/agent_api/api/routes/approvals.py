"""GET /me/approvals and the two decisions the queue can take.

The same three MCP tools the agent already calls, reached over HTTP instead of
over a turn (23a decision A, unchanged). Nothing here reads the database and
nothing here decides anything: the call crosses the MCP boundary, so RBAC, the
kill switch, the seat rules and the audit event apply to a decision the browser
makes exactly as they do to one the model makes. a project rule holds as written —
the mutation is a typed MCP tool behind RBAC and this module is transport.

**Identity comes from the token and from nowhere else**. The
caller's own bearer is forwarded verbatim, no route takes a user argument, and
the approver in the derived key below is read from the VERIFIED claims rather
than from anything the browser sent. Pinned by
`test_approval_routes.py::test_the_approve_route_answers_the_tools_output_under_the_callers_own_bearer`,
which asserts the arguments as an exact key set so an `approver_user_id` riding
along fails the case.

**The body is the tool's structured output, unchanged**, for the same reason
the `/me` reads are: a route that reshaped on the way out would be a second
contract for 23d's page to track.

**The comment and reason bounds are the TOOL's** (decision E). Re-declaring
them here would refuse at FastAPI's untyped 422 a value the tool accepts, and
the design's `VALIDATION` is what the page has a sentence for.

Routing reads as what it is: the queue is mine (`GET /me/approvals`), the
decision acts on somebody else's request (`POST /approvals/{request_id}/…`).

No lifespan resource is touched: these routes read nothing off `app.state`,
which is why their tests enter no lifespan and open no socket.
"""

import uuid

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from helpdesk.agent_api.agent.keys import derive_decision_key
from helpdesk.agent_api.core.deps import request_claims
from helpdesk.agent_api.schemas.approvals import (
    ApproveBody,
    ApproveRequestArgs,
    ListPendingApprovalsArgs,
    ListPendingApprovalsArgsWithLimit,
    RejectBody,
    RejectRequestArgs,
)
from helpdesk.agent_api.services.tool_proxy import call_tool_route

router = APIRouter()

APPROVE_VERB = "approve"
REJECT_VERB = "reject"
"""The two path segments, and the two verbs the decision key is taken over.

One constant per verb, used by the route and by the key alike, so the segment
a browser posts to and the material the key digests cannot drift apart. Pinned
as two DIFFERENT keys for one approver and one request by
`test_approval_routes.py::test_the_reject_route_answers_the_tools_output_under_the_callers_own_bearer`."""


@router.get("/me/approvals")
async def my_approvals(
    request: Request,
    limit: int | None = Query(default=None),
) -> JSONResponse:
    """Every pending request waiting on THIS caller, oldest first.

    The queue's whole filter is "waiting for me", and that "me" is the token's
    user: the tool declares no user argument and this route sends none. An
    absent `limit` sends no argument at all, so the tool's own default applies
    and a page cannot silently show a shorter queue than the caller has
    (`test_approval_routes.py::test_the_queue_route_sends_no_limit_when_none_is_asked_for`).
    """
    arguments = (
        ListPendingApprovalsArgs()
        if limit is None
        else ListPendingApprovalsArgsWithLimit(limit=limit)
    )
    return await call_tool_route(request, "list_pending_approvals", arguments)


@router.post("/approvals/{request_id}/approve")
async def approve(
    request_id: uuid.UUID,
    request: Request,
    body: ApproveBody | None = None,
) -> JSONResponse:
    """Record this caller's approval of one seat of one request's chain.

    `request_id` is a `uuid.UUID` path parameter, so a non-uuid path never
    reaches a tool, a key or a query — it would otherwise digest happily into
    a perfectly valid key filed against nothing
    (`::test_a_non_uuid_request_id_reaches_no_tool_and_no_key`).

    The approver the key is derived from is read off the verified claims on
    `request.state`, which `RequireAuthMiddleware` put there after the
    one verify every request gets; the browser's own `approver_user_id` never
    decides it (a project rule,
    `::test_the_approve_route_answers_the_tools_output_under_the_callers_own_bearer`).
    """
    claims = request_claims(request)

    arguments = ApproveRequestArgs(
        request_id=request_id,
        comment=None if body is None else body.comment,
        idempotency_key=derive_decision_key(
            approver_user_id=uuid.UUID(claims.sub), request_id=request_id, verb=APPROVE_VERB
        ),
    )
    return await call_tool_route(request, "approve_request", arguments)


@router.post("/approvals/{request_id}/reject")
async def reject(
    request_id: uuid.UUID,
    body: RejectBody,
    request: Request,
) -> JSONResponse:
    """Refuse one request, with the reason the audit record keeps.

    The key is derived over `REJECT_VERB`, so an Approve and a Reject by one
    approver on one request are two different keys: reusing the approve key
    would replay an approval's stored response for a rejection, which is the
    worst failure this derivation exists to prevent.

    The approver is read off the verified claims on `request.state`, the same
    way `approve` above reads it.
    """
    claims = request_claims(request)

    arguments = RejectRequestArgs(
        request_id=request_id,
        reason=body.reason,
        idempotency_key=derive_decision_key(
            approver_user_id=uuid.UUID(claims.sub), request_id=request_id, verb=REJECT_VERB
        ),
    )
    return await call_tool_route(request, "reject_request", arguments)
