"""The three approval tools: the queue and the two decisions.

`list_pending_approvals` answers "what is waiting for ME" (decision G):
pending requests in the caller's scope that carry an open seat this caller
may actually fill. It re-reads `rbac.authorize` — not as a second permission
check, the pipeline already made that decision, but for
`Authorization.scopes`, the un-collapsed set. A caller who is both a manager
and a system owner has two incomparable scopes and `Authorization.scope`
keeps only the wider one, which would silently drop every request on the
systems they own (the narrowing an earlier change recorded and an earlier change re-owed).
Pinned by
`test_mcp_approvals.py::test_a_manager_and_system_owner_caller_sees_the_union_of_both_queues`.

`approve_request` and `reject_request` record the caller against exactly ONE
open seat of the chain, then act on what that leaves:

    still open seats  -> the row stays `pending`, nothing is granted
    every seat approved -> `approved`, and `execute_grant` runs in the SAME
                           transaction, so the request and the grant commit
                           or roll back together
    rejected            -> `rejected`; one rejection ends the chain and a
                           later approve on that row is refused typed

Both take the row with `SELECT … FOR UPDATE` first (decision F). Two
approvers deciding the same last seat send two DIFFERENT idempotency keys, so
the guard cannot serialise them; the row lock is the only thing that can, and
`test_mcp_approvals.py::test_two_approvers_racing_the_last_slot_produce_exactly_one_grant`
is what tells us it does.

Execution failing on the last approval keeps the row and marks it `failed`
(decision E): the approval really happened and the audit trail must keep it,
so the call SUCCEEDS carrying `failure_code`. The consequence is that the
idempotency row stores a `failed` outcome and every retry of that key
replays it rather than re-attempting the decision — deliberate, since a
retry must not re-run a decision, and pinned by
`test_a_replay_of_a_failed_approval_returns_the_stored_response`.

Nobody approves their own request, one person fills at most one seat of a
chain, and a caller's the design scope has to reach the request as well as the
seat naming them. All of those rules live in `helpdesk.access.approvals`, so
the queue and the two deciding tools cannot answer them differently.

The two decision events (`request.approved`, `request.rejected`) are appended
by these handlers rather than declared as the registry `event_type`: the
pipeline's own event carries a fixed payload and the TOOL as its subject,
which would record that somebody called a tool and not which request they
decided.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from helpdesk.access import (
    NotAnApproverError,
    RequestNotPendingError,
    SimulatedDirectory,
    SlotDecision,
    SlotState,
    chain_is_complete,
    decide_slot,
    dump_slots,
    execute_grant,
    first_fillable_index,
    list_pending_for_approver,
    lock_request,
    parse_slots,
    request_in_scope,
    set_request_status,
)
from helpdesk.access.execute import (
    PolicyBlockedAtExecutionError,
    SodConflictAtExecutionError,
)
from helpdesk.audit import AuditEventInput, append_event
from helpdesk.auth import rbac
from helpdesk.core.repository import Repository
from helpdesk.errors import ErrorCode, NotFoundError
from helpdesk.models import AccessRequest, Grant, RequestStatus, Role, System
from helpdesk.policy.types import ApproverSlot
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_server.identity import Identity
from mcp_server.tools.base import MutatingToolInput

REQUEST_APPROVED_EVENT_TYPE = "request.approved"
REQUEST_REJECTED_EVENT_TYPE = "request.rejected"
"""the design's two decision events, appended by these handlers (decision I).

Appended here rather than declared as the registry `event_type` because the
pipeline's own event carries a fixed payload (`{"tool", "scope"}`) and a
subject of the TOOL name: as the declared event it would record that somebody
called `approve_request`, and not which request they approved or which seat
they filled. The domain event carries all of that, the same way an earlier change's
`request.queued` does; the pipeline's generic `tool.succeeded` lands after it.

The decision is appended BEFORE execution, so the chain reads in the order the
facts happened — `request.approved`, then `grant.executed` if the chain
completed and the fresh the design check allowed it. An approval that completed
a chain and then FAILED execution (decision E) is therefore distinguishable
from one that granted: same `request.approved`, no `grant.executed` after it.
Both orders are pinned exactly, as whole event lists, by
`test_mcp_approvals.py::test_approving_the_last_open_slot_grants_once_with_the_fresh_expiry`
and `::test_a_conflict_planted_while_the_request_waits_fails_the_row_without_a_grant`."""

_ACTOR_KIND = "user"
_SUBJECT_TYPE = "access_request"

LIMIT_MIN = 1
LIMIT_DEFAULT = 50
LIMIT_MAX = 200
"""Bounds on the queue size (decision H).

`list_pending_approvals` returns rows over the wire and nothing downstream
bounds them, so an absent cap is an unbounded response. Pinned exactly, and
the default read off the input model, by
`test_mcp_approvals.py::test_the_limit_boundary_is_inclusive_at_1_and_200`
and `::test_the_default_limit_is_exactly_fifty`."""

COMMENT_MAX_LENGTH = 2000
"""Cap on `approve_request.comment`.

The comment lands inside the unbounded `access_requests.approvals` JSONB, so
this Pydantic bound is the only backstop there is. Pinned inclusively by
`test_mcp_approvals.py::test_the_comment_boundary_is_inclusive_at_2000`."""

REASON_MAX_LENGTH = 2000
"""Cap on `reject_request.reason`, in the same unbounded JSONB column.

The lower bound is 1, not 0: a rejection with an empty reason is a refusal
nobody can audit. Both ends pinned by
`test_mcp_approvals.py::test_the_reason_boundary_is_inclusive_at_1_and_2000`."""

_IGNORE_EXTRAS = ConfigDict(extra="ignore")
"""The read tools' contract: identity arrives on the token, so a spare
argument is data to discard rather than a reason to fail the call. The two
mutating inputs below FORBID extras instead, through `MutatingToolInput`."""


class ListPendingApprovalsInput(BaseModel):
    model_config = _IGNORE_EXTRAS

    limit: int = Field(default=LIMIT_DEFAULT, ge=LIMIT_MIN, le=LIMIT_MAX)


class PendingApproval(BaseModel):
    """One request waiting on this caller, with the seat that is theirs."""

    request_id: uuid.UUID
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
    """Which seat of the chain this caller may fill. Without it an approver
    is shown a row and cannot tell what they are being asked to decide."""

    approvals: list[SlotState]
    """The whole chain, decided seats included, so the caller can see who
    else has to act."""


class ListPendingApprovalsOutput(BaseModel):
    approvals: list[PendingApproval]


class ApproveRequestInput(MutatingToolInput):
    """the design's signature. `extra="forbid"` comes from `MutatingToolInput`."""

    request_id: uuid.UUID
    comment: str | None = Field(default=None, max_length=COMMENT_MAX_LENGTH)


class ApproveRequestOutput(BaseModel):
    request_id: uuid.UUID
    status: RequestStatus
    slot: ApproverSlot
    """The seat this call filled."""

    chain_complete: bool
    grant_id: uuid.UUID | None
    expires_at: datetime | None
    failure_code: ErrorCode | None
    """Set only on decision E's path: the chain completed, execution was
    refused by the fresh the design check, the approval was kept and the row
    is `failed`. The call itself succeeds, so this is how the caller is told
    `SOD_CONFLICT`."""


class RejectRequestInput(MutatingToolInput):
    request_id: uuid.UUID
    reason: str = Field(min_length=1, max_length=REASON_MAX_LENGTH)


class RejectRequestOutput(BaseModel):
    request_id: uuid.UUID
    status: RequestStatus
    slot: ApproverSlot


async def list_pending_approvals(
    session: AsyncSession, identity: Identity, args: ListPendingApprovalsInput
) -> ListPendingApprovalsOutput:
    """The caller's actionable approval queue, oldest first.

    The `authorize` call here is not a second permission gate — the pipeline
    already ran the matrix and a caller with no cell never reaches this
    function. It is how the handler gets `scopes`, the set the pipeline's own
    check discards, which is the only enforcement narrow enough to be right
    for a caller holding two incomparable scopes.

    The caller's department is read from the directory inside
    `list_pending_for_approver`, never from the token's `department_id`
    claim; the user id itself still comes only from the token.
    """
    authorization = rbac.authorize(identity.claims, rbac.Tool.LIST_PENDING_APPROVALS)
    rows = await list_pending_for_approver(
        session,
        SimulatedDirectory(session),
        approver_user_id=identity.user_id,
        scopes=authorization.scopes,
        principals=rbac.principals_of(identity.claims),
        limit=args.limit,
    )
    return ListPendingApprovalsOutput(approvals=[_pending(request, slot) for request, slot in rows])


def _pending(request: AccessRequest, slot: SlotState) -> PendingApproval:
    return PendingApproval(
        request_id=request.id,
        requester_user_id=request.requester_user_id,
        target_user_id=request.target_user_id,
        role_id=request.role_id,
        justification=request.justification,
        requested_expiry_days=request.requested_expiry_days,
        risk_class=request.risk_class,
        decision=request.decision,
        status=request.status,
        created_at=request.created_at,
        my_slot=slot.slot,
        approvals=list(parse_slots(request.approvals)),
    )


async def _append(
    session: AsyncSession,
    identity: Identity,
    event_type: str,
    request: AccessRequest,
    extra: dict[str, Any],
    now: datetime,
) -> None:
    """One decision event, subject the REQUEST, in the tool's own transaction.

    Subject and payload are what the pipeline's generic event cannot carry:
    without them the audit log records that somebody called a tool, not which
    request they decided. Pinned by
    `test_mcp_approvals.py::test_the_decision_event_names_the_request_and_the_seat`.
    """
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


async def _in_scope(
    session: AsyncSession, identity: Identity, tool: rbac.Tool, request: AccessRequest
) -> bool:
    """Does the design's scope on this row still reach this request?

    The seat and the scope are two obligations, not one. The seat says the
    chain asked for this person; the scope says the person still has reach.
    A manager named on a `department_manager` seat who transferred yesterday
    passes the seat rule and must fail here, or they decide a request in a
    department the directory says they left — the same hole the queue closes,
    which would otherwise be open to anyone holding a request id.

    The caller's department and the target's are read from the DIRECTORY, not
    from the token's `department_id` claim (the same reasoning as
    `tools/access.py::_authorize_target`); only the user id comes from the
    token. Pinned by
    `test_mcp_approvals.py::test_a_transferred_manager_may_not_decide_a_row_outside_their_new_department`.
    """
    directory = SimulatedDirectory(session)
    approver = await directory.lookup_user(identity.user_id)
    target = await directory.lookup_user(request.target_user_id)
    role = await Repository(session, Role).get(request.role_id)
    system = None if role is None else await Repository(session, System).get(role.system_id)
    return request_in_scope(
        rbac.authorize(identity.claims, tool).scopes,
        approver_user_id=identity.user_id,
        approver_department_id=None if approver is None else approver.department_id,
        target_department_id=None if target is None else target.department_id,
        system_owner_user_id=None if system is None else system.owner_user_id,
    )


async def _claim_seat(
    session: AsyncSession, identity: Identity, tool: rbac.Tool, request_id: uuid.UUID
) -> tuple[AccessRequest, tuple[SlotState, ...], int]:
    """Lock the row, refuse anything that is not a decision this caller may take.

    Three refusals, each a different the design code and each with a case no
    other guard can decide:

    `NOT_FOUND` covers both "no such request" and "a request in another
    tenant" — the lock runs through the ORM, so the tenant guard makes the
    two indistinguishable and the id cannot be used as an existence oracle
    (`test_another_tenants_request_and_a_missing_one_answer_the_same_way`).

    `POLICY_BLOCKED` is a row that is not `pending` any more (decision J),
    including a chain somebody has already rejected
    (`test_a_row_that_is_not_pending_is_refused_policy_blocked`,
    `test_one_rejection_ends_the_chain_for_a_later_approver`).

    `FORBIDDEN` is a caller who holds no open seat, or whose the design scope no
    longer reaches the request. The seat half covers a seat naming somebody
    else, a group's principal the caller does not hold, a party to the
    request, and a caller who has already decided another seat of this chain;
    the scope half covers a manager who has transferred. One shared refusal
    for all of them, so no branch can drift into telling the caller which
    rule refused them.
    `test_a_caller_matching_no_open_slot_is_refused_and_writes_nothing` uses a
    `platform_admin` on a pending row, so the matrix and the status check both
    admit that call and only the seat rule can refuse it;
    `test_a_party_to_the_request_is_refused_even_holding_the_seats_principal`,
    `test_one_person_cannot_fill_two_seats_of_the_same_chain` and
    `test_a_transferred_manager_may_not_decide_a_row_outside_their_new_department`
    do the same for the other three, each admitted by every guard but its own.
    """
    request = await lock_request(session, request_id)
    if request is None:
        raise NotFoundError("no such access request in this tenant")
    if request.status != RequestStatus.PENDING.value:
        raise RequestNotPendingError(f"the request is {request.status}, not pending")
    if not await _in_scope(session, identity, tool, request):
        raise NotAnApproverError("this request is outside your approval scope")

    slots = parse_slots(request.approvals)
    index = first_fillable_index(
        slots,
        caller_user_id=identity.user_id,
        caller_principals=rbac.principals_of(identity.claims),
        requester_user_id=request.requester_user_id,
        target_user_id=request.target_user_id,
    )
    if index is None:
        raise NotAnApproverError("no open approval slot on this request is yours to fill")
    return request, slots, index


async def approve_request(
    session: AsyncSession, identity: Identity, args: ApproveRequestInput
) -> ApproveRequestOutput:
    """Record one approval, and execute the grant when it was the last one.

    Everything below runs in the one transaction the pipeline opened, so the
    decided chain, the row's status and the grant land together or not at
    all. `now` is read once and passed on, so the decision timestamp, the
    fresh classification, `granted_at` and `expires_at` all answer for one
    instant.
    """
    request, slots, index = await _claim_seat(
        session, identity, rbac.Tool.APPROVE_REQUEST, args.request_id
    )
    now = datetime.now(UTC)
    decided = decide_slot(
        slots,
        index,
        decision=SlotDecision.APPROVE,
        decided_by_user_id=identity.user_id,
        decided_at=now,
        comment=args.comment,
    )
    complete = chain_is_complete(decided)
    await _append(
        session,
        identity,
        REQUEST_APPROVED_EVENT_TYPE,
        request,
        {"slot": slots[index].slot.value, "chain_complete": complete},
        now,
    )
    if not complete:
        await set_request_status(
            session, request.id, status=RequestStatus.PENDING, approvals=dump_slots(decided)
        )
        return _approved(request.id, RequestStatus.PENDING, slots[index], False, None, None)

    try:
        grant = await _execute(session, identity, request, now)
    except (SodConflictAtExecutionError, PolicyBlockedAtExecutionError) as exc:
        # Decision E: the approval happened, so the row keeps it and moves to
        # `failed` rather than rolling back to `pending`. Returning instead of
        # raising is what lets both the decided chain and the stored
        # idempotency response survive.
        await set_request_status(
            session, request.id, status=RequestStatus.FAILED, approvals=dump_slots(decided)
        )
        return _approved(request.id, RequestStatus.FAILED, slots[index], True, None, exc.code)

    await set_request_status(
        session, request.id, status=RequestStatus.APPROVED, approvals=dump_slots(decided)
    )
    return _approved(request.id, RequestStatus.APPROVED, slots[index], True, grant, None)


async def _execute(
    session: AsyncSession, identity: Identity, request: AccessRequest, now: datetime
) -> Grant:
    """An earlier change's `execute_grant`, called with the request's own parameters.

    The requester is the person who FILED the request, not the approver: the
    the design re-check has to classify the same request that was queued, and
    passing the approver here would re-run it as somebody else's request.
    `granted_by_user_id` is the approver, because that is who caused the
    grant.
    """
    return await execute_grant(
        session,
        SimulatedDirectory(session),
        request_id=request.id,
        requester_user_id=request.requester_user_id,
        target_user_id=request.target_user_id,
        role_id=request.role_id,
        requested_expiry_days=request.requested_expiry_days,
        granted_by_user_id=identity.user_id,
        actor_ref=str(identity.user_id),
        session_id=identity.session_id,
        now=now,
    )


def _approved(
    request_id: uuid.UUID,
    status: RequestStatus,
    slot: SlotState,
    chain_complete: bool,
    grant: Grant | None,
    failure_code: ErrorCode | None,
) -> ApproveRequestOutput:
    return ApproveRequestOutput(
        request_id=request_id,
        status=status,
        slot=slot.slot,
        chain_complete=chain_complete,
        grant_id=None if grant is None else grant.id,
        expires_at=None if grant is None else grant.expires_at,
        failure_code=failure_code,
    )


async def reject_request(
    session: AsyncSession, identity: Identity, args: RejectRequestInput
) -> RejectRequestOutput:
    """Refuse the request, keeping who refused it and why.

    One rejection ends the chain: the row goes `rejected` even while other
    seats are still open, and the status check in `_claim_seat` is what
    refuses a later approver typed. The reason is stored on the seat the
    rejecter filled, so the audit record says who refused and why in the same
    place — no migration, and nothing about the rest of the chain is rewritten.
    """
    request, slots, index = await _claim_seat(
        session, identity, rbac.Tool.REJECT_REQUEST, args.request_id
    )
    now = datetime.now(UTC)
    decided = decide_slot(
        slots,
        index,
        decision=SlotDecision.REJECT,
        decided_by_user_id=identity.user_id,
        decided_at=now,
        comment=args.reason,
    )
    await _append(
        session,
        identity,
        REQUEST_REJECTED_EVENT_TYPE,
        request,
        {"slot": slots[index].slot.value},
        now,
    )
    await set_request_status(
        session, request.id, status=RequestStatus.REJECTED, approvals=dump_slots(decided)
    )
    return RejectRequestOutput(
        request_id=request.id, status=RequestStatus.REJECTED, slot=slots[index].slot
    )
