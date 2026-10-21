"""The three approval tools: the queue and the two decisions.

Both decisions lock the row first: two approvers on the last seat send
different idempotency keys, so only the lock stops a double grant. When
the last approval's grant is refused, the row is `failed` but the call
succeeds, because the approval really happened.
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.mcp_server.core.identity import Identity
from helpdesk.mcp_server.tools.base import MutatingToolInput
from helpdesk.mcp_server.tools.context import ToolContext
from helpdesk.shared.access.approvals import (
    NotAnApproverError,
    PendingWithContext,
    RequestNotPendingError,
    SlotDecision,
    SlotState,
    chain_is_complete,
    decide_slot,
    dump_slots,
    first_fillable_index,
    list_pending_with_context_for_approver,
    lock_request,
    parse_slots,
    request_in_scope,
)
from helpdesk.shared.access.execute import (
    PolicyBlockedAtExecutionError,
    SodConflictAtExecutionError,
    execute_grant,
)
from helpdesk.shared.access.requests import (
    REQUEST_REJECTED_EVENT_TYPE as REQUEST_REJECTED_EVENT_TYPE,  # re-exported, see below
)
from helpdesk.shared.access.requests import set_request_status
from helpdesk.shared.audit import AuditEventInput, append_event
from helpdesk.shared.auth import rbac
from helpdesk.shared.core.errors import ErrorCode, NotFoundError
from helpdesk.shared.crud import Repository
from helpdesk.shared.models import AccessRequest, Grant, RequestStatus, Role, System
from helpdesk.shared.models.numbers import NumberKind, format_number
from helpdesk.shared.notifications.request_events import (
    notify_failed,
    notify_granted,
    notify_progress,
    notify_rejected,
    plain_role_name_for,
)
from helpdesk.shared.policy.types import ApproverSlot

REQUEST_APPROVED_EVENT_TYPE = "request.approved"
"""Decision event, appended by the handler so it names the request and
the seat, which the pipeline's own event cannot."""

_ACTOR_KIND = "user"
_SUBJECT_TYPE = "access_request"

LIMIT_MIN = 1
LIMIT_DEFAULT = 50
LIMIT_MAX = 200
"""Bounds on the queue size, so a response is never unbounded."""

COMMENT_MAX_LENGTH = 2000
"""Cap on `approve_request.comment`, stored in JSONB."""

REASON_MAX_LENGTH = 2000
"""Cap on `reject_request.reason`, in the same JSONB column. At least one
character, since a refusal with no reason cannot be audited."""

_IGNORE_EXTRAS = ConfigDict(extra="ignore")
"""Read inputs drop extra arguments; mutating inputs refuse them."""


class ListPendingApprovalsInput(BaseModel):
    model_config = _IGNORE_EXTRAS

    limit: int = Field(default=LIMIT_DEFAULT, ge=LIMIT_MIN, le=LIMIT_MAX)


class PendingApproval(BaseModel):
    """One request waiting on this caller, with names joined in, since the
    browser has no directory lookup."""

    request_id: uuid.UUID
    request_number: str
    requester_user_id: uuid.UUID
    target_user_id: uuid.UUID
    role_id: uuid.UUID
    justification: str
    requested_expiry_days: int | None
    risk_class: str
    decision: str
    status: str
    created_at: datetime
    my_slot: ApproverSlot
    """Which seat of the chain this caller may fill."""

    approvals: list[SlotState]
    """The whole chain, so the caller can see who else has to act."""

    role_slug: str
    role_name: str
    system_slug: str
    system_name: str
    target_display_name: str
    requester_display_name: str
    decision_reasons: list[Any]
    citations: list[Any]
    """The stored JSONB columns as they are; the shape is the engine's."""


class ListPendingApprovalsOutput(BaseModel):
    approvals: list[PendingApproval]


class RequestDecisionInput(MutatingToolInput):
    """What approve and reject share: the one request being decided."""

    request_id: uuid.UUID


class ApproveRequestInput(RequestDecisionInput):
    """Input for `approve_request`."""

    comment: str | None = Field(default=None, max_length=COMMENT_MAX_LENGTH)


class ApproveRequestOutput(BaseModel):
    request_id: uuid.UUID
    request_number: str
    status: RequestStatus
    slot: ApproverSlot
    """The seat this call filled."""

    chain_complete: bool
    grant_id: uuid.UUID | None
    expires_at: datetime | None
    failure_code: ErrorCode | None
    """Set only when the chain completed but the fresh check refused the
grant. The row is `failed`; the call still succeeds."""

    role_name: str = ""
    """The requested role in plain words ("Production AWS auditor"), so a reply
    never reads the filled seat as the role."""

    waiting_on: list[ApproverSlot] = []
    """The seats still open after this approval, in chain order; empty once complete."""


class RejectRequestInput(RequestDecisionInput):
    reason: str = Field(min_length=1, max_length=REASON_MAX_LENGTH)


class RejectRequestOutput(BaseModel):
    request_id: uuid.UUID
    request_number: str
    status: RequestStatus
    slot: ApproverSlot
    role_name: str = ""
    """The requested role in plain words."""


async def list_pending_approvals(
    ctx: ToolContext, args: ListPendingApprovalsInput
) -> ListPendingApprovalsOutput:
    """The caller's actionable approval queue, oldest first.

    Uses the full `scopes` set: a manager who also owns a system holds two
    scopes, and keeping only the wider one would hide their systems.
    """
    authorization = rbac.authorize(ctx.identity.claims, rbac.Tool.LIST_PENDING_APPROVALS)
    rows = await list_pending_with_context_for_approver(
        ctx.session,
        ctx.directory,
        approver_user_id=ctx.identity.user_id,
        scopes=authorization.scopes,
        principals=rbac.principals_of(ctx.identity.claims),
        limit=args.limit,
    )
    return ListPendingApprovalsOutput(approvals=[_pending(row) for row in rows])


def _pending(row: PendingWithContext) -> PendingApproval:
    """One queue row, its names taken from the join, never from the request."""
    request = row.request
    return PendingApproval(
        request_id=request.id,
        request_number=format_number(NumberKind.ACCESS_REQUEST, request.number),
        requester_user_id=request.requester_user_id,
        target_user_id=request.target_user_id,
        role_id=request.role_id,
        justification=request.justification,
        requested_expiry_days=request.requested_expiry_days,
        risk_class=request.risk_class,
        decision=request.decision,
        status=request.status,
        created_at=request.created_at,
        my_slot=row.slot.slot,
        approvals=list(parse_slots(request.approvals)),
        role_slug=row.role_slug,
        role_name=row.role_name,
        system_slug=row.system_slug,
        system_name=row.system_name,
        target_display_name=row.target_display_name,
        requester_display_name=row.requester_display_name,
        decision_reasons=list(request.decision_reasons),
        citations=list(request.citations),
    )


async def _append(
    session: AsyncSession,
    identity: Identity,
    event_type: str,
    request: AccessRequest,
    extra: dict[str, Any],
    now: datetime,
) -> None:
    """One decision event naming the request and seat."""
    await append_event(
        session,
        identity.tenant_id,
        AuditEventInput(
            occurred_at=now,
            actor_kind=_ACTOR_KIND,
            actor_ref=str(identity.user_id),
            session_id=identity.session_id,
            trace_id=None,
            event_type=event_type,
            subject_type=_SUBJECT_TYPE,
            subject_id=str(request.id),
            payload={
                "request_id": str(request.id),
                "target_user_id": str(request.target_user_id),
                "role_id": str(request.role_id),
                **extra,
            },
        ),
    )


async def _in_scope(ctx: ToolContext, tool: rbac.Tool, request: AccessRequest) -> bool:
    """Does the caller's scope still reach this request? A manager moved to
    another department passes the seat rule and must fail here."""
    approver = await ctx.directory.lookup_user(ctx.identity.user_id)
    target = await ctx.directory.lookup_user(request.target_user_id)
    role = await Repository(ctx.session, Role).get(request.role_id)
    system = None if role is None else await Repository(ctx.session, System).get(role.system_id)
    return request_in_scope(
        rbac.authorize(ctx.identity.claims, tool).scopes,
        approver_user_id=ctx.identity.user_id,
        approver_department_id=None if approver is None else approver.department_id,
        target_department_id=None if target is None else target.department_id,
        system_owner_user_id=None if system is None else system.owner_user_id,
    )


async def _claim_seat(
    ctx: ToolContext, tool: rbac.Tool, request_id: uuid.UUID
) -> tuple[AccessRequest, tuple[SlotState, ...], int]:
    """Lock the row, refuse anything that is not a decision this caller may take.

    `NOT_FOUND`, `POLICY_BLOCKED` when no longer pending, or one shared
    `FORBIDDEN` that does not say which seat rule refused.
    """
    request = await lock_request(ctx.session, request_id)
    if request is None:
        raise NotFoundError("no such access request in this tenant")
    if request.status != RequestStatus.PENDING.value:
        raise RequestNotPendingError(f"the request is {request.status}, not pending")
    if not await _in_scope(ctx, tool, request):
        raise NotAnApproverError("this request is outside your approval scope")

    slots = parse_slots(request.approvals)
    index = first_fillable_index(
        slots,
        caller_user_id=ctx.identity.user_id,
        caller_principals=rbac.principals_of(ctx.identity.claims),
        requester_user_id=request.requester_user_id,
        target_user_id=request.target_user_id,
    )
    if index is None:
        raise NotAnApproverError("no open approval slot on this request is yours to fill")
    return request, slots, index


async def approve_request(ctx: ToolContext, args: ApproveRequestInput) -> ApproveRequestOutput:
    """Record one approval, and execute the grant when it was the last one."""
    request, slots, index = await _claim_seat(ctx, rbac.Tool.APPROVE_REQUEST, args.request_id)
    now = ctx.now
    decided = decide_slot(
        slots,
        index,
        decision=SlotDecision.APPROVE,
        decided_by_user_id=ctx.identity.user_id,
        decided_at=now,
        comment=args.comment,
    )
    complete = chain_is_complete(decided)
    await _append(
        ctx.session,
        ctx.identity,
        REQUEST_APPROVED_EVENT_TYPE,
        request,
        {"slot": slots[index].slot.value, "chain_complete": complete},
        now,
    )
    if not complete:
        await set_request_status(
            ctx.session, request.id, status=RequestStatus.PENDING, approvals=dump_slots(decided)
        )
        await notify_progress(ctx.session, request, decided, actor_id=ctx.identity.user_id)
        return _approved(
            request,
            RequestStatus.PENDING,
            slots[index],
            False,
            None,
            None,
            role_name=await plain_role_name_for(ctx.session, request.role_id),
            waiting_on=[slot.slot for slot in decided if slot.is_open],
        )

    try:
        grant = await _execute(ctx, request)
    except (SodConflictAtExecutionError, PolicyBlockedAtExecutionError) as exc:
        # The approval happened, so keep it and mark the row `failed`.
        # Returning, not raising, keeps the chain and the stored response.
        await set_request_status(
            ctx.session, request.id, status=RequestStatus.FAILED, approvals=dump_slots(decided)
        )
        await notify_failed(ctx.session, request, actor_id=ctx.identity.user_id)
        return _approved(
            request,
            RequestStatus.FAILED,
            slots[index],
            True,
            None,
            exc.code,
            role_name=await plain_role_name_for(ctx.session, request.role_id),
        )

    await set_request_status(
        ctx.session, request.id, status=RequestStatus.APPROVED, approvals=dump_slots(decided)
    )
    await notify_granted(ctx.session, request, actor_id=ctx.identity.user_id)
    return _approved(
        request,
        RequestStatus.APPROVED,
        slots[index],
        True,
        grant,
        None,
        role_name=await plain_role_name_for(ctx.session, request.role_id),
    )


async def _execute(ctx: ToolContext, request: AccessRequest) -> Grant:
    """Run `execute_grant` as the original request, so the fresh check judges
    what was filed. The approver is `granted_by_user_id`."""
    return await execute_grant(
        ctx.session,
        ctx.directory,
        request_id=request.id,
        requester_user_id=request.requester_user_id,
        target_user_id=request.target_user_id,
        role_id=request.role_id,
        requested_expiry_days=request.requested_expiry_days,
        granted_by_user_id=ctx.identity.user_id,
        actor_ref=str(ctx.identity.user_id),
        session_id=ctx.identity.session_id,
        now=ctx.now,
    )


def _approved(
    request: AccessRequest,
    status: RequestStatus,
    slot: SlotState,
    chain_complete: bool,
    grant: Grant | None,
    failure_code: ErrorCode | None,
    *,
    role_name: str,
    waiting_on: list[ApproverSlot] | None = None,
) -> ApproveRequestOutput:
    return ApproveRequestOutput(
        request_id=request.id,
        request_number=format_number(NumberKind.ACCESS_REQUEST, request.number),
        status=status,
        slot=slot.slot,
        chain_complete=chain_complete,
        grant_id=None if grant is None else grant.id,
        expires_at=None if grant is None else grant.expires_at,
        failure_code=failure_code,
        role_name=role_name,
        waiting_on=waiting_on or [],
    )


async def reject_request(ctx: ToolContext, args: RejectRequestInput) -> RejectRequestOutput:
    """Refuse the request; one rejection ends the chain. The reason is
    stored on the rejecter's seat."""
    request, slots, index = await _claim_seat(ctx, rbac.Tool.REJECT_REQUEST, args.request_id)
    now = ctx.now
    decided = decide_slot(
        slots,
        index,
        decision=SlotDecision.REJECT,
        decided_by_user_id=ctx.identity.user_id,
        decided_at=now,
        comment=args.reason,
    )
    await _append(
        ctx.session,
        ctx.identity,
        REQUEST_REJECTED_EVENT_TYPE,
        request,
        {"slot": slots[index].slot.value},
        now,
    )
    await set_request_status(
        ctx.session, request.id, status=RequestStatus.REJECTED, approvals=dump_slots(decided)
    )
    await notify_rejected(ctx.session, request, args.reason, actor_id=ctx.identity.user_id)
    return RejectRequestOutput(
        request_id=request.id,
        request_number=format_number(NumberKind.ACCESS_REQUEST, request.number),
        status=RequestStatus.REJECTED,
        slot=slots[index].slot,
        role_name=await plain_role_name_for(ctx.session, request.role_id),
    )
