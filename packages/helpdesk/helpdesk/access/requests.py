"""Access-request reads and writes for the tools.

Lives here rather than in `core/repository.py` so the access domain owns
its own queries; the sessions come from `tenant_session`, so the guard
scopes every read to the bound tenant and `Repository.create` stamps the
bound tenant on every write.
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.core.repository import Repository
from helpdesk.errors import NotFoundError
from helpdesk.models import AccessRequest, RequestStatus
from helpdesk.policy.types import Classification, RequiredApprover


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
    statement = select(AccessRequest).where(AccessRequest.requester_user_id == requester_user_id)
    if status is not None:
        statement = statement.where(AccessRequest.status == status.value)
    result = await session.execute(statement.order_by(AccessRequest.created_at, AccessRequest.id))
    return result.scalars().all()


async def list_approved_role_ids_for_target(
    session: AsyncSession, target_user_id: uuid.UUID
) -> frozenset[uuid.UUID]:
    """Role ids approved for this user but not yet executed (the design).

    Exactly the rows whose status is `approved`: an executed request shows
    up as a grant instead, and every other status never conferred anything.
    The status constant is read off `RequestStatus`, so a renamed member
    fails loudly here rather than silently emptying the set.
    """
    result = await session.execute(
        select(AccessRequest).where(
            AccessRequest.target_user_id == target_user_id,
            AccessRequest.status == RequestStatus.APPROVED.value,
        )
    )
    return frozenset(request.role_id for request in result.scalars())
