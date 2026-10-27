"""The access tools: classify, file, list and revoke access requests.

The requester is always the token's user, never an input.
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.mcp_server.core.identity import Identity
from helpdesk.mcp_server.tools.base import MutatingToolInput
from helpdesk.mcp_server.tools.context import ToolContext
from helpdesk.shared.access.approvals import parse_slots, request_in_scope
from helpdesk.shared.access.directory import DirectoryProvider
from helpdesk.shared.access.execute import execute_grant
from helpdesk.shared.access.requests import (
    approval_slots,
    open_request_for_target_role,
    resolved_approver_ids,
)
from helpdesk.shared.access.requests import (
    create_access_request as persist_access_request,
)
from helpdesk.shared.access.revoke import GrantNotActiveError, revoke_role_access
from helpdesk.shared.access.snapshot import build_request_snapshot
from helpdesk.shared.audit import AuditEventInput, append_event
from helpdesk.shared.auth import rbac
from helpdesk.shared.core.errors import NotFoundError
from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.access_requests import list_requests_with_context_for_requester
from helpdesk.shared.crud.grants import is_live_grant
from helpdesk.shared.models import Grant, RequestStatus, Role, System
from helpdesk.shared.models.numbers import NumberKind, format_number
from helpdesk.shared.notifications.request_events import (
    notify_approval_needed,
    notify_granted,
)
from helpdesk.shared.policy.engine import classify
from helpdesk.shared.policy.types import (
    Classification,
    Decision,
    RequiredApprover,
    RiskClass,
    SodConflict,
)

CLASSIFIED_EVENT_TYPE = "request.classified"
"""Classify's own success event."""

EXPIRY_DAYS_MAX = 3650
"""Inclusive cap on `requested_expiry_days`; the column is int4."""

_IGNORE_EXTRAS = ConfigDict(extra="ignore")
"""Read inputs drop extra arguments: identity comes from the token."""


class ClassifyAccessRequestInput(BaseModel):
    model_config = _IGNORE_EXTRAS

    target_user_id: uuid.UUID
    role_id: uuid.UUID
    requested_expiry_days: int | None = Field(default=None, gt=0, le=EXPIRY_DAYS_MAX)


class ClassifyAccessRequestOutput(BaseModel):
    """The engine's `Classification`, field for field."""

    risk_class: RiskClass
    decision: Decision
    reasons: list[str]
    required_approvers: list[RequiredApprover]
    effective_expiry_days: int
    sod_conflicts: list[SodConflict]
    policy_refs: list[str]
    already_requested: bool = False
    """True when a pending or approved request for this target and role is open."""

    request_number: str | None = None
    """That open request's number; `None` when nothing is open."""

    status: RequestStatus | None = None
    """That open request's status; `None` when nothing is open."""


REQUESTS_LIMIT_MIN = 1
REQUESTS_LIMIT_DEFAULT = 50
REQUESTS_LIMIT_MAX = 200
"""Page bounds for `list_my_requests`; the table only grows."""


class ListMyRequestsInput(BaseModel):
    model_config = _IGNORE_EXTRAS

    status: RequestStatus | None = None
    """Must match a status exactly; `"pending "` is refused."""

    limit: int = Field(default=REQUESTS_LIMIT_DEFAULT, ge=REQUESTS_LIMIT_MIN, le=REQUESTS_LIMIT_MAX)
    """Checked by the input model, before any database access."""


class AccessRequestSummary(BaseModel):
    """One request as a reader sees it, ids and names both.

    The browser has no directory lookup, so names are joined in here.
    `grant_id` and `expires_at` come from the grant made, if any.
    """

    request_id: uuid.UUID
    request_number: str
    target_user_id: uuid.UUID
    role_id: uuid.UUID
    justification: str
    requested_expiry_days: int | None
    risk_class: str
    decision: str
    status: str
    created_at: datetime
    role_slug: str
    role_name: str
    system_slug: str
    system_name: str
    target_display_name: str
    decision_reasons: list[Any]
    citations: list[Any]
    approvals: list[Any]
    grant_id: uuid.UUID | None
    expires_at: datetime | None


class ListMyRequestsOutput(BaseModel):
    requests: list[AccessRequestSummary]


async def classify_access_request(
    ctx: ToolContext, args: ClassifyAccessRequestInput
) -> ClassifyAccessRequestOutput:
    """Classify one hypothetical request. Read-only; the engine decides."""
    snapshot = await build_request_snapshot(
        ctx.session,
        ctx.directory,
        requester_user_id=ctx.identity.user_id,
        target_user_id=args.target_user_id,
        role_id=args.role_id,
        requested_expiry_days=args.requested_expiry_days,
        now=ctx.now,
    )
    classification = classify(snapshot)
    open_request = await open_request_for_target_role(
        ctx.session, args.target_user_id, args.role_id
    )
    return ClassifyAccessRequestOutput(
        risk_class=classification.risk_class,
        decision=classification.decision,
        reasons=list(classification.reasons),
        required_approvers=list(classification.required_approvers),
        effective_expiry_days=classification.effective_expiry_days,
        sod_conflicts=list(classification.sod_conflicts),
        policy_refs=list(classification.policy_refs),
        already_requested=open_request is not None,
        request_number=None
        if open_request is None
        else format_number(NumberKind.ACCESS_REQUEST, open_request.number),
        status=None if open_request is None else RequestStatus(open_request.status),
    )


REQUEST_CREATED_EVENT_TYPE = "request.created"
"""The pipeline's event for `create_access_request`, written after the
handler, so it lands last in the chain."""

REQUEST_QUEUED_EVENT_TYPE = "request.queued"
"""Appended by this module on the `approval` branch."""

REQUEST_AUTO_EXECUTED_EVENT_TYPE = "request.auto_executed"
"""Appended on the `auto` branch, before `grant.executed`."""

JUSTIFICATION_MAX_LENGTH = 2000
"""Cap on `justification`; the column has no limit of its own."""

CITATIONS_MAX = 20
"""Cap on `citations[]` entries. The JSONB column has no limit of its own."""

CITATION_FIELD_MAX_LENGTH = 512
"""Cap on every `Citation` string field."""

_ACTOR_KIND = "user"
_SUBJECT_TYPE = "access_request"

# The engine gives the decision; this module picks the row's status.
_STATUS_FOR_DECISION: dict[Decision, RequestStatus] = {
    Decision.AUTO: RequestStatus.EXECUTED,
    Decision.APPROVAL: RequestStatus.PENDING,
    Decision.BLOCKED: RequestStatus.REJECTED,
}


class Citation(BaseModel):
    """One policy citation. Extra keys are refused."""

    model_config = ConfigDict(extra="forbid")

    document_slug: str = Field(max_length=CITATION_FIELD_MAX_LENGTH)
    title: str = Field(max_length=CITATION_FIELD_MAX_LENGTH)
    version: str = Field(max_length=CITATION_FIELD_MAX_LENGTH)
    heading_path: str = Field(max_length=CITATION_FIELD_MAX_LENGTH)
    chunk_id: str = Field(max_length=CITATION_FIELD_MAX_LENGTH)


class CreateAccessRequestInput(MutatingToolInput):
    """Input for `create_access_request`."""

    target_user_id: uuid.UUID
    role_id: uuid.UUID
    justification: str = Field(min_length=1, max_length=JUSTIFICATION_MAX_LENGTH)
    requested_expiry_days: int | None = Field(default=None, gt=0, le=EXPIRY_DAYS_MAX)
    citations: list[Citation] = Field(default_factory=list, max_length=CITATIONS_MAX)


class CreateAccessRequestOutput(BaseModel):
    """The filed request: the engine's verdict plus what the branch did with it."""

    request_id: uuid.UUID
    request_number: str
    """What the user is told, like `REQ-0107`; the id stays for code."""

    status: RequestStatus
    risk_class: RiskClass
    decision: Decision
    reasons: list[str]
    required_approvers: list[RequiredApprover]
    approver_user_ids: list[uuid.UUID]
    """Only the seats the engine could resolve; a group seat names nobody."""
    effective_expiry_days: int
    sod_conflicts: list[SodConflict]
    policy_refs: list[str]
    grant_id: uuid.UUID | None
    expires_at: datetime | None
    already_requested: bool = False
    """True when nothing was filed because this request was already open; the
    id, number and status are then that open request's."""


async def _authorize_target(
    directory: DirectoryProvider, identity: Identity, target_user_id: uuid.UUID
) -> None:
    """May this caller ask on behalf of this target?

    Naming someone else needs `any` scope or the same department. Checked
    here because the target is only known after input checks. The caller's
    department comes from the directory, not the token.
    """
    if target_user_id == identity.user_id:
        return

    scope = rbac.authorize(identity.claims, rbac.Tool.CREATE_ACCESS_REQUEST_ON_BEHALF).scope
    if scope is rbac.Scope.ANY:
        return
    if scope is not rbac.Scope.OWN_DEPARTMENT:
        # Refuse an unknown scope, so a future table edit cannot widen this
        # tool by accident.
        raise _on_behalf_denied()

    caller = await directory.lookup_user(identity.user_id)
    if caller is None or caller.department_id is None:
        raise _on_behalf_denied()
    target = await directory.lookup_user(target_user_id)
    # "No such user" and "another department" get one answer, so this
    # cannot be used to learn which user ids exist.
    if target is None or target.department_id != caller.department_id:
        raise _on_behalf_denied()


def _on_behalf_denied() -> rbac.RbacDenied:
    """The one refusal every on-behalf branch raises, so none can drift."""
    return rbac.RbacDenied(rbac.Tool.CREATE_ACCESS_REQUEST_ON_BEHALF.value)


async def _append(
    session: AsyncSession,
    identity: Identity,
    event_type: str,
    request_id: uuid.UUID,
    payload: dict[str, Any],
    now: datetime,
) -> None:
    """One domain event, in the tool's own transaction."""
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
            subject_id=str(request_id),
            payload=payload,
        ),
    )


async def create_access_request(
    ctx: ToolContext, args: CreateAccessRequestInput
) -> CreateAccessRequestOutput:
    """File one access request, then do what the engine's decision says."""
    directory = ctx.directory
    await _authorize_target(directory, ctx.identity, args.target_user_id)

    now = ctx.now
    snapshot = await build_request_snapshot(
        ctx.session,
        directory,
        requester_user_id=ctx.identity.user_id,
        target_user_id=args.target_user_id,
        role_id=args.role_id,
        requested_expiry_days=args.requested_expiry_days,
        now=now,
    )
    classification = classify(snapshot)

    # Checked in the tool, not only the agent, so no planner can file twice.
    open_request = await open_request_for_target_role(
        ctx.session, args.target_user_id, args.role_id
    )
    if open_request is not None:
        repeat = _output(
            classification,
            open_request.id,
            open_request.number,
            RequestStatus(open_request.status),
            None,
        )
        return repeat.model_copy(update={"already_requested": True})

    status = _STATUS_FOR_DECISION[classification.decision]

    request = await persist_access_request(
        ctx.session,
        requester_user_id=ctx.identity.user_id,
        target_user_id=args.target_user_id,
        role_id=args.role_id,
        justification=args.justification,
        requested_expiry_days=args.requested_expiry_days,
        classification=classification,
        status=status,
        citations=[citation.model_dump(mode="json") for citation in args.citations],
        idempotency_key=args.idempotency_key,
        session_id=ctx.identity.session_id,
    )

    grant = await _run_branch(ctx, args, classification, request.id, now)
    actor_id = ctx.identity.user_id
    if classification.decision is Decision.APPROVAL:
        await notify_approval_needed(
            ctx.session, request, parse_slots(request.approvals), actor_id=actor_id
        )
    elif grant is not None:
        await notify_granted(ctx.session, request, actor_id=actor_id)
    return _output(classification, request.id, request.number, status, grant)


async def _run_branch(
    ctx: ToolContext,
    args: CreateAccessRequestInput,
    classification: Classification,
    request_id: uuid.UUID,
    now: datetime,
) -> Grant | None:
    """The one branch this decision earns, with its domain events."""
    if classification.decision is Decision.AUTO:
        await _append(
            ctx.session,
            ctx.identity,
            REQUEST_AUTO_EXECUTED_EVENT_TYPE,
            request_id,
            {
                "role_id": str(args.role_id),
                "target_user_id": str(args.target_user_id),
                "risk_class": classification.risk_class.value,
            },
            now,
        )
        return await execute_grant(
            ctx.session,
            ctx.directory,
            request_id=request_id,
            requester_user_id=ctx.identity.user_id,
            target_user_id=args.target_user_id,
            role_id=args.role_id,
            requested_expiry_days=args.requested_expiry_days,
            granted_by_user_id=ctx.identity.user_id,
            actor_ref=str(ctx.identity.user_id),
            session_id=ctx.identity.session_id,
            now=now,
        )
    if classification.decision is Decision.APPROVAL:
        await _append(
            ctx.session,
            ctx.identity,
            REQUEST_QUEUED_EVENT_TYPE,
            request_id,
            {
                "role_id": str(args.role_id),
                "target_user_id": str(args.target_user_id),
                "risk_class": classification.risk_class.value,
                "approvals": approval_slots(classification.required_approvers),
            },
            now,
        )
    return None


def _output(
    classification: Classification,
    request_id: uuid.UUID,
    number: int,
    status: RequestStatus,
    grant: Grant | None,
) -> CreateAccessRequestOutput:
    return CreateAccessRequestOutput(
        request_id=request_id,
        request_number=format_number(NumberKind.ACCESS_REQUEST, number),
        status=status,
        risk_class=classification.risk_class,
        decision=classification.decision,
        reasons=list(classification.reasons),
        required_approvers=list(classification.required_approvers),
        approver_user_ids=resolved_approver_ids(classification.required_approvers),
        effective_expiry_days=classification.effective_expiry_days,
        sod_conflicts=list(classification.sod_conflicts),
        policy_refs=list(classification.policy_refs),
        grant_id=None if grant is None else grant.id,
        expires_at=None if grant is None else grant.expires_at,
    )


REASON_MAX_LENGTH = 2000
"""Cap on `revoke_access.reason`. At least one character, since a
removal with no reason cannot be audited."""

SELF_REASON_PREFIX = "Removed by the user: "
"""Starts the stored reason when a person gives up their own access, so the
audit reads it as their own choice. Outside the cap: the cap is on what they
typed."""


class RevokeAccessInput(MutatingToolInput):
    """Input for `revoke_access`."""

    grant_id: uuid.UUID
    reason: str = Field(min_length=1, max_length=REASON_MAX_LENGTH)


class RevokeAccessOutput(BaseModel):
    """Every grant this call ended, not just the one it was handed."""

    revoked_grant_ids: list[uuid.UUID]
    """Every live row for the named grant's (user, role), the named one
included."""

    revoked_count: int


async def _grant_in_scope(ctx: ToolContext, grant: Grant) -> bool:
    """Does the caller's `revoke_access` scope reach this grant?

    The caller's own grant always passes: every role may give up its own
    access. Any other grant takes the same scope check as approvals.
    Departments come from the directory, not the token, so a moved manager
    cannot revoke in their old department.
    """
    if grant.user_id == ctx.identity.user_id:
        return True
    caller = await ctx.directory.lookup_user(ctx.identity.user_id)
    holder = await ctx.directory.lookup_user(grant.user_id)
    role = await Repository(ctx.session, Role).get(grant.role_id)
    system = None if role is None else await Repository(ctx.session, System).get(role.system_id)
    return request_in_scope(
        rbac.authorize(ctx.identity.claims, rbac.Tool.REVOKE_ACCESS).scopes,
        approver_user_id=ctx.identity.user_id,
        approver_department_id=None if caller is None else caller.department_id,
        target_department_id=None if holder is None else holder.department_id,
        system_owner_user_id=None if system is None else system.owner_user_id,
    )


def _stored_reason(ctx: ToolContext, grant: Grant, reason: str) -> str:
    """The reason as typed, marked "Removed by the user" when it is the caller's own grant."""
    if grant.user_id == ctx.identity.user_id:
        return f"{SELF_REASON_PREFIX}{reason}"
    return reason


async def revoke_access(ctx: ToolContext, args: RevokeAccessInput) -> RevokeAccessOutput:
    """Take away the access this grant represents, and every live twin of it.

    The scope check runs before the liveness check, so an out-of-scope
    caller cannot probe a grant's state. A missing grant and another
    tenant's give the same `NOT_FOUND`.
    """
    grant = await Repository(ctx.session, Grant).get(args.grant_id)
    if grant is None:
        raise NotFoundError("no such grant in this tenant")
    if not await _grant_in_scope(ctx, grant):
        raise rbac.RbacDenied(rbac.Tool.REVOKE_ACCESS.value)

    now = ctx.now
    # Check the named grant first, so a caller naming a dead row is told,
    # instead of a sibling row being revoked behind their back.
    if not is_live_grant(grant, now):
        raise GrantNotActiveError(f"grant {grant.id} is not live: it was revoked, or it expired")

    revoked = await revoke_role_access(
        ctx.session,
        ctx.directory,
        user_id=grant.user_id,
        role_id=grant.role_id,
        reason=_stored_reason(ctx, grant, args.reason),
        revoked_by_user_id=ctx.identity.user_id,
        actor_ref=str(ctx.identity.user_id),
        session_id=ctx.identity.session_id,
        now=now,
    )
    return RevokeAccessOutput(
        revoked_grant_ids=[row.id for row in revoked], revoked_count=len(revoked)
    )


async def list_my_requests(ctx: ToolContext, args: ListMyRequestsInput) -> ListMyRequestsOutput:
    """The requests the token user has filed or that were filed for them, optionally by status."""
    rows = await list_requests_with_context_for_requester(
        ctx.session,
        ctx.identity.user_id,
        None if args.status is None else args.status.value,
        args.limit,
    )
    return ListMyRequestsOutput(
        requests=[
            AccessRequestSummary(
                request_id=row.request.id,
                request_number=format_number(NumberKind.ACCESS_REQUEST, row.request.number),
                target_user_id=row.request.target_user_id,
                role_id=row.request.role_id,
                justification=row.request.justification,
                requested_expiry_days=row.request.requested_expiry_days,
                risk_class=row.request.risk_class,
                decision=row.request.decision,
                status=row.request.status,
                created_at=row.request.created_at,
                role_slug=row.role_slug,
                role_name=row.role_name,
                system_slug=row.system_slug,
                system_name=row.system_name,
                target_display_name=row.target_display_name,
                decision_reasons=list(row.request.decision_reasons),
                citations=list(row.request.citations),
                approvals=list(row.request.approvals),
                grant_id=row.grant_id,
                expires_at=row.expires_at,
            )
            for row in rows
        ]
    )
