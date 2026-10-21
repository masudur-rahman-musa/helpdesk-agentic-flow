"""What people are told when a request moves or access is taken away.

The `plan_*` functions are pure: they pick who is told and write the words.
The `notify_*` writers read what the words need, then hand each row to the
one `notify` writer, in the caller's transaction, so the change and its
message commit or undo together.

Two rules hold for every planner: the person acting is never told about
their own action, and one person gets at most one row per change.
"""

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.access.approvals import SlotState
from helpdesk.shared.access.role_names import plain_role_name, role_word
from helpdesk.shared.auth.rbac import Principal
from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.users import fetch_active_holders, find_user_by_id
from helpdesk.shared.models import AccessRequest, Grant, Role, System
from helpdesk.shared.models.notification import NOTIFICATION_BODY_MAX_LENGTH, NotificationKind
from helpdesk.shared.models.numbers import NumberKind, format_number
from helpdesk.shared.notifications.notify import notify
from helpdesk.shared.policy.types import ApproverSlot, TargetStatus

_REQUEST_SUBJECT = "access_request"
_GRANT_SUBJECT = "grant"
_ELLIPSIS = "…"

_SEAT_LABELS: Mapping[ApproverSlot, str] = {
    ApproverSlot.DEPARTMENT_MANAGER: "Department manager",
    ApproverSlot.SYSTEM_OWNER: "System owner",
    ApproverSlot.IT_ADMIN: "IT admin",
    ApproverSlot.HR: "HR",
}

_GROUP_PRINCIPALS: Mapping[ApproverSlot, Principal] = {
    ApproverSlot.IT_ADMIN: Principal.IT_ADMIN,
    ApproverSlot.HR: Principal.HR,
}
"""Seats filled by anyone holding a principal, not by one named person."""


class Holder(Protocol):
    """Anyone who may fill a group seat: a `User` row, or a test fake."""

    @property
    def id(self) -> uuid.UUID: ...

    @property
    def status(self) -> str: ...


@dataclass(frozen=True)
class RequestFacts:
    """What the words about one request need."""

    request_id: uuid.UUID
    number: str
    role_name: str
    requester_id: uuid.UUID
    target_id: uuid.UUID


@dataclass(frozen=True)
class Planned:
    """One row to write."""

    user_id: uuid.UUID
    kind: NotificationKind
    title: str
    body: str


def _fit(body: str) -> str:
    """Cut a body to the column width, ending in "…", so the write never fails
    and undoes the change (`test_a_long_reason_is_cut_to_the_body_cap`)."""
    if len(body) <= NOTIFICATION_BODY_MAX_LENGTH:
        return body
    return body[: NOTIFICATION_BODY_MAX_LENGTH - 1] + _ELLIPSIS


def _title(facts: RequestFacts) -> str:
    return f"{facts.number}: {facts.role_name}"


def _each_once(
    people: Iterable[uuid.UUID],
    *,
    actor_id: uuid.UUID | None,
    kind: NotificationKind,
    title: str,
    body: str,
) -> list[Planned]:
    """One row per person, in first-seen order, the actor left out."""
    seen: set[uuid.UUID] = set()
    planned: list[Planned] = []
    for person in people:
        if person == actor_id or person in seen:
            continue
        seen.add(person)
        planned.append(Planned(user_id=person, kind=kind, title=title, body=_fit(body)))
    return planned


def plan_approval_needed(
    facts: RequestFacts,
    seats: Sequence[SlotState],
    holders: Mapping[ApproverSlot, Sequence[Holder]],
    *,
    actor_id: uuid.UUID | None,
) -> list[Planned]:
    """Every open seat's person; a group seat reaches every active holder.
    The requester and the target never approve, so they are never asked."""
    people: list[uuid.UUID] = []
    for seat in seats:
        if not seat.is_open:
            continue
        if seat.user_id is not None:
            people.append(seat.user_id)
            continue
        people.extend(
            holder.id
            for holder in holders.get(seat.slot, ())
            if holder.status == TargetStatus.ACTIVE.value
        )
    parties = {facts.requester_id, facts.target_id}
    return _each_once(
        (person for person in people if person not in parties),
        actor_id=actor_id,
        kind=NotificationKind.APPROVAL_NEEDED,
        title=_title(facts),
        body=(
            f"{facts.number} asks for {facts.role_name} and waits for your approval. "
            "Open Approvals to decide."
        ),
    )


def plan_progress(
    facts: RequestFacts, seats_after: Sequence[SlotState], *, actor_id: uuid.UUID | None
) -> list[Planned]:
    """The requester, naming the seats still waiting in plain words."""
    waiting = ", ".join(_SEAT_LABELS[seat.slot] for seat in seats_after if seat.is_open)
    return _each_once(
        [facts.requester_id],
        actor_id=actor_id,
        kind=NotificationKind.REQUEST_PROGRESS,
        title=_title(facts),
        body=f"One approval for {facts.number} is in. Still waiting on: {waiting}.",
    )


def plan_granted(facts: RequestFacts, *, actor_id: uuid.UUID | None) -> list[Planned]:
    """The requester and the person the access is for."""
    return _each_once(
        [facts.requester_id, facts.target_id],
        actor_id=actor_id,
        kind=NotificationKind.REQUEST_GRANTED,
        title=_title(facts),
        body=f"{facts.number} is granted. Access to {facts.role_name} is now on.",
    )


def plan_rejected(facts: RequestFacts, reason: str, *, actor_id: uuid.UUID | None) -> list[Planned]:
    """The requester and the person the access is for, with the reason."""
    return _each_once(
        [facts.requester_id, facts.target_id],
        actor_id=actor_id,
        kind=NotificationKind.REQUEST_REJECTED,
        title=_title(facts),
        body=f"{facts.number} for {facts.role_name} was rejected. Reason: {reason}",
    )


def plan_failed(facts: RequestFacts, *, actor_id: uuid.UUID | None) -> list[Planned]:
    """The requester: every approval is in, but the access could not be given."""
    return _each_once(
        [facts.requester_id],
        actor_id=actor_id,
        kind=NotificationKind.REQUEST_FAILED,
        title=_title(facts),
        body=(
            f"{facts.number} for {facts.role_name} was approved, but the access "
            "could not be given. Ask the help desk to look at it."
        ),
    )


def plan_revoked(
    *,
    user_id: uuid.UUID,
    role_name: str,
    request_number: str | None,
    reason: str,
    actor_id: uuid.UUID | None,
) -> list[Planned]:
    """The person whose access ended, unless they ended it themselves."""
    from_request = "" if request_number is None else f" (from {request_number})"
    return _each_once(
        [user_id],
        actor_id=actor_id,
        kind=NotificationKind.ACCESS_REVOKED,
        title=f"Access removed: {role_name}",
        body=f"Your access to {role_name}{from_request} was removed. Reason: {reason}",
    )


async def send(
    session: AsyncSession, planned: Sequence[Planned], *, subject_type: str, subject_id: str
) -> None:
    """Write each row. A person missing from the tenant is skipped, never
    raised, so a message can never undo the change it is about."""
    for item in planned:
        if await find_user_by_id(session, item.user_id) is None:
            continue
        await notify(
            session,
            user_id=item.user_id,
            kind=item.kind,
            title=item.title,
            body=item.body,
            subject_type=subject_type,
            subject_id=subject_id,
        )


async def plain_role_name_for(session: AsyncSession, role_id: uuid.UUID) -> str:
    """The role in plain words ("Production Cluster deployer"), never its stored code."""
    role = await Repository(session, Role).get(role_id)
    if role is None:
        return "a role"
    system = await Repository(session, System).get(role.system_id)
    return role_word(role.slug) if system is None else plain_role_name(system.name, role.slug)


async def _facts(session: AsyncSession, request: AccessRequest) -> RequestFacts:
    return RequestFacts(
        request_id=request.id,
        number=format_number(NumberKind.ACCESS_REQUEST, request.number),
        role_name=await plain_role_name_for(session, request.role_id),
        requester_id=request.requester_user_id,
        target_id=request.target_user_id,
    )


async def _send_about(
    session: AsyncSession, request: AccessRequest, planned: Sequence[Planned]
) -> None:
    await send(session, planned, subject_type=_REQUEST_SUBJECT, subject_id=str(request.id))


async def notify_approval_needed(
    session: AsyncSession,
    request: AccessRequest,
    seats: Sequence[SlotState],
    *,
    actor_id: uuid.UUID | None,
) -> None:
    holders: dict[ApproverSlot, Sequence[Holder]] = {}
    for seat in seats:
        principal = _GROUP_PRINCIPALS.get(seat.slot)
        if seat.is_open and seat.user_id is None and principal is not None:
            holders[seat.slot] = await fetch_active_holders(session, principal.value)
    facts = await _facts(session, request)
    await _send_about(
        session, request, plan_approval_needed(facts, seats, holders, actor_id=actor_id)
    )


async def notify_progress(
    session: AsyncSession,
    request: AccessRequest,
    seats_after: Sequence[SlotState],
    *,
    actor_id: uuid.UUID | None,
) -> None:
    facts = await _facts(session, request)
    await _send_about(session, request, plan_progress(facts, seats_after, actor_id=actor_id))


async def notify_granted(
    session: AsyncSession, request: AccessRequest, *, actor_id: uuid.UUID | None
) -> None:
    facts = await _facts(session, request)
    await _send_about(session, request, plan_granted(facts, actor_id=actor_id))


async def notify_rejected(
    session: AsyncSession, request: AccessRequest, reason: str, *, actor_id: uuid.UUID | None
) -> None:
    facts = await _facts(session, request)
    await _send_about(session, request, plan_rejected(facts, reason, actor_id=actor_id))


async def notify_failed(
    session: AsyncSession, request: AccessRequest, *, actor_id: uuid.UUID | None
) -> None:
    facts = await _facts(session, request)
    await _send_about(session, request, plan_failed(facts, actor_id=actor_id))


async def notify_revoked(
    session: AsyncSession, grant: Grant, reason: str, *, actor_id: uuid.UUID | None
) -> None:
    """Every revoke path calls this through `revoke_grant_row`."""
    request = (
        None
        if grant.request_id is None
        else await Repository(session, AccessRequest).get(grant.request_id)
    )
    planned = plan_revoked(
        user_id=grant.user_id,
        role_name=await plain_role_name_for(session, grant.role_id),
        request_number=None
        if request is None
        else format_number(NumberKind.ACCESS_REQUEST, request.number),
        reason=reason,
        actor_id=actor_id,
    )
    await send(session, planned, subject_type=_GRANT_SUBJECT, subject_id=str(grant.id))
