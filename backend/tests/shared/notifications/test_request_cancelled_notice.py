"""Who is told, and what they read, when a waiting request is cancelled.

Surface under test (written before it exists):

``helpdesk.shared.notifications.request_events.plan_cancelled(facts, seats,
holders, *, actor_id)`` — a pure planner shaped like ``plan_approval_needed``:
one ``REQUEST_CANCELLED`` row per person on a still-open seat (a group seat
reaches its active holders), and nobody else.

Plain objects only: no database, no clock, no network. The writer
(``notify_cancelled``) is covered through the tool in
``tests/mcp_server/test_mcp_cancel_request.py``.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from helpdesk.shared.access.approvals import SlotDecision, SlotState
from helpdesk.shared.models.notification import NotificationKind
from helpdesk.shared.notifications.request_events import (
    Planned,
    RequestFacts,
    plan_cancelled,
)
from helpdesk.shared.policy.types import ApproverSlot

ACTIVE = "active"
LEAVER = "leaver"


@dataclass(frozen=True)
class FakeHolder:
    """Any object with `.id` and `.status` is a holder."""

    id: uuid.UUID
    status: str


REQUESTER = uuid.uuid4()
TARGET = uuid.uuid4()
MANAGER = uuid.uuid4()
DECIDED_OWNER = uuid.uuid4()
ADMIN_ONE = uuid.uuid4()
ADMIN_TWO = uuid.uuid4()
LEAVER_ADMIN = uuid.uuid4()
ACTOR = uuid.uuid4()

FACTS = RequestFacts(
    request_id=uuid.uuid4(),
    number="REQ-0107",
    role_name="CRM viewer",
    requester_id=REQUESTER,
    target_id=TARGET,
)

HOLDERS: Mapping[ApproverSlot, Sequence[FakeHolder]] = {
    ApproverSlot.IT_ADMIN: [
        FakeHolder(ADMIN_ONE, ACTIVE),
        FakeHolder(ADMIN_TWO, ACTIVE),
        FakeHolder(LEAVER_ADMIN, LEAVER),
        FakeHolder(REQUESTER, ACTIVE),
        FakeHolder(TARGET, ACTIVE),
        FakeHolder(ACTOR, ACTIVE),
    ],
}


def _open(slot: ApproverSlot, user_id: uuid.UUID | None = None) -> SlotState:
    return SlotState(slot=slot, user_id=user_id)


def _decided(slot: ApproverSlot, user_id: uuid.UUID) -> SlotState:
    return SlotState(
        slot=slot, user_id=user_id, decision=SlotDecision.APPROVE, decided_by_user_id=user_id
    )


def _users(planned: list[Planned]) -> list[uuid.UUID]:
    return [item.user_id for item in planned]


def test_plan_cancelled_reaches_open_seats_and_active_group_holders_only() -> None:
    """Catches a planner that also tells the person on an already-decided seat,
    one that tells a leaver in the group, one that tells the requester, the
    target or the person cancelling, and one that returns nothing.

    The group holders include the requester, the target and the actor, and the
    decided seat names a fifth person, so only each exclusion rule can drop
    them; the named manager and two admins must still be told.
    """
    seats = [
        _open(ApproverSlot.DEPARTMENT_MANAGER, MANAGER),
        _decided(ApproverSlot.SYSTEM_OWNER, DECIDED_OWNER),
        _open(ApproverSlot.IT_ADMIN),
    ]

    planned = plan_cancelled(FACTS, seats, HOLDERS, actor_id=ACTOR)

    assert sorted(_users(planned)) == sorted([MANAGER, ADMIN_ONE, ADMIN_TWO])
    assert {item.kind for item in planned} == {NotificationKind.REQUEST_CANCELLED}


def test_plan_cancelled_tells_nobody_when_every_seat_is_decided() -> None:
    """Catches a planner that walks every seat instead of the open ones: with
    all seats decided there is nobody left to tell, so any row is a notice to
    someone who has nothing to decide."""
    seats = [_decided(ApproverSlot.SYSTEM_OWNER, DECIDED_OWNER)]

    assert plan_cancelled(FACTS, seats, HOLDERS, actor_id=ACTOR) == []


def test_plan_cancelled_tells_a_person_on_two_open_seats_once() -> None:
    """Catches a planner that writes two rows to one person who sits on a named
    seat and on the group seat."""
    seats = [
        _open(ApproverSlot.DEPARTMENT_MANAGER, ADMIN_ONE),
        _open(ApproverSlot.IT_ADMIN),
    ]

    planned = plan_cancelled(FACTS, seats, HOLDERS, actor_id=ACTOR)

    assert sorted(_users(planned)) == sorted([ADMIN_ONE, ADMIN_TWO])


def test_plan_cancelled_words_the_notice() -> None:
    """Catches wrong wording: a title that is not the shared `REQ-0107: CRM
    viewer` form, or a body that says rejected, or one that leaves out that
    nothing is left to decide."""
    planned = plan_cancelled(
        FACTS, [_open(ApproverSlot.DEPARTMENT_MANAGER, MANAGER)], HOLDERS, actor_id=ACTOR
    )

    assert len(planned) == 1
    assert planned[0].title == "REQ-0107: CRM viewer"
    assert (
        planned[0].body == "REQ-0107 for CRM viewer was cancelled. There is nothing left to decide."
    )
