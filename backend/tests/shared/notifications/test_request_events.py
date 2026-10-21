"""Who is told, and what they read, when a request moves.

Surface under test (written before it exists):

``helpdesk.shared.notifications.request_events``
    ``RequestFacts`` and ``Planned`` (frozen dataclasses) and six pure
    planners, each returning ``list[Planned]`` with at most one entry per
    person and the acting person never included: ``plan_approval_needed``,
    ``plan_progress``, ``plan_granted``, ``plan_rejected``, ``plan_failed``,
    ``plan_revoked``.

Plain objects only: no database, no clock, no network. The writers that put
these rows in Postgres are covered through the tools in
``tests/mcp_server/test_mcp_request_notifications.py``.
"""

import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import pytest

from helpdesk.shared.access.approvals import SlotDecision, SlotState
from helpdesk.shared.models.notification import NOTIFICATION_BODY_MAX_LENGTH, NotificationKind
from helpdesk.shared.notifications.request_events import (
    Planned,
    RequestFacts,
    plan_approval_needed,
    plan_failed,
    plan_granted,
    plan_progress,
    plan_rejected,
    plan_revoked,
)
from helpdesk.shared.policy.types import ApproverSlot

ELLIPSIS = "…"
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
OWNER = uuid.uuid4()
ADMIN_ONE = uuid.uuid4()
ADMIN_TWO = uuid.uuid4()
LEAVER_ADMIN = uuid.uuid4()

FACTS = RequestFacts(
    request_id=uuid.uuid4(),
    number="REQ-0022",
    role_name="Data Warehouse engineer",
    requester_id=REQUESTER,
    target_id=TARGET,
)

HOLDERS: Mapping[ApproverSlot, Sequence[FakeHolder]] = {
    ApproverSlot.IT_ADMIN: [
        FakeHolder(ADMIN_ONE, ACTIVE),
        FakeHolder(ADMIN_TWO, ACTIVE),
        FakeHolder(LEAVER_ADMIN, LEAVER),
    ],
    ApproverSlot.HR: [],
}


def _users(planned: list[Planned]) -> list[uuid.UUID]:
    return [item.user_id for item in planned]


def _seat(
    slot: ApproverSlot, user_id: uuid.UUID | None = None, *, decided: bool = False
) -> SlotState:
    if decided:
        return SlotState(
            slot=slot,
            user_id=user_id,
            decision=SlotDecision.APPROVE,
            decided_by_user_id=user_id,
        )
    return SlotState(slot=slot, user_id=user_id)


def test_approvers_are_every_open_seat_and_every_group_holder() -> None:
    """Catches an implementation that tells only the named seats and forgets the
    group seat, one that tells a leaver in the group, or one that also tells
    the person on a seat that is already decided.

    The decided seat names a fifth person who must stay out, so a planner that
    walks every seat instead of the open ones fails here.
    """
    decided_person = uuid.uuid4()
    seats = [
        _seat(ApproverSlot.DEPARTMENT_MANAGER, MANAGER),
        _seat(ApproverSlot.SYSTEM_OWNER, OWNER),
        _seat(ApproverSlot.IT_ADMIN),
        _seat(ApproverSlot.HR, decided_person, decided=True),
    ]

    planned = plan_approval_needed(FACTS, seats, HOLDERS, actor_id=None)

    assert sorted(_users(planned)) == sorted([MANAGER, OWNER, ADMIN_ONE, ADMIN_TWO])
    assert LEAVER_ADMIN not in _users(planned)
    assert decided_person not in _users(planned)
    assert {item.kind for item in planned} == {NotificationKind.APPROVAL_NEEDED}


@pytest.mark.parametrize("party", ["requester", "target"])
@pytest.mark.parametrize("seat_kind", ["group", "named"])
def test_requester_and_target_are_never_approvers(party: str, seat_kind: str) -> None:
    """Catches an implementation that tells the requester (or the target) to
    approve their own request when they hold the group's principal or are
    named on a seat.

    `actor_id` is None, so only the party rule can drop them; the other
    approver on the same chain is still told, so a planner that returns
    nothing at all fails too.
    """
    person = REQUESTER if party == "requester" else TARGET
    other = uuid.uuid4()
    if seat_kind == "group":
        seats = [_seat(ApproverSlot.IT_ADMIN)]
        holders = {ApproverSlot.IT_ADMIN: [FakeHolder(person, ACTIVE), FakeHolder(other, ACTIVE)]}
    else:
        seats = [
            _seat(ApproverSlot.DEPARTMENT_MANAGER, person),
            _seat(ApproverSlot.SYSTEM_OWNER, other),
        ]
        holders = {}

    planned = plan_approval_needed(FACTS, seats, holders, actor_id=None)

    assert _users(planned) == [other]


_PlannerCall = Callable[[uuid.UUID | None], list[Planned]]


def _approval_needed(actor: uuid.UUID | None) -> list[Planned]:
    seats = [_seat(ApproverSlot.SYSTEM_OWNER, OWNER), _seat(ApproverSlot.IT_ADMIN)]
    holders = {ApproverSlot.IT_ADMIN: [FakeHolder(ADMIN_ONE, ACTIVE)]}
    return plan_approval_needed(FACTS, seats, holders, actor_id=actor)


def _progress(actor: uuid.UUID | None) -> list[Planned]:
    return plan_progress(FACTS, [_seat(ApproverSlot.IT_ADMIN)], actor_id=actor)


def _granted(actor: uuid.UUID | None) -> list[Planned]:
    return plan_granted(FACTS, actor_id=actor)


def _rejected(actor: uuid.UUID | None) -> list[Planned]:
    return plan_rejected(FACTS, "No business need", actor_id=actor)


def _failed(actor: uuid.UUID | None) -> list[Planned]:
    return plan_failed(FACTS, actor_id=actor)


def _revoked(actor: uuid.UUID | None) -> list[Planned]:
    return plan_revoked(
        user_id=TARGET,
        role_name=FACTS.role_name,
        request_number=FACTS.number,
        reason="Left the team",
        actor_id=actor,
    )


PLANNERS = [
    pytest.param(
        _approval_needed, NotificationKind.APPROVAL_NEEDED, {OWNER, ADMIN_ONE}, id="approval"
    ),
    pytest.param(_progress, NotificationKind.REQUEST_PROGRESS, {REQUESTER}, id="progress"),
    pytest.param(_granted, NotificationKind.REQUEST_GRANTED, {REQUESTER, TARGET}, id="granted"),
    pytest.param(_rejected, NotificationKind.REQUEST_REJECTED, {REQUESTER, TARGET}, id="rejected"),
    pytest.param(_failed, NotificationKind.REQUEST_FAILED, {REQUESTER}, id="failed"),
    pytest.param(_revoked, NotificationKind.ACCESS_REVOKED, {TARGET}, id="revoked"),
]


@pytest.mark.parametrize(("planner", "kind", "recipients"), PLANNERS)
def test_the_actor_is_never_told_about_their_own_action(
    planner: _PlannerCall, kind: NotificationKind, recipients: set[uuid.UUID]
) -> None:
    """Catches an implementation that tells the person who did the thing (the
    approver who is also a seat holder, the requester who rejects, the
    person who revokes their own access).

    First the table row pins who is told and with which kind when nobody is
    acting, so a planner that returns an empty list cannot pass the actor
    half. Then each recipient in turn is made the actor and must be the only
    one missing.
    """
    baseline = planner(None)
    assert {item.user_id for item in baseline} == recipients
    assert {item.kind for item in baseline} == {kind}

    for actor in recipients:
        assert {item.user_id for item in planner(actor)} == recipients - {actor}


def test_one_person_gets_one_notification_per_change() -> None:
    """Catches an implementation that writes two rows to a person who is named
    on two seats, who is both a named approver and a group holder, or who is
    both requester and target.
    """
    same_person = uuid.uuid4()
    seats = [
        _seat(ApproverSlot.DEPARTMENT_MANAGER, same_person),
        _seat(ApproverSlot.SYSTEM_OWNER, same_person),
        _seat(ApproverSlot.IT_ADMIN),
    ]
    holders = {
        ApproverSlot.IT_ADMIN: [FakeHolder(same_person, ACTIVE), FakeHolder(ADMIN_ONE, ACTIVE)]
    }

    approvers = plan_approval_needed(FACTS, seats, holders, actor_id=None)

    assert sorted(_users(approvers)) == sorted([same_person, ADMIN_ONE])

    self_request = RequestFacts(
        request_id=FACTS.request_id,
        number=FACTS.number,
        role_name=FACTS.role_name,
        requester_id=same_person,
        target_id=same_person,
    )
    assert _users(plan_granted(self_request, actor_id=None)) == [same_person]
    assert _users(plan_rejected(self_request, "No", actor_id=None)) == [same_person]


def test_wording_names_the_number_and_role_in_plain_words() -> None:
    """Catches a title that drops the request number or the role name, a body
    that does not tell the approver where to act, and a body that shows a
    person a code value (`department_manager`, `wiki.viewer`) instead of a
    plain label.
    """
    approval = _approval_needed(None)[0]
    assert "REQ-0022" in approval.title
    assert "Data Warehouse engineer" in approval.title
    assert approval.body.endswith("Open Approvals to decide.")

    code_value = re.compile(r"[A-Za-z_]+\.[A-Za-z_]+")
    every_body = [
        approval.body,
        _progress(None)[0].body,
        _granted(None)[0].body,
        _failed(None)[0].body,
    ]
    for body in every_body:
        assert code_value.search(body) is None, body
        assert "_" not in body, body


@pytest.mark.parametrize(
    ("open_slots", "decided_slots", "labels"),
    [
        pytest.param(
            [ApproverSlot.DEPARTMENT_MANAGER, ApproverSlot.IT_ADMIN, ApproverSlot.SYSTEM_OWNER],
            [ApproverSlot.HR],
            ["Department manager", "IT admin", "System owner"],
            id="three-open-hr-decided",
        ),
        pytest.param([ApproverSlot.HR], [ApproverSlot.IT_ADMIN], ["HR"], id="hr-open"),
    ],
)
def test_progress_names_only_the_seats_still_waiting(
    open_slots: list[ApproverSlot], decided_slots: list[ApproverSlot], labels: list[str]
) -> None:
    """Catches a progress body that names the decided seats too, or that shows
    the raw slot code (`department_manager`) instead of a plain label.

    A decided seat's label must be absent, so a planner that lists every seat
    fails.
    """
    seats = [_seat(slot, uuid.uuid4(), decided=True) for slot in decided_slots]
    seats += [_seat(slot) for slot in open_slots]

    planned = plan_progress(FACTS, seats, actor_id=None)

    assert _users(planned) == [REQUESTER]
    body = planned[0].body
    for label in labels:
        assert label in body
    all_labels = {
        ApproverSlot.DEPARTMENT_MANAGER: "Department manager",
        ApproverSlot.IT_ADMIN: "IT admin",
        ApproverSlot.SYSTEM_OWNER: "System owner",
        ApproverSlot.HR: "HR",
    }
    for slot in decided_slots:
        assert all_labels[slot] not in body
    assert "_" not in body, "a raw slot code such as department_manager"


def _rejected_body(reason: str) -> str:
    return plan_rejected(FACTS, reason, actor_id=None)[0].body


def _revoked_body(reason: str) -> str:
    return plan_revoked(
        user_id=TARGET,
        role_name=FACTS.role_name,
        request_number=None,
        reason=reason,
        actor_id=None,
    )[0].body


@pytest.mark.parametrize("body_of", [_rejected_body, _revoked_body], ids=["rejected", "revoked"])
def test_a_long_reason_is_cut_to_the_body_cap(body_of: Callable[[str], str]) -> None:
    """Catches a body that grows past the column width (the write would fail and
    undo the change), a cut that lands one short or one over the cap, a cut
    that leaves off the ellipsis, and a cut that fires one character too
    early on a body that fits exactly.

    The fixed text around the reason is measured, not guessed: the reason
    that makes the body exactly the cap must be kept whole, and one more
    character must be cut back to the cap ending in the ellipsis.
    """
    assert NOTIFICATION_BODY_MAX_LENGTH == 2000

    one = len(body_of("x"))
    assert len(body_of("xx")) == one + 1, "premise: the reason is added as it is"
    fits_exactly = NOTIFICATION_BODY_MAX_LENGTH - (one - 1)

    whole = body_of("x" * fits_exactly)
    assert len(whole) == NOTIFICATION_BODY_MAX_LENGTH
    assert whole.count("x") == fits_exactly
    assert ELLIPSIS not in whole

    cut = body_of("x" * (fits_exactly + 1))
    assert len(cut) == NOTIFICATION_BODY_MAX_LENGTH
    assert cut.endswith(ELLIPSIS)

    huge = body_of("x" * 2000)
    assert len(huge) == NOTIFICATION_BODY_MAX_LENGTH
    assert huge.endswith(ELLIPSIS)
