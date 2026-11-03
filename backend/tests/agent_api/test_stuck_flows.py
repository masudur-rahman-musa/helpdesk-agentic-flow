"""Flows that used to stop half way: a bare yes, onboarding, department lookup.

Cost and isolation. Pure: the real ``ModelClient`` over ``httpx.MockTransport``
and the fakes of ``test_agent_loop.py``. No service, no ``db`` marker, no
socket, no waiting.

Expected implementation surface: ``graph.PICK_AGAIN_REPLY``;
``loop_tools.DepartmentRow``, ``FIND_DEPARTMENT`` and ``find_department``;
``Directory.departments``; ``shared.access.onboarding.EMAIL_MAX_LENGTH``.
"""

import asyncio
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from helpdesk.agent_api.agent import confirmation
from helpdesk.agent_api.agent.graph import FAILURE_REPLY, PICK_AGAIN_REPLY, today_line
from helpdesk.agent_api.agent.loop_tools import (
    FIND_DEPARTMENT,
    DepartmentRow,
    PersonRow,
    find_department,
    offered_tools,
)
from helpdesk.agent_api.agent.state import TurnOutcome, TurnState
from helpdesk.shared.access.onboarding import EMAIL_MAX_LENGTH
from helpdesk.shared.core.errors import ErrorCode
from helpdesk.shared.models.conversation import MessageRole
from tests.agent_api.test_agent_loop import (
    START,
    FakeStore,
    FakeTools,
    ScriptedGateway,
    answer_body,
    question_body,
    run,
    tool_names,
    tool_turns,
    wire_call,
)
from tests.agent_api.test_budget_refusal import RefusingGateway

PERSON_ID = uuid.UUID(int=201)
UNKNOWN_ID = uuid.UUID(int=999)
PROCUREMENT_ID = uuid.UUID(int=301)
ENGINEERING_ID = uuid.UUID(int=302)

PICK_QUESTION = "Do you mean the viewer role or the editor role?"
PICK_HISTORY = [("user", "I need wiki access"), ("assistant", PICK_QUESTION)]
PICK_CHOICES = ("Internal Wiki viewer", "Internal Wiki editor")


def kept_choices() -> "confirmation.OfferedChoices":
    """The two roles the pick question offered, kept with the chat."""
    return confirmation.OfferedChoices(asked_at=START, choices=PICK_CHOICES)


OUTAGE = (503, {"error": "down"})

TYPED = "mara.quill@cuan.example"
ONBOARD_MESSAGE = f"Onboard Mara Quill, {TYPED}, into Engineering"

DEPARTMENTS = [
    DepartmentRow(id=PROCUREMENT_ID, name="Procurement"),
    DepartmentRow(id=ENGINEERING_ID, name="Engineering"),
    DepartmentRow(id=uuid.UUID(int=303), name="Finance"),
    DepartmentRow(id=uuid.UUID(int=304), name="Sales"),
]

# --- a bare yes with nothing held -----------------------------------------------


@pytest.mark.parametrize("message", ["yes", "ja"])
def test_a_yes_with_nothing_held_and_a_failed_step_asks_which_one(message: str) -> None:
    """Catches a loop that ends the failed turn on the generic failure reply
    although the last reply offered choices, or one that hides the failure by
    changing the outcome or the error code, so telemetry would stop showing it.

    After a pick question (its two choices kept) the person says yes, no change
    is held, and the gateway is down. The reply must be `PICK_AGAIN_REPLY`,
    saved as the assistant message, with outcome `FAILED` and error `INTERNAL`.
    """
    store = FakeStore(history=PICK_HISTORY, question=kept_choices())

    state = run(RefusingGateway(OUTAGE), FakeTools(), store, message=message)

    assert state.reply == PICK_AGAIN_REPLY
    assert state.outcome is TurnOutcome.FAILED
    assert state.error_code is ErrorCode.INTERNAL
    assert store.messages[-1] == (MessageRole.ASSISTANT, PICK_AGAIN_REPLY)


def test_a_yes_with_nothing_held_still_goes_to_the_model() -> None:
    """Catches a loop that answers a bare yes by code, before the model, so the
    model never gets to say which one it meant.

    The gateway holds one answer; the tool server holds nothing. The model's
    own words must be the reply, the yes must be its last message (the kept
    choices come before it), and nothing is held or run.
    """
    gateway = ScriptedGateway(answer_body("Then the viewer role. Shall I file it?"))
    tools = FakeTools()
    store = FakeStore(history=PICK_HISTORY, question=kept_choices())

    state = run(gateway, tools, store, message="yes")

    assert state.reply == "Then the viewer role. Shall I file it?"
    assert state.outcome is TurnOutcome.DONE
    assert len(gateway.requests) == 1
    assert gateway.requests[0]["messages"][-1] == {
        "role": "user",
        "content": f"{today_line(START)}\nyes",
    }
    assert tools.calls == []
    assert store.held == []


def test_any_other_failed_turn_still_shows_the_generic_reply() -> None:
    """Unhappy path. Catches a loop that shows the pick-again reply for every
    failure after a pick question, although no choices were kept.

    Same history and same outage as the yes case; no choices are kept and the
    message differs.
    """
    store = FakeStore(history=PICK_HISTORY)

    state = run(RefusingGateway(OUTAGE), FakeTools(), store, message="show me my access")

    assert state.reply == FAILURE_REPLY
    assert state.outcome is TurnOutcome.FAILED
    assert state.error_code is ErrorCode.INTERNAL


# --- onboarding takes the typed email -------------------------------------------


def _onboard(email: str, manager: uuid.UUID = PERSON_ID) -> dict[str, Any]:
    return {
        "email": email,
        "display_name": "Mara Quill",
        "department_id": str(ENGINEERING_ID),
        "start_date": "2026-11-02",
        "manager_user_id": str(manager),
    }


ONBOARD_LINE_START = "I'll onboard user: email "
ONBOARD_LINE_END = (
    ", display name Mara Quill, department Engineering, start date 2026-11-02, manager Dana Fake."
)


def _written_onboarding(message: str, history: Sequence[tuple[str, str]]) -> str:
    """The last scripted answer of `_onboarding`. When the change is held it is
    the written question: it names the joiner, the start date and each address
    the person typed. When the call is sent back it is the model's follow-up."""
    typed = re.findall(
        r"[\w.+-]+@[\w-]+\.[\w.-]+", " ".join([*(t for r, t in history if r == "user"), message])
    )
    lines = [f"{ONBOARD_LINE_START}{email}{ONBOARD_LINE_END}" for email in dict.fromkeys(typed)]
    return " ".join(["Which address should I use?", *lines, "Mara Quill 2026-11-02"])


def _onboarding(
    sent: str,
    message: str,
    history: Sequence[tuple[str, str]] = (),
    *,
    manager: uuid.UUID = PERSON_ID,
    look_up_manager: bool = True,
) -> tuple[TurnState, FakeStore, ScriptedGateway]:
    """One turn: the department (the HR user's own, from `get_me`) and the
    manager are looked up, then the model sends `onboard_user` with `sent` as
    the email. A last answer is scripted for a sent-back call."""
    lookups = [wire_call("call_d", "get_me", {})]
    if look_up_manager:
        lookups.append(wire_call("call_p", "find_person", {"words": "dana"}))
    steps = [answer_body(None, *lookups)]
    steps.append(answer_body(None, wire_call("call_c", "onboard_user", _onboard(sent, manager))))
    steps.append(question_body(_written_onboarding(message, history)))
    gateway = ScriptedGateway(*steps)
    store = FakeStore(
        principals=["hr"],
        history=history,
        people=[PersonRow(id=PERSON_ID, display_name="Dana Fake", department="Sales")],
    )
    me = {"department_id": str(ENGINEERING_ID), "department_name": "Engineering"}
    state = run(gateway, FakeTools({"get_me": me}), store, message=message)
    return state, store, gateway


def _held_email(store: FakeStore) -> str:
    assert len(store.held) == 1
    calls = store.held[0].calls
    assert [call.tool for call in calls] == ["onboard_user"]
    return str(calls[0].arguments["email"])


def _sent_back_reason(gateway: ScriptedGateway) -> str:
    """The reason the model was given for its `onboard_user` call."""
    turns = [turn for turn in tool_turns(gateway.requests[-1]) if turn["tool_call_id"] == "call_c"]
    assert len(turns) == 1
    body = json.loads(turns[0]["content"])
    assert body["error"] == "bad_arguments"
    return str(body["reason"])


def test_onboarding_takes_the_email_the_person_typed() -> None:
    """Catches a loop that holds the model's `<EMAIL_1>` placeholder (the
    gateway hides the real address), or one that keeps the comma that follows
    the typed address.

    The person types the address inside a sentence; the model sends the
    placeholder. The held call must carry exactly the typed address.
    """
    state, store, _ = _onboarding("<EMAIL_1>", ONBOARD_MESSAGE)

    assert _held_email(store) == TYPED
    assert state.outcome is TurnOutcome.CONFIRM


def test_onboarding_ignores_an_email_the_model_made_up() -> None:
    """Catches a loop that only fills the email when the model left it empty or
    a placeholder, and so trusts a well-formed address the model invented.

    The model sends a valid address of its own; the held email must still be
    the typed one.
    """
    _, store, _ = _onboarding("someone@else.example", ONBOARD_MESSAGE)

    assert _held_email(store) == TYPED


def test_the_latest_message_with_an_email_wins() -> None:
    """Catches a loop that takes the first address in the chat, or the oldest.

    Turn one holds `a.one`, turn two holds `b.two` and is the latest person
    message with an address; the held email must be `b.two`.
    """
    history = [
        ("user", "Onboard Mara Quill, a.one@cuan.example"),
        ("assistant", "Which department?"),
    ]

    _, store, _ = _onboarding("<EMAIL_1>", "Engineering, address b.two@cuan.example", history)

    assert _held_email(store) == "b.two@cuan.example"


def test_an_earlier_message_supplies_the_email_when_the_latest_has_none() -> None:
    """Catches a loop that reads only the message just sent, so a joiner whose
    address came a turn earlier is asked for it again.

    The address is in the first person message; the latest one holds none.
    """
    history = [("user", ONBOARD_MESSAGE), ("assistant", "Which department?")]

    _, store, _ = _onboarding("<EMAIL_1>", "Engineering", history)

    assert _held_email(store) == TYPED


def test_an_address_in_an_assistant_message_is_not_taken() -> None:
    """Catches a loop that scans every stored message, so an address the
    assistant repeated (or an injected one) beats the person's own.

    Only the user's message holds `TYPED`; a later assistant message holds
    another address. The held email must be `TYPED`.
    """
    history = [
        ("user", ONBOARD_MESSAGE),
        ("assistant", "I will use other.person@cuan.example, ok?"),
    ]

    _, store, _ = _onboarding("<EMAIL_1>", "Engineering", history)

    assert _held_email(store) == TYPED


def test_onboarding_with_no_typed_email_is_sent_back() -> None:
    """Unhappy path. Catches a loop that falls back to the model's address when
    the person typed none, and one whose reason leaks an address to the model.

    The model sends a well-formed address; the person typed none. Nothing is
    held, the reason names `email`, and it holds no `@`.
    """
    _, store, gateway = _onboarding(TYPED, "Onboard Mara Quill into Engineering")

    assert store.held == []
    reason = _sent_back_reason(gateway)
    assert "email" in reason
    assert "@" not in reason


def test_onboarding_with_two_emails_in_one_message_is_sent_back() -> None:
    """Unhappy path. Catches a loop that picks the first (or last) of two
    addresses in one message instead of asking which one.

    Nothing is held; the reason asks which address and shows neither of them.
    """
    message = "Onboard Mara Quill, mara.quill@cuan.example or m.quill@cuan.example"

    _, store, gateway = _onboarding("<EMAIL_1>", message)

    assert store.held == []
    reason = _sent_back_reason(gateway).lower()
    assert "which" in reason
    assert "address" in reason
    assert "@" not in reason


def test_a_typed_email_at_the_column_length_is_taken_and_one_over_is_sent_back() -> None:
    """Catches a loop that holds an address the column cannot store (the mutation
    would fail later), or that refuses one that exactly fits.

    One address of 320 characters is held; one of 321 is sent back with a reason
    naming `email`.
    """
    domain = "@cuan.example"
    fits = "a" * (EMAIL_MAX_LENGTH - len(domain)) + domain
    over = "a" * (EMAIL_MAX_LENGTH + 1 - len(domain)) + domain
    assert (len(fits), len(over)) == (320, 321)

    _, fits_store, _ = _onboarding("<EMAIL_1>", f"Onboard Mara Quill, {fits} into Engineering")
    _, over_store, over_gateway = _onboarding(
        "<EMAIL_1>", f"Onboard Mara Quill, {over} into Engineering"
    )

    assert _held_email(fits_store) == fits
    assert over_store.held == []
    assert "email" in _sent_back_reason(over_gateway)


def test_a_made_up_manager_id_is_still_named_before_the_email() -> None:
    """Unhappy path. Catches an email check that runs before the id check, so a
    call with two faults hides the made-up id.

    The person typed no address and the manager id was never looked up; the
    reason must name `manager_user_id` first.
    """
    _, store, gateway = _onboarding(
        "<EMAIL_1>",
        "Onboard Mara Quill into Engineering",
        manager=UNKNOWN_ID,
        look_up_manager=False,
    )

    assert store.held == []
    assert _sent_back_reason(gateway).startswith("manager_user_id")


def test_the_onboard_line_shows_the_email() -> None:
    """Catches a held line that leaves the address out, so the person cannot
    check it before saying yes.

    The held reply must hold the whole line with the typed address in brackets.
    """
    state, _, _ = _onboarding("<EMAIL_1>", ONBOARD_MESSAGE)

    assert state.reply is not None
    assert f"{ONBOARD_LINE_START}{TYPED}{ONBOARD_LINE_END}" in state.reply


# --- find_department ------------------------------------------------------------


@pytest.mark.parametrize("words", ["procurment", "PROCUREMENT"])
def test_find_department_matches_a_name_without_case_and_with_a_typo(words: str) -> None:
    """Catches a finder that compares exact, case-sensitive names, so a typo or
    capitals find nothing; and one that answers the wrong row first.

    Both spellings must answer the Procurement id first, as a string, with the
    stored name.
    """
    found = asyncio.run(find_department(words, directory=FakeStore(departments=DEPARTMENTS)))

    assert found[0] == {"department_id": str(PROCUREMENT_ID), "name": "Procurement"}


@pytest.mark.parametrize("words", ["Astronomy", "", "   "])
def test_find_department_answers_nothing_for_an_unknown_name(words: str) -> None:
    """Unhappy path. Catches a finder that answers its nearest row whatever is
    typed, or lists every department for blank words.
    """
    found = asyncio.run(find_department(words, directory=FakeStore(departments=DEPARTMENTS)))

    assert found == []


def test_find_department_answers_at_most_three() -> None:
    """Catches a finder with no cap, or a cap other than 3.

    Four departments all close to the words must answer exactly three.
    """
    names = ["Sales North", "Sales South", "Sales East", "Sales West"]
    rows = [DepartmentRow(id=uuid.UUID(int=400 + n), name=name) for n, name in enumerate(names)]

    found = asyncio.run(find_department("sales", directory=FakeStore(departments=rows)))

    assert len(found) == 3
    assert {row["name"] for row in found} <= set(names)


def _offered_names(principals: Sequence[str]) -> list[str]:
    return [tool.name for tool in offered_tools(principals)]


@pytest.mark.parametrize(
    ("principal", "offered"),
    [("hr", True), ("it_admin", True), ("employee", False), ("manager", True)],
)
def test_find_department_is_offered_only_to_roles_that_name_a_department(
    principal: str, offered: bool
) -> None:
    """Catches a finder offered to everyone (a name list for an employee who
    cannot act on it), or to nobody (hr could never propose a transfer).

    Each principal alone is offered the tools; the finder is there for `hr`,
    `it_admin` and `manager` (who opens a department's review) and absent for
    `employee`.
    """
    assert FIND_DEPARTMENT == "find_department"
    assert (FIND_DEPARTMENT in _offered_names([principal])) is offered


def _tool_content(request_body: Mapping[str, Any], call_id: str) -> str:
    turns = [turn for turn in tool_turns(request_body) if turn["tool_call_id"] == call_id]
    assert len(turns) == 1
    return str(turns[0]["content"])


def test_a_transfer_from_a_department_name_holds_the_found_id() -> None:
    """Catches a loop that cannot run the finder (the model is asked for an
    internal id), or one that refuses the found id on the transfer.

    The model finds the person, finds "Procurement", then proposes the move with
    both ids. The finder's answer must reach the model, and the change is held
    with the Procurement id.
    """
    gateway = ScriptedGateway(
        answer_body(None, wire_call("call_p", "find_person", {"words": "dana"})),
        answer_body(None, wire_call("call_d", FIND_DEPARTMENT, {"words": "Procurement"})),
        answer_body(
            None,
            wire_call(
                "call_c",
                "transfer_user",
                {
                    "user_id": str(PERSON_ID),
                    "new_department_id": str(PROCUREMENT_ID),
                    "effective_date": "2026-11-02",
                },
            ),
        ),
        question_body(
            "Soll ich Dana Fake zum 2026-11-02 nach Procurement versetzen? Bitte antworte mit Ja."
        ),
    )
    store = FakeStore(
        principals=["hr"],
        people=[PersonRow(id=PERSON_ID, display_name="Dana Fake", department="Sales")],
        departments=DEPARTMENTS,
    )

    state = run(gateway, FakeTools(), store)

    assert FIND_DEPARTMENT in tool_names(gateway.requests[0])
    assert str(PROCUREMENT_ID) in _tool_content(gateway.requests[2], "call_d")
    assert [call.tool for call in store.held[0].calls] == ["transfer_user"]
    assert store.held[0].calls[0].arguments["new_department_id"] == str(PROCUREMENT_ID)
    assert state.outcome is TurnOutcome.CONFIRM
