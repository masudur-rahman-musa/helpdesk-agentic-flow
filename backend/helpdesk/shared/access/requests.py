"""Access-request reads and writes for the tools, always inside the bound tenant."""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.audit import AuditEventInput, append_event
from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.core.errors import NotFoundError
from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.access_requests import (
    fetch_approved_role_ids_for_target,
    fetch_open_request,
    fetch_requests_for_requester,
)
from helpdesk.shared.crud.access_requests import (
    lock_open_requests_for_target as _crud_lock_open_requests,
)
from helpdesk.shared.models import AccessRequest, RequestStatus
from helpdesk.shared.policy.types import Classification, RequiredApprover

REQUEST_REJECTED_EVENT_TYPE = "request.rejected"
"""The audit event for "this request ended without being executed".

Shared by a person rejecting a seat and by an offboard closing a leaver's
requests. One spelling, so an auditor's filter never misses half of them."""

_ACTOR_KIND = "user"
_SUBJECT_TYPE = "access_request"


def resolved_approver_ids(approvers: Sequence[RequiredApprover]) -> list[uuid.UUID]:
    """The approver ids the engine could name; group seats have none."""
    return [approver.user_id for approver in approvers if approver.user_id is not None]


def approval_slots(approvers: Sequence[RequiredApprover]) -> list[dict[str, Any]]:
    """The engine's full seat list for the request row.

    Group seats are kept with a null id: a seat never recorded can never be
    filled.
    """
    return [
        {
            "slot": approver.slot.value,
            "user_id": None if approver.user_id is None else str(approver.user_id),
        }
        for approver in approvers
    ]


async def create_access_request(
    session: AsyncSession,
    *,
    requester_user_id: uuid.UUID,
    target_user_id: uuid.UUID,
    role_id: uuid.UUID,
    justification: str,
    requested_expiry_days: int | None,
    classification: Classification,
    status: RequestStatus,
    citations: list[dict[str, Any]],
    idempotency_key: str | None,
    session_id: uuid.UUID | None = None,
    trace_id: str | None = None,
) -> AccessRequest:
    """Store one classified request, verdict as the user was told it. Flushed, not committed."""
    return await Repository(session, AccessRequest).create(
        requester_user_id=requester_user_id,
        target_user_id=target_user_id,
        role_id=role_id,
        justification=justification,
        requested_expiry_days=requested_expiry_days,
        risk_class=classification.risk_class.value,
        decision=classification.decision.value,
        decision_reasons=list(classification.reasons),
        citations=citations,
        status=status.value,
        approver_user_ids=resolved_approver_ids(classification.required_approvers),
        approvals=approval_slots(classification.required_approvers),
        session_id=session_id,
        trace_id=trace_id,
        idempotency_key=idempotency_key,
    )


async def set_request_status(
    session: AsyncSession,
    request_id: uuid.UUID,
    *,
    status: RequestStatus,
    approvals: list[dict[str, Any]] | None = None,
) -> AccessRequest:
    """Move one request to its next status. Flushed, not committed.

    The caller's transaction holds the status, the seats and the grant
    together, so a request is never `approved` with a grant that rolled
    back. `approvals=None` leaves the seats as they are; an empty list would
    erase who decided what.
    """
    fields: dict[str, Any] = {"status": status.value}
    if approvals is not None:
        fields["approvals"] = approvals
    updated = await Repository(session, AccessRequest).update(request_id, **fields)
    if updated is None:
        raise NotFoundError("no such access request in this tenant")
    return updated


async def list_requests_for_requester(
    session: AsyncSession,
    requester_user_id: uuid.UUID,
    status: RequestStatus | None = None,
) -> Sequence[AccessRequest]:
    """The bound tenant's requests filed by this user, oldest first.

    By requester, not target: a request filed on someone's behalf belongs
    to the manager who filed it.
    """
    return await fetch_requests_for_requester(session, requester_user_id, status)


async def list_approved_role_ids_for_target(
    session: AsyncSession, target_user_id: uuid.UUID
) -> frozenset[uuid.UUID]:
    """Role ids approved for this user but not yet executed."""
    return await fetch_approved_role_ids_for_target(session, target_user_id)


async def open_request_for_target_role(
    session: AsyncSession, target_user_id: uuid.UUID, role_id: uuid.UUID
) -> AccessRequest | None:
    """A pending or approved request for this user and role, if one is open.

    By target, not requester: a manager asking again for someone who already
    has one waiting is the same request.
    """
    return await fetch_open_request(session, target_user_id, role_id)


async def lock_open_requests_for_target(
    session: AsyncSession, target_user_id: uuid.UUID
) -> Sequence[AccessRequest]:
    """The bound tenant's still-open requests targeting this user, locked.

    Open means `pending` or `approved`. An approved row can still become a
    grant, so leaving it would hand back access an offboard just removed.
    Ended requests are left alone, so their history stays true. By target,
    not requester: a request this user filed for someone else is that
    person's access. The lock stops two offboards closing one request
    twice. Ordered so closure events come out in a steady order.
    """
    return await _crud_lock_open_requests(session, target_user_id)


async def append_request_rejected(
    session: AsyncSession,
    request: AccessRequest,
    *,
    reason: str,
    actor_ref: str,
    actor_kind: str = _ACTOR_KIND,
    session_id: uuid.UUID | None,
    now: datetime,
) -> None:
    """One `request.rejected` event, with the request as subject.

    Without it a closed request's own trail would end at its creation.
    `reason` lets an auditor tell an offboard's closures from a person's.
    `actor_kind` is `system` when the nightly leaver sweep did it.
    """
    await append_event(
        session,
        bound_tenant_id(session),
        AuditEventInput(
            occurred_at=now,
            actor_kind=actor_kind,
            actor_ref=actor_ref,
            session_id=session_id,
            trace_id=None,
            event_type=REQUEST_REJECTED_EVENT_TYPE,
            subject_type=_SUBJECT_TYPE,
            subject_id=str(request.id),
            payload={
                "request_id": str(request.id),
                "target_user_id": str(request.target_user_id),
                "role_id": str(request.role_id),
                "reason": reason,
            },
        ),
    )
