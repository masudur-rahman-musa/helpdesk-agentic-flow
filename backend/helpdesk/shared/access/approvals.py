"""The approval slot state machine and the scoped approver queue.

the design gives a request a chain of approver SLOTS, not a list of people.
An earlier change stores that chain in `access_requests.approvals` as
`{"slot", "user_id"}` objects, with `user_id` null for the two seats that
name a group rather than a person. This module is the other half: who may
fill a seat, what a decided seat looks like, when a chain is finished, and
which pending requests a caller can actually act on.

The per-slot decision extends those same JSONB objects in place (an earlier change
decision D), so there is no migration and no second table: a decided seat
gains `decision`, `decided_by_user_id`, `decided_at` and `comment`.
`approver_user_ids` keeps an earlier change's meaning — the ids the ENGINE could
resolve — and is never rewritten when a group seat is filled.

Nobody approves their own request, and that rule is enforced HERE rather
than in the engine. `policy/decision.py::_ROLE_WIDE_SLOTS` carries the
standing-rule-5 marker saying why: the engine is pure, `it_admin` and `hr`
name a group, and a snapshot cannot say whether the requester is in it. So
`may_fill` refuses the requester and the target on EVERY seat, group seats
included, pinned by
`test_access_approvals.py::test_a_party_to_the_request_fills_no_seat_of_any_kind`
(every slot kind crossed with both parties) and its non-party twin
`test_a_caller_who_is_neither_party_still_fills_the_seat`, which is what
stops an over-broad exclusion from refusing everybody.

A group seat is filled by a holder of that seat's OWN principal: an
`it_admin` may not fill an `hr` seat, and a `platform_admin` fills neither.
the design's `any` scope says which requests a caller may reach, not which
seats they may occupy. Pinned by
`test_a_group_seat_is_fillable_only_by_a_holder_of_its_own_principal` and
`test_a_platform_admin_holding_neither_group_principal_fills_neither_seat`.

The queue consumes `Authorization.scopes`, not `Authorization.scope`: a
caller who is both a manager and a system owner holds two incomparable
scopes and the total-order collapse would drop their owned systems (the
narrowing an earlier change recorded, re-owed by an earlier change). Pinned by
`test_two_scopes_return_the_union_and_one_scope_returns_only_its_half`.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import ColumnElement, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from helpdesk.shared.access.directory import DirectoryProvider
from helpdesk.shared.auth.rbac import Principal, Scope
from helpdesk.shared.errors import ErrorCode, ToolError
from helpdesk.shared.models import AccessRequest, RequestStatus, Role, System, User
from helpdesk.shared.policy.types import ApproverSlot


class SlotDecision(StrEnum):
    """What one approver did with one seat. Stored in the `approvals` JSONB."""

    APPROVE = "approve"
    REJECT = "reject"


GROUP_SLOTS: frozenset[ApproverSlot] = frozenset({ApproverSlot.IT_ADMIN, ApproverSlot.HR})
"""The two the design seats that name a group instead of a person.

Exactly `policy/decision.py::_ROLE_WIDE_SLOTS`, and pinned as an exact set
by `test_access_approvals.py::test_the_group_slots_are_exactly_it_admin_and_hr`:
`department_manager` sliding in would let anyone holding some principal fill
a seat that names one person."""

_PRINCIPAL_FOR_GROUP_SLOT: dict[ApproverSlot, Principal] = {
    ApproverSlot.IT_ADMIN: Principal.IT_ADMIN,
    ApproverSlot.HR: Principal.HR,
}


class RequestNotPendingError(ToolError):
    """The request is not waiting for a decision any more (decision J).

    `POLICY_BLOCKED`, never `FORBIDDEN`: the caller may well have been
    entitled to decide the row a second earlier, so answering with a
    permission code would tell an approver who simply arrived second that
    they lack permission, and the audit log would file a race as a denial.
    Pinned by
    `test_access_approvals.py::test_the_approval_errors_carry_their_spec_error_codes`.
    """

    code = ErrorCode.POLICY_BLOCKED


class NotAnApproverError(ToolError):
    """No open seat on this chain is one this caller may fill.

    One answer for "the seat names someone else", "you do not hold the
    group's principal" and "you are a party to this request": telling them
    apart would say who the named approver is to a caller who may not know.
    """

    code = ErrorCode.FORBIDDEN


class SlotState(BaseModel):
    """One seat of the chain, open or decided.

    Frozen, so `decide_slot` can only answer with a new chain rather than
    mutating one a caller still holds. `extra="forbid"` because this is read
    back out of a JSONB column the system itself wrote: a key nobody parses
    is a decision silently dropped on the next write-back.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    slot: ApproverSlot
    user_id: uuid.UUID | None = None
    """Whom the ENGINE resolved for this seat; always None for a group seat.
    Never rewritten when somebody fills the seat — `decided_by_user_id`
    records who acted."""

    decision: SlotDecision | None = None
    decided_by_user_id: uuid.UUID | None = None
    decided_at: datetime | None = None
    comment: str | None = None

    @property
    def is_open(self) -> bool:
        return self.decision is None


def parse_slots(raw: Sequence[Any]) -> tuple[SlotState, ...]:
    """The stored chain as slot states, in chain order.

    An earlier change's rows carry `{"slot", "user_id"}` and nothing else, so every
    decision field defaults to None and an existing pending request reads as
    a chain of open seats.
    """
    return tuple(SlotState.model_validate(entry) for entry in raw)


def dump_slots(slots: Sequence[SlotState]) -> list[dict[str, Any]]:
    """The chain as JSON-safe objects for the JSONB column.

    `mode="json"` so uuids and datetimes land as strings: a raw `uuid.UUID`
    would not survive the write, and a datetime that did would come back as
    something `parse_slots` cannot read.
    """
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

    Three rules, in order: a decided seat is closed; a party to the request
    fills nothing at all; and then the seat's own kind decides — a group seat
    wants that group's principal, a named seat wants exactly the person it
    names. A named seat with no resolved person is fillable by nobody, which
    is why the comparison is against `slot.user_id` rather than an equality
    that two Nones would satisfy.
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

    None is what the tools turn into `NotAnApproverError`; there is
    deliberately no fallback index, because a fallback would record a
    stranger against somebody else's seat.

    ONE PERSON, ONE SEAT PER CHAIN. This is the rule `may_fill` structurally
    cannot hold, because it sees one seat and this is a property of the whole
    chain: a manager who also holds `it_admin`, or who also owns the system,
    would otherwise fill two of the three seats of a `critical` chain over
    two calls and approve a request nearly on their own. The engine already
    refuses to build such a chain — `policy/decision.py::_is_usable_fallback`
    will not substitute a slot that is already in the chain, "that would
    collapse two approvers into one person and quietly halve the control" —
    and this is the same obligation one layer up, where seats are filled by
    whoever acts (decision C). Pinned by
    `test_access_approvals.py::test_a_caller_who_already_decided_a_seat_gets_no_second_one`
    and, end to end, by
    `test_mcp_approvals.py::test_one_person_cannot_fill_two_seats_of_the_same_chain`.
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
    """True only when every seat is APPROVED.

    Not "no open seats left": a chain whose last decision was a rejection
    also has no open seats, and reading that as complete would execute the
    grant a rejection refused — which is why the rejection test is written
    out here rather than implied by the `all`.
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
    """A new chain with exactly one seat decided; every other seat untouched.

    `decided_by_user_id` is the person who ACTED, never `slot.user_id`: a
    group seat names nobody, so copying the seat's id would record a critical
    request as approved by no one.
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

    `SELECT … FOR UPDATE` through the ORM, so the tenant guard still injects
    its filter: a row in another tenant reads exactly like a row that does
    not exist (both None), and the id is not an existence oracle. Pinned by
    `test_access_approvals.py::test_locking_answers_the_same_way_for_another_tenants_row_and_a_missing_one`.

    The lock is what serialises two approvers deciding the same last seat
    (decision F): the second waits, then re-reads a row that is no longer
    `pending`.
    """
    result = await session.execute(
        select(AccessRequest).where(AccessRequest.id == request_id).with_for_update()
    )
    return result.scalars().one_or_none()


def request_in_scope(
    scopes: frozenset[Scope],
    *,
    approver_user_id: uuid.UUID,
    approver_department_id: uuid.UUID | None,
    target_department_id: uuid.UUID | None,
    system_owner_user_id: uuid.UUID | None,
) -> bool:
    """Does any scope this caller holds reach this ONE request? (the design.)

    The single authority on scope for both the queue and the two deciding
    tools. The seat rules and this are independent obligations: a seat says
    the chain asked for THIS person, scope says the person still has reach
    over the request. A manager named on a seat who has since transferred
    passes the first and fails this one, which is what
    `test_mcp_approvals.py::test_a_transferred_manager_may_not_decide_a_row_outside_their_new_department`
    pins on the decide path and
    `::test_the_managers_department_comes_from_the_directory_not_the_token`
    on the queue.

    A scope this row of the design does not use (`self_only`, `system_only`)
    reaches nothing rather than everything: an unknown scope must narrow,
    never widen. Every department
    comparison takes ids the caller supplies from the DIRECTORY, never from a
    token claim.
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
    """The SQL half of `request_in_scope`, one scope at a time.

    A prefilter and nothing more: the queue re-checks every row it keeps with
    `request_in_scope`, so this narrows the read and the Python predicate is
    what actually decides. If the two ever disagree the Python one wins, which
    is why a scope with no predicate here (`self_only`, `system_only`) is
    still the same "reaches nothing" answer on both sides.
    """
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

    A frozen dataclass around the ORM row rather than a Pydantic model
    repeating its columns, exactly as an earlier change's `RequestWithContext` is: the
    wire shape is `mcp_server.tools.approvals.PendingApproval`, which SPEC
    §8.2 owns, and a second twenty-field model here would be two contracts to
    keep in step. What this carries beside the row is what the JOIN answered
    and the row cannot — the role's and the system's names, and the display
    names of the two PEOPLE the request is about, resolved from the rows their
    ids point at rather than echoed from the request. Pinned by
    `test_mcp_approvals.py::test_the_queue_names_come_from_the_directory_rows_the_ids_point_at`.
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
    """What is waiting for THIS caller: pending, in scope, theirs to fill, named.

    Decision G — a queue of rows only somebody else can move is not a queue.
    Two independent filters, and each has a case the other cannot decide: the scope predicate is the only thing that hides a
    row whose seat names the caller but whose target left their department,
    and the seat predicate is the only thing that hides a row in their
    department whose seat names somebody else.

    The caller's department comes from the DIRECTORY, never from the token's
    `department_id` claim: a manager transferred after their token was issued
    must not keep the queue of the department they left. Pinned end to end by
    `test_mcp_approvals.py::test_the_managers_department_comes_from_the_directory_not_the_token`.

    The SQL scope clause is a prefilter; `request_in_scope` re-checks every
    row that survives it, so the queue and the deciding tools answer the scope
    question through one function rather than two that can drift.

    FOUR joins in one statement: the role, the role's system, the
    target and — through an ALIAS, because both people are rows of `users` —
    the requester. All four are inner joins, since every pending request names
    all four rows, and the tenant guard reaches the alias too
    (`with_loader_criteria(..., include_aliases=True)` in `helpdesk.core.db`).
    One row per pending request, never multiplied by the joins, pinned by
    `test_mcp_approvals.py::test_several_pending_seats_come_back_once_each_despite_the_joins`.

    Ordering is oldest first so the longest-waiting request is worked first,
    and `limit` (decision H) is applied after the seat filter, so the answer
    is `limit` rows the caller can actually act on rather than `limit` rows
    of which some are theirs.

    KNOWN LIMIT, recorded rather than fixed:
    the seat filter runs in Python, so the read is bounded by "pending rows in
    scope", not by `limit`. A tenant with tens of thousands of pending
    requests loads them all on every call. Capping the SQL instead would
    silently hide a request from the only person who can approve it, which is
    worse than a large read; a keyset-paginated queue belongs with the web
    rows that need paging.
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

    requester = aliased(User)
    result = await session.execute(
        select(AccessRequest, Role, System, User, requester)
        .join(User, User.id == AccessRequest.target_user_id)
        .join(Role, Role.id == AccessRequest.role_id)
        .join(System, System.id == Role.system_id)
        .join(requester, requester.id == AccessRequest.requester_user_id)
        .where(AccessRequest.status == RequestStatus.PENDING.value, or_(*predicates))
        .order_by(AccessRequest.created_at, AccessRequest.id)
    )

    queue: list[PendingWithContext] = []
    for request, role, system, target, requester_row in result.all():
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
    """The same queue, narrowed to the request and the seat.

    The narrow twin of `list_pending_with_context_for_approver` and a pure
    projection of it, kept because the filtering rules — scope, seat, order
    and limit — may exist in exactly one place. A caller that needs
    the names calls the widened one; nothing here re-reads or re-decides.
    """
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
