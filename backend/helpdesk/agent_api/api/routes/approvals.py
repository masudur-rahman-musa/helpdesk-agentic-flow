"""GET /me/approvals and the two decisions an approver can take.

These call the same MCP tools the agent uses, so access checks, the kill
switch, the approval rules and audit apply to a browser decision exactly as to
the model's. The caller's own token is forwarded and the approver comes from
the verified claims. The tool's output is returned unchanged, and the tool
alone checks comment and reason lengths so errors stay typed.
"""

import uuid
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Query, Request

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
from helpdesk.agent_api.services.tool_proxy import proxy_tool

router = APIRouter()

APPROVE_VERB = "approve"
REJECT_VERB = "reject"
"""The path segments, reused as key material so the two cannot drift apart."""


@router.get("/me/approvals")
async def my_approvals(
    request: Request,
    limit: int | None = Query(default=None),
) -> Mapping[str, Any]:
    """Pending requests waiting on this caller, oldest first."""
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
    """Approve one step of one request's approval chain.

    The id is a UUID so a bad path never turns into a key filed against nothing.
    The requester learns the decision from the notifications and from their
    own request list; no chat turn waits on it.
    """
    claims = request_claims(request)

    arguments = ApproveRequestArgs(
        request_id=request_id,
        comment=None if body is None else body.comment,
        idempotency_key=derive_decision_key(
            approver_user_id=uuid.UUID(claims.sub), request_id=request_id, verb=APPROVE_VERB
        ),
    )
    return await proxy_tool(request, "approve_request", arguments)


@router.post("/approvals/{request_id}/reject")
async def reject(
    request_id: uuid.UUID,
    body: RejectBody,
    request: Request,
) -> Mapping[str, Any]:
    """Reject one request, with the reason the audit record keeps.

    The key includes the verb, so a reject never replays a stored approve.
    """
    claims = request_claims(request)

    arguments = RejectRequestArgs(
        request_id=request_id,
        reason=body.reason,
        idempotency_key=derive_decision_key(
            approver_user_id=uuid.UUID(claims.sub), request_id=request_id, verb=REJECT_VERB
        ),
    )
    return await proxy_tool(request, "reject_request", arguments)
