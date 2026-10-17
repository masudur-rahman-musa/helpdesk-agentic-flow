"""Access-request reads and writes for the tools.

Lives here rather than in `core/repository.py` so the access domain owns
its own queries; the sessions come from `tenant_session`, so the guard
scopes every read to the bound tenant and `Repository.create` stamps the
bound tenant on every write.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.core.errors import NotFoundError
from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.access_requests import (
    fetch_approved_role_ids_for_target,
    fetch_requests_for_requester,
    lock_open_requests_for_target as _crud_lock_open_requests,
)
from helpdesk.shared.models import AccessRequest, RequestStatus
from helpdesk.shared.policy.types import Classification, RequiredApprover
from helpdesk.shared.audit import AuditEventInput, append_event

REQUEST_REJECTED_EVENT_TYPE = "request.rejected"
"""the design's name for "this request ended without being executed".

Defined here rather than in either caller because it now has two: an earlier change's
`reject_request` (a person deciding a seat) and an earlier change's `offboard_user`
(every open request of a leaver, closed on their way out). Two spellings of
one event type would let an auditor's filter miss half the closures, so
`mcp_server.tools.approvals` imports this name rather than repeating the
string. Pinned to the exact value by `test_access_offboard.py::
test_the_offboard_constants_and_error_code_are_exactly_the_spec_values`, and
its subject and payload by `::
test_the_pending_and_approved_requests_targeting_the_user_are_rejected`."""

_ACTOR_KIND = "user"
_SUBJECT_TYPE = "access_request"


def resolved_approver_ids(approvers: Sequence[RequiredApprover]) -> list[uuid.UUID]:
    """The approver ids the engine could actually name, in chain order.

    Role-wide slots (`it_admin`, `hr`) stand for a group the pure engine
    cannot resolve and come back with `user_id = None`; they contribute
    nothing here, so `approver_user_ids` can be SHORTER than `approvals`.
    That is the unresolved marker an earlier change left, owed to row 11c, which must
    exclude the requester and the target when it fills a group seat. Both
    columns are written from this one source, so they cannot drift.
    """
    return [approver.user_id for approver in approvers if approver.user_id is not None]


def approval_slots(approvers: Sequence[RequiredApprover]) -> list[dict[str, Any]]:
    """The engine's FULL slot list for `access_requests.approvals`.

    Unresolved slots are kept with a null id rather than dropped: a chain
    that needs an `it_admin` is not the same chain as one that does not, and
    11c cannot fill a seat that was never recorded.
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
    """Persist one classified request. Flushed, not committed.

    The engine's verdict is stored as given — `risk_class`, `decision` and
    the reason codes — so the row records what the user was told, and the
    caller supplies only the lifecycle `status` its branch reached.
    """
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
    """Move one request to its next lifecycle status. Flushed, not committed.

    The caller's transaction owns the write, which is what lets an earlier change's
    approve path put the status, the decided chain and the grant in ONE
    transaction: a self-committing write here would leave a request marked
    `approved` whose grant had rolled back — access granted on paper and
    never in the directory. Pinned by
    `test_access_approvals.py::test_the_status_write_is_flushed_not_committed`.

    `approvals=None` means "leave the chain exactly as it is", not "empty
    it": the reject and fail paths write a status without touching the seats,
    and an empty list there would erase the only record of who decided what
    (pinned by `test_the_status_write_leaves_the_chain_alone_when_none_is_given`).
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
    """The bound tenant's requests filed BY this user, oldest first.

    Filtered by requester, not target: `list_my_requests` answers "what did
    I ask for", and an on-behalf request belongs to the manager
    who filed it, not to the report it names. `status` is already a
    `RequestStatus` member — the boundary validated it — so the query never
    sees free text. The order (`created_at`, then id for a stable tie) is a
    reading convenience, not a promise callers may rely on.
    """
    return await fetch_requests_for_requester(session, requester_user_id, status)


async def list_approved_role_ids_for_target(
    session: AsyncSession, target_user_id: uuid.UUID
) -> frozenset[uuid.UUID]:
    """Role ids approved for this user but not yet executed (the design).

    Exactly the rows whose status is `approved`: an executed request shows
    up as a grant instead, and every other status never conferred anything.
    The status constant is read off `RequestStatus`, so a renamed member
    fails loudly here rather than silently emptying the set.
    """
    return await fetch_approved_role_ids_for_target(session, target_user_id)


async def lock_open_requests_for_target(
    session: AsyncSession, target_user_id: uuid.UUID
) -> Sequence[AccessRequest]:
    """The bound tenant's still-open requests TARGETING this user, locked.

    "Open" is `pending` or `approved` and nothing else.
    `approved` belongs here because an approved-but-not-yet-executed row is
    exactly the one that can still become a grant — the design already counts
    it for SoD — so leaving it open would hand back the access an offboard
    just removed. `executed`, `rejected`, `failed`, `rolled_back` and
    `expired` are ended already, and rewriting one would erase the record of
    how it ended (pinned by `test_access_offboard.py::
    test_an_executed_and_a_rejected_request_targeting_the_user_are_untouched`).

    Filtered by TARGET, not requester: a request this user FILED for somebody
    else is that person's access, and closing it would punish a third party
    for somebody else's departure (pinned by the `filed_for_somebody_else`
    row of `::test_the_pending_and_approved_requests_targeting_the_user_are_rejected`).

    `SELECT … FOR UPDATE` through the ORM, so the tenant guard still injects
    its filter and the rows are held until the caller's transaction ends. The
    lock is what stops two concurrent offboards closing one request twice;
    its serialising half cannot be seen from a single session, so it is pinned
    end to end by `test_mcp_offboard_user.py::
    test_two_callers_racing_the_same_offboard_leave_one_departure`.

    Ordered by `created_at` then id, so two runs over the same rows write
    their closure events in the same order.
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
    """One `request.rejected`, subject the REQUEST.

    A status change that produced no event would be the one lifecycle effect
    invisible in the log: the offboard's summary names the closed ids, but a
    request's own trail would then end at its creation. `reason` is carried
    so an auditor can tell an offboard's rejections from a person's — the
    offboard passes its fixed `OFFBOARD_REVOKE_REASON`. Subject and payload
    pinned by `test_access_offboard.py::
    test_the_pending_and_approved_requests_targeting_the_user_are_rejected`.

    `actor_kind` defaults to `user` and moves only with the offboard that
    called it: an earlier change's leaver sweep closes a departing person's open
    requests as `system`, and a closure filed as a person's decision inside an
    otherwise-automatic departure is the one event that would contradict the
    other two. Named in that plan's Deviations, because its §1 enumerated only
    `user.offboarded` and `grant.revoked`.
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
