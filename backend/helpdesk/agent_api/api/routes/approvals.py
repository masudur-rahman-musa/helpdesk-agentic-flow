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

**After a final decision, the paused chat turn is resumed**. The
`resume_after_decision` seam below is what tests replace; in production it
builds `TurnDeps` from `app.state` and calls `resume_turn`. `ResumeRefusedError`
is swallowed here so the approver still receives the decision tool's own result.
"""

import contextlib
import uuid
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Query, Request

from helpdesk.agent_api.agent.gates import DECIDED_RESULTS
from helpdesk.agent_api.agent.keys import derive_decision_key
from helpdesk.agent_api.agent.resume import ResumeRefusedError
from helpdesk.agent_api.core.deps import request_claims
from helpdesk.agent_api.schemas.approvals import (
    ApproveBody,
    ApproveRequestArgs,
    ListPendingApprovalsArgs,
    ListPendingApprovalsArgsWithLimit,
    RejectBody,
    RejectRequestArgs,
)
from helpdesk.agent_api.services import approvals as approvals_service
from helpdesk.agent_api.services.tool_proxy import proxy_tool

router = APIRouter()

resume_after_decision = approvals_service.resume_after_decision
"""Resume the paused chat turn that filed this request.

Module-level seam: tests patch this name to a spy so they can verify the
trigger without entering the lifespan or opening a socket. In
production it calls `services.approvals.resume_after_decision`, which builds
`TurnDeps` from `app.state` and calls `resume_turn`.

Called only when the tool body carries a status in `gates.DECIDED_RESULTS`
(`approved`, `executed`, `rejected`, `failed`). A `pending` seat never
triggers it. `ResumeRefusedError` is caught in the caller below so the approver
gets the decision's own 200 even when the request was filed outside a turn.
Pinned by `test_approval_resume_wiring.py`.
"""

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
) -> Mapping[str, Any]:
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
    return await proxy_tool(request, "list_pending_approvals", arguments)


@router.post("/approvals/{request_id}/approve")
async def approve(
    request_id: uuid.UUID,
    request: Request,
    body: ApproveBody | None = None,
) -> Mapping[str, Any]:
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

    After the tool returns, the paused chat turn is resumed if the decision is
    final. `ResumeRefusedError` is swallowed: a request filed outside
    a turn has no paused thread, and the approver's result should not change.
    """
    claims = request_claims(request)

    arguments = ApproveRequestArgs(
        request_id=request_id,
        comment=None if body is None else body.comment,
        idempotency_key=derive_decision_key(
            approver_user_id=uuid.UUID(claims.sub), request_id=request_id, verb=APPROVE_VERB
        ),
    )
    tool_body = await proxy_tool(request, "approve_request", arguments)
    status = str(tool_body.get("status", ""))
    if status in DECIDED_RESULTS:
        with contextlib.suppress(ResumeRefusedError):
            await resume_after_decision(request, request_id, status)
    return tool_body


@router.post("/approvals/{request_id}/reject")
async def reject(
    request_id: uuid.UUID,
    body: RejectBody,
    request: Request,
) -> Mapping[str, Any]:
    """Refuse one request, with the reason the audit record keeps.

    The key is derived over `REJECT_VERB`, so an Approve and a Reject by one
    approver on one request are two different keys: reusing the approve key
    would replay an approval's stored response for a rejection, which is the
    worst failure this derivation exists to prevent.

    The approver is read off the verified claims on `request.state`, the same
    way `approve` above reads it.

    After the tool returns, the paused chat turn is resumed if the decision is
    final. `ResumeRefusedError` is swallowed for the same reason as in
    `approve` above.
    """
    claims = request_claims(request)

    arguments = RejectRequestArgs(
        request_id=request_id,
        reason=body.reason,
        idempotency_key=derive_decision_key(
            approver_user_id=uuid.UUID(claims.sub), request_id=request_id, verb=REJECT_VERB
        ),
    )
    tool_body = await proxy_tool(request, "reject_request", arguments)
    status = str(tool_body.get("status", ""))
    if status in DECIDED_RESULTS:
        with contextlib.suppress(ResumeRefusedError):
            await resume_after_decision(request, request_id, status)
    return tool_body
