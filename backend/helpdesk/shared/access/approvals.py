"""Approver seats: who may fill one, what a decided seat holds, and the queue.

A request carries a chain of seats, stored as JSON objects on the request
row. A decided seat gains `decision`, `decided_by_user_id`, `decided_at` and
`comment` in place. Two seats (`it_admin`, `hr`) name a group, not a person.

Nobody fills a seat on a request they made or are the target of, group
seats included. That check lives here, not in the pure policy engine,
because the engine cannot know who is in a group. A group seat is filled
only by a holder of that group's own principal; a wide `any` scope says
which requests a caller can reach, not which seats they may sit in.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import ColumnElement, true
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.access.directory import DirectoryProvider
from helpdesk.shared.auth.rbac import Principal, Scope
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.crud.approvals import (
    fetch_pending_with_context,
)
from helpdesk.shared.crud.approvals import (
    lock_request as _crud_lock_request,
)
from helpdesk.shared.models import AccessRequest, System, User
from helpdesk.shared.policy.types import ApproverSlot


class SlotDecision(StrEnum):
    """What one approver did with one seat. Stored in the `approvals` JSONB."""

    APPROVE = "approve"
    REJECT = "reject"


GROUP_SLOTS: frozenset[ApproverSlot] = frozenset({ApproverSlot.IT_ADMIN, ApproverSlot.HR})
"""The seats that name a group instead of a person. A named seat must never
join this set, or anyone with some principal could fill it."""

_PRINCIPAL_FOR_GROUP_SLOT: dict[ApproverSlot, Principal] = {
    ApproverSlot.IT_ADMIN: Principal.IT_ADMIN,
    ApproverSlot.HR: Principal.HR,
}


class RequestNotPendingError(ToolError):
    """The request is no longer waiting for a decision.

    `POLICY_BLOCKED`, not `FORBIDDEN`: an approver who simply arrived second
    must not be told they lack permission, and the audit log must not file a
    race as a denial.
    """

    code = ErrorCode.POLICY_BLOCKED


class NotAnApproverError(ToolError):
    """No open seat on this chain is one this caller may fill.

    One answer for every reason, so the caller does not learn who the named
    approver is.
    """

    code = ErrorCode.FORBIDDEN


class SlotState(BaseModel):
    """One seat of the chain, open or decided.

    Frozen, so a decision makes a new chain. `extra="forbid"`: an unread key
    in the stored JSON would be a decision quietly dropped on the next write.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    slot: ApproverSlot
    user_id: uuid.UUID | None = None
    """Whom the policy engine named for this seat; None for a group seat.
    Never rewritten; `decided_by_user_id` records who acted."""

    decision: SlotDecision | None = None
    decided_by_user_id: uuid.UUID | None = None
    decided_at: datetime | None = None
    comment: str | None = None

    @property
    def is_open(self) -> bool:
        return self.decision is None


def parse_slots(raw: Sequence[Any]) -> tuple[SlotState, ...]:
    """The stored chain as seat states. Fields not yet decided default to None."""
    return tuple(SlotState.model_validate(entry) for entry in raw)


def dump_slots(slots: Sequence[SlotState]) -> list[dict[str, Any]]:
    """The chain as JSON-safe objects, so uuids and datetimes store as strings."""
    return [slot.model_dump(mode="json") for slot in slots]


def may_fill(
    slot: SlotState,
    *,
    caller_user_id: uuid.UUID,
    caller_principals: frozenset[Principal],
    requester_user_id: uuid.UUID,
    target_user_id: uuid.UUID,
) -> bool:
    """May this caller record a decision against this seat?

    A decided seat is closed; a party to the request fills nothing; a group
    seat wants that group's principal; a named seat wants exactly its
    person. A named seat with no person is fillable by nobody.
    """
    if not slot.is_open:
        return False
    if caller_user_id in {requester_user_id, target_user_id}:
        return False
    if slot.slot in GROUP_SLOTS:
        return _PRINCIPAL_FOR_GROUP_SLOT[slot.slot] in caller_principals
    return slot.user_id is not None and slot.user_id == caller_user_id


def first_fillable_index(
    slots: Sequence[SlotState],
    *,
    caller_user_id: uuid.UUID,
    caller_principals: frozenset[Principal],
    requester_user_id: uuid.UUID,
    target_user_id: uuid.UUID,
) -> int | None:
    """The first seat this caller may fill, or None.

    One person, one seat per chain. `may_fill` sees only one seat, so it
    cannot stop a manager who also holds `it_admin` from filling two seats
    of a critical chain over two calls and nearly approving alone.
    """
    if any(slot.decided_by_user_id == caller_user_id for slot in slots):
        return None
    for index, slot in enumerate(slots):
        if may_fill(
            slot,
            caller_user_id=caller_user_id,
            caller_principals=caller_principals,
            requester_user_id=requester_user_id,
            target_user_id=target_user_id,
        ):
            return index
    return None


def is_rejected(slots: Sequence[SlotState]) -> bool:
    """True once any seat has been rejected. One rejection ends the chain."""
    return any(slot.decision is SlotDecision.REJECT for slot in slots)


def chain_is_complete(slots: Sequence[SlotState]) -> bool:
    """True only when every seat is approved.

    Not "no open seats left": a chain ended by a rejection has none either.
    """
    return (
        bool(slots)
        and not is_rejected(slots)
        and all(slot.decision is SlotDecision.APPROVE for slot in slots)
    )


def decide_slot(
    slots: Sequence[SlotState],
    index: int,
    *,
    decision: SlotDecision,
    decided_by_user_id: uuid.UUID,
    decided_at: datetime,
    comment: str | None,
) -> tuple[SlotState, ...]:
    """A new chain with exactly one seat decided.

    `decided_by_user_id` is who acted, never `slot.user_id`: a group seat
    names nobody, so copying it would record the approval as by no one.
    """
    decided = slots[index].model_copy(
        update={
            "decision": decision,
            "decided_by_user_id": decided_by_user_id,
            "decided_at": decided_at,
            "comment": comment,
        }
    )
    return tuple(decided if position == index else slot for position, slot in enumerate(slots))


async def lock_request(session: AsyncSession, request_id: uuid.UUID) -> AccessRequest | None:
    """The bound tenant's request, locked for the rest of this transaction.

    Another tenant's row reads as missing, so the id tells nothing. The lock
    makes a second approver on the same last seat wait, then see a row that
    is no longer `pending`.
    """
    return await _crud_lock_request(session, request_id)


def request_in_scope(
    scopes: frozenset[Scope],
    *,
    approver_user_id: uuid.UUID,
    approver_department_id: uuid.UUID | None,
    target_department_id: uuid.UUID | None,
    system_owner_user_id: uuid.UUID | None,
) -> bool:
    """Does any scope this caller holds reach this one request?

    Used by the queue and both deciding tools. It is separate from the seat
    rules: a manager named on a seat who has since moved department passes
    the seat check and fails this one. Unknown scopes reach nothing.
    Department ids must come from the directory, never the token.
    """
    return any(
        _one_scope_reaches(
            scope,
            approver_user_id=approver_user_id,
            approver_department_id=approver_department_id,
            target_department_id=target_department_id,
            system_owner_user_id=system_owner_user_id,
        )
        for scope in scopes
    )


def _one_scope_reaches(
    scope: Scope,
    *,
    approver_user_id: uuid.UUID,
    approver_department_id: uuid.UUID | None,
    target_department_id: uuid.UUID | None,
    system_owner_user_id: uuid.UUID | None,
) -> bool:
    if scope is Scope.ANY:
        return True
    if scope is Scope.OWN_DEPARTMENT:
        return approver_department_id is not None and target_department_id == approver_department_id
    if scope is Scope.OWN_SYSTEMS:
        return system_owner_user_id is not None and system_owner_user_id == approver_user_id
    return False


def _scope_predicate(
    scope: Scope, *, approver_user_id: uuid.UUID, approver_department_id: uuid.UUID | None
) -> ColumnElement[bool] | None:
    """The SQL prefilter for one scope. `request_in_scope` re-checks every row and decides."""
    if scope is Scope.ANY:
        return true()
    if scope is Scope.OWN_DEPARTMENT:
        if approver_department_id is None:
            return None
        return User.department_id == approver_department_id
    if scope is Scope.OWN_SYSTEMS:
        return System.owner_user_id == approver_user_id
    return None


@dataclass(frozen=True)
class PendingWithContext:
    """One pending request, the seat this caller may fill, and the names.

    Names of the role, system and both people come from the rows the ids
    point at, not from text on the request.
    """

    request: AccessRequest
    slot: SlotState
    role_slug: str
    role_name: str
    system_slug: str
    system_name: str
    target_display_name: str
    requester_display_name: str


async def list_pending_with_context_for_approver(
    session: AsyncSession,
    directory: DirectoryProvider,
    *,
    approver_user_id: uuid.UUID,
    scopes: frozenset[Scope],
    principals: frozenset[Principal],
    limit: int,
) -> list[PendingWithContext]:
    """What is waiting for this caller: pending, in scope, theirs to fill.

    Scope and seat are both needed: scope hides a row whose target left the
    caller's department, the seat hides a row whose seat names someone else.
    The caller's department comes from the directory, not the token, so a
    moved manager loses the old queue. Oldest first; `limit` applies after
    the seat filter, so every row returned is one the caller can act on.

    Known limit: the seat filter runs in Python, so every pending row in
    scope is loaded. Capping the SQL instead could hide a request from the
    only person who can approve it.
    """
    approver = await directory.lookup_user(approver_user_id)
    approver_department_id = None if approver is None else approver.department_id
    predicates = [
        predicate
        for scope in scopes
        if (
            predicate := _scope_predicate(
                scope,
                approver_user_id=approver_user_id,
                approver_department_id=approver_department_id,
            )
        )
        is not None
    ]
    if not predicates:
        return []

    rows = await fetch_pending_with_context(session, predicates)
    queue: list[PendingWithContext] = []
    for request, role, system, target, requester_row in rows:
        if not request_in_scope(
            scopes,
            approver_user_id=approver_user_id,
            approver_department_id=approver_department_id,
            target_department_id=target.department_id,
            system_owner_user_id=system.owner_user_id,
        ):
            continue
        slots = parse_slots(request.approvals)
        index = first_fillable_index(
            slots,
            caller_user_id=approver_user_id,
            caller_principals=principals,
            requester_user_id=request.requester_user_id,
            target_user_id=request.target_user_id,
        )
        if index is None:
            continue
        queue.append(
            PendingWithContext(
                request=request,
                slot=slots[index],
                role_slug=role.slug,
                role_name=role.name,
                system_slug=system.slug,
                system_name=system.name,
                target_display_name=target.display_name,
                requester_display_name=requester_row.display_name,
            )
        )
        if len(queue) == limit:
            break
    return queue


async def list_pending_for_approver(
    session: AsyncSession,
    directory: DirectoryProvider,
    *,
    approver_user_id: uuid.UUID,
    scopes: frozenset[Scope],
    principals: frozenset[Principal],
    limit: int,
) -> list[tuple[AccessRequest, SlotState]]:
    """The same queue, narrowed to the request and the seat."""
    return [
        (row.request, row.slot)
        for row in await list_pending_with_context_for_approver(
            session,
            directory,
            approver_user_id=approver_user_id,
            scopes=scopes,
            principals=principals,
            limit=limit,
        )
    ]


__all__ = [
    "GROUP_SLOTS",
    "NotAnApproverError",
    "PendingWithContext",
    "RequestNotPendingError",
    "SlotDecision",
    "SlotState",
    "chain_is_complete",
    "decide_slot",
    "dump_slots",
    "first_fillable_index",
    "is_rejected",
    "list_pending_for_approver",
    "list_pending_with_context_for_approver",
    "lock_request",
    "may_fill",
    "parse_slots",
    "request_in_scope",
]
