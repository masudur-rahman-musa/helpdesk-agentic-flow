"""One record of what the chat has found, read by every check.

What it reads: the new `helpdesk.agent_api.agent.found` module with
`Found.of(kept, results)` (a frozen record with `results`, `names`, `people`
and `roles`) and the checks that read it: holding a change, naming its ids,
the written question and the reply check. `Fact` stays importable from
`confirmation`.

Cost and isolation. Pure: the real `ModelClient` over `httpx.MockTransport`,
the fakes of `test_agent_loop.py` (`FakeStore`, `FakeTools`, `ScriptedGateway`)
and plain objects. No service, no `db` marker, no socket, no waiting.
"""

import json
import re
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from helpdesk.agent_api.agent import confirmation, graph
from helpdesk.agent_api.agent.confirmation import change_summary, confirm_facts
from helpdesk.agent_api.agent.found import Found
from helpdesk.agent_api.agent.graph import CHECK_NOTE_MARK, UNNAMED_ID
from helpdesk.agent_api.agent.loop import CHANGE_FAILED_REPLY
from helpdesk.agent_api.agent.loop_tools import DepartmentRow, PersonRow
from helpdesk.agent_api.agent.state import ResolvedEntities, ToolResult, TurnOutcome, ValidatedCall
from helpdesk.shared.core.errors import ErrorCode
from helpdesk.shared.models.conversation import MessageRole
from tests.agent_api.test_agent_loop import (
    PENDING_LISTING,
    REQUEST_ID,
    USER_ID,
    WIKI_EDITOR_ID,
    WIKI_VIEWER_ID,
    FakeStore,
    FakeTools,
    ScriptedGateway,
    _create_arguments,
    answer_body,
    question_body,
    role_row,
    run,
    tool_turns,
    wire_call,
)

PERSON_ID = uuid.UUID(int=201)
DEPARTMENT_ID = uuid.UUID(int=301)
HIDDEN_ID = uuid.UUID(int=999)
KEY = "secret-key-0001"
CONFIRM_WORDS = "Bitte antworte mit Ja."
WRITTEN_VIEWER_30 = (
    "Soll ich Internal Wiki viewer für 30 Tage beantragen? "
    f"Grund: Obviously fake reason for a test. {CONFIRM_WORDS}"
)
WRITTEN_FOR_DANA = (
    "Soll ich Internal Wiki viewer für Dana Fake für 30 Tage beantragen? "
    f"Grund: Obviously fake reason for a test. {CONFIRM_WORDS}"
)
UNNAMED_REASON = {
    "error": "bad_arguments",
    "reason": f"role_id: {UNNAMED_ID}",
}

ME_RESULT = ToolResult(
    tool="get_me", ok=True, payload={"user_id": str(USER_ID), "display_name": "Fake Person"}
)
ROLES_PAYLOAD: dict[str, Any] = {
    "roles": [
        role_row("wiki", "Internal Wiki", "viewer"),
        role_row("wiki", "Internal Wiki", "editor"),
    ]
}
VIEWER_FOUND = ToolResult(
    tool="find_role",
    ok=True,
    payload={
        "roles": [
            {
                "role_id": str(WIKI_VIEWER_ID),
                "name": "Internal Wiki viewer",
                "system": "Internal Wiki",
                "already_held": False,
            }
        ]
    },
)
LISTING_OF_APPROVALS = ToolResult(
    tool="list_pending_approvals",
    ok=True,
    payload={
        "approvals": [
            {
                "request_id": str(REQUEST_ID),
                "request_number": "REQ-0001",
                "status": "pending",
                "system_name": "Internal Wiki",
                "role_slug": "viewer",
            }
        ]
    },
)


def _person_found(user_id: uuid.UUID, name: str, department: str) -> ToolResult:
    return ToolResult(
        tool="find_person",
        ok=True,
        payload={"people": [{"user_id": str(user_id), "name": name, "department": department}]},
    )


def _department_found(name: str) -> ToolResult:
    return ToolResult(
        tool="find_department",
        ok=True,
        payload={"departments": [{"department_id": str(DEPARTMENT_ID), "name": name}]},
    )


def _call(tool: str, **arguments: Any) -> ValidatedCall:
    return ValidatedCall(tool=tool, arguments={"idempotency_key": KEY, **arguments})


def _transfer() -> ValidatedCall:
    return _call(
        "transfer_user",
        user_id=str(PERSON_ID),
        new_department_id=str(DEPARTMENT_ID),
        effective_date="2026-11-02",
    )


def _texts(calls: Sequence[ValidatedCall], results: Sequence[ToolResult]) -> tuple[str, list[str]]:
    """The summary and the text of every fact for the held calls."""
    resolved = ResolvedEntities()
    summary = change_summary(calls, results, resolved)
    return summary, [fact.text for fact in confirm_facts(calls, results, resolved)]


def _answer(request_body: Mapping[str, Any], call_id: str) -> dict[str, Any]:
    """What a request carried as the tool turn for `call_id`, parsed."""
    turns = [turn for turn in tool_turns(request_body) if turn["tool_call_id"] == call_id]
    assert len(turns) == 1, f"{len(turns)} tool turns for {call_id}"
    parsed = json.loads(turns[0]["content"])
    assert isinstance(parsed, dict)
    return parsed


def _tools() -> FakeTools:
    """A tool server that answers the role listing `find_role` reads."""
    return FakeTools({"get_me": ME_RESULT.payload, "list_requestable_roles": ROLES_PAYLOAD})


# --- the record ---------------------------------------------------------------------


def test_the_record_holds_kept_then_this_turn_ok_results() -> None:
    """Catches a record that puts this turn's results before the kept ones, that
    lets a refused result in (its ids and names would then count as found), or
    that forgets the kept results.

    Two kept results (one ok, one refused whose payload still holds a person
    and a role) and two from this turn (one ok, one refused). The record's
    results are the two ok ones, kept first; the refused ids and names are in
    none of `names`, `people` or `roles`.
    """
    kept_ok = _person_found(PERSON_ID, "Dana Fake", "Sales")
    kept_refused = ToolResult(
        tool="find_person",
        ok=False,
        error_code=ErrorCode.NOT_FOUND,
        payload={
            "people": [{"user_id": str(HIDDEN_ID), "name": "Eve Hidden"}],
            "roles": [{"role_id": str(WIKI_EDITOR_ID), "name": "Hidden role"}],
        },
    )
    now_ok = VIEWER_FOUND
    now_refused = ToolResult(
        tool="find_department",
        ok=False,
        error_code=ErrorCode.FORBIDDEN,
        payload={"departments": [{"department_id": str(HIDDEN_ID), "name": "Hidden Dept"}]},
    )

    found = Found.of([kept_ok, kept_refused], [now_ok, now_refused])

    assert found.results == (kept_ok, now_ok)
    assert set(found.names) == {str(PERSON_ID), str(WIKI_VIEWER_ID)}
    assert found.names[str(PERSON_ID)].shown == "Dana Fake"
    assert found.people == {str(PERSON_ID): "Dana Fake"}
    assert set(found.roles) == {str(WIKI_VIEWER_ID)}
    assert found.roles[str(WIKI_VIEWER_ID)]["name"] == "Internal Wiki viewer"


# --- holding a change reads the record ------------------------------------------------


def test_an_own_id_found_in_an_earlier_turn_is_held() -> None:
    """Catches a hold that accepts only ids from this turn's results, so the
    person's own id (found by `get_me` in an earlier turn) is refused, the
    change is refused twice and the person is told it could not be put together.

    `get_me` is kept; `find_role` runs this turn; the filing is for the person.
    It must be held on the first try, with the confirm outcome and no refusal
    step (three model requests: lookup, filing, written question).
    """
    gateway = ScriptedGateway(
        answer_body(None, wire_call("call_r", "find_role", {"words": "wiki viewer"})),
        answer_body(
            None,
            wire_call(
                "call_c",
                "create_access_request",
                _create_arguments(role_id=str(WIKI_VIEWER_ID)),
            ),
        ),
        question_body(WRITTEN_VIEWER_30),
    )
    tools = _tools()
    store = FakeStore(kept=[ME_RESULT])

    state = run(gateway, tools, store)

    assert tools.names == ["list_requestable_roles"]
    assert len(gateway.requests) == 3
    assert len(store.held) == 1
    assert store.held[0].calls[0].arguments["target_user_id"] == str(USER_ID)
    assert state.outcome is TurnOutcome.CONFIRM
    assert not any(turn["content"].startswith('{"error"') for turn in _all_tool_turns(gateway))


def _all_tool_turns(gateway: ScriptedGateway) -> list[dict[str, Any]]:
    return [turn for body in gateway.requests for turn in tool_turns(body)]


def test_a_role_found_in_an_earlier_turn_is_held_after_a_reason() -> None:
    """Catches a hold that cannot use a role found two turns ago, so a person
    who only gives the reason is told "I could not put that change together
    correctly".

    The role and the person were found earlier and are kept; this turn runs no
    lookup. The filing is held, its question names the role, and no reply the
    person is shown is the failure line.
    """
    gateway = ScriptedGateway(
        answer_body(
            None,
            wire_call(
                "call_c",
                "create_access_request",
                _create_arguments(role_id=str(WIKI_VIEWER_ID)),
            ),
        ),
        question_body(WRITTEN_VIEWER_30),
    )
    tools = FakeTools()
    store = FakeStore(kept=[ME_RESULT, VIEWER_FOUND])

    state = run(gateway, tools, store, message="Because I need the handbook.")

    assert tools.calls == []
    assert len(gateway.requests) == 2
    assert len(store.held) == 1
    assert store.held[0].calls[0].arguments["role_id"] == str(WIKI_VIEWER_ID)
    assert "Internal Wiki viewer" in store.held[0].asked
    assert state.outcome is TurnOutcome.CONFIRM
    assert CHANGE_FAILED_REPLY not in [text for _, text in store.messages]
    assert state.reply != CHANGE_FAILED_REPLY
    assert (MessageRole.ASSISTANT, CHANGE_FAILED_REPLY) not in store.messages


def test_a_person_found_earlier_is_named_in_the_question() -> None:
    """Catches a hold that reads the people found this turn only, so a filing
    for someone found in an earlier turn is refused, or is held with the
    person shown as "someone else".

    `find_person` is kept; `find_role` runs this turn; the filing is for
    Dana Fake. It is held, the held extraction names Dana Fake, and the
    question the person sees names her and never says "someone else".
    """
    gateway = ScriptedGateway(
        answer_body(None, wire_call("call_r", "find_role", {"words": "wiki viewer"})),
        answer_body(
            None,
            wire_call(
                "call_c",
                "create_access_request",
                _create_arguments(role_id=str(WIKI_VIEWER_ID), target_user_id=str(PERSON_ID)),
            ),
        ),
        question_body(WRITTEN_FOR_DANA),
    )
    store = FakeStore(principals=["manager"], kept=[_person_found(PERSON_ID, "Dana Fake", "Sales")])

    state = run(gateway, _tools(), store)

    assert state.outcome is TurnOutcome.CONFIRM
    assert len(store.held) == 1
    held = store.held[0]
    assert held.extraction.entities.target_user == "Dana Fake"
    assert "Dana Fake" in held.asked
    assert "someone else" not in held.asked.lower()


def test_an_id_only_written_in_the_chat_is_refused() -> None:
    """Unhappy path. Catches a hold that counts an id as found because it sits
    in the history text or in the person's own message, so anyone could hand
    the model any id.

    The role id is in an earlier assistant message and in the new message, in
    no result kept or new; `get_me` found the person, so the role is the only
    id left. The model's next step gets the refusal with `role_id` and the
    unnamed reason, and nothing is held.
    """
    history = [
        ("user", "Which role is the wiki viewer?"),
        ("assistant", f"The wiki viewer role id is {WIKI_VIEWER_ID}."),
    ]
    gateway = ScriptedGateway(
        answer_body(None, wire_call("call_me", "get_me", {})),
        answer_body(
            None,
            wire_call(
                "call_c",
                "create_access_request",
                _create_arguments(role_id=str(WIKI_VIEWER_ID)),
            ),
        ),
        answer_body("Let me find that role first."),
    )
    store = FakeStore(history=history, kept=[ME_RESULT])

    run(gateway, _tools(), store, message=f"Use role {WIKI_VIEWER_ID} please.")

    assert store.held == []
    assert _answer(gateway.requests[2], "call_c") == UNNAMED_REASON


def test_an_id_only_in_a_refused_result_is_refused() -> None:
    """Unhappy path. Catches a record that counts the rows of a refused result
    as found, so an id the server never confirmed is held.

    A kept refused `find_role` result still carries the role row in its
    payload. It is in no ok result, so the filing is refused with `role_id`
    and the unnamed reason, and nothing is held.
    """
    refused = ToolResult(
        tool="find_role",
        ok=False,
        error_code=ErrorCode.NOT_FOUND,
        payload=VIEWER_FOUND.payload,
    )
    gateway = ScriptedGateway(
        answer_body(None, wire_call("call_me", "get_me", {})),
        answer_body(
            None,
            wire_call(
                "call_c",
                "create_access_request",
                _create_arguments(role_id=str(WIKI_VIEWER_ID)),
            ),
        ),
        answer_body("Let me find that role first."),
    )
    store = FakeStore(kept=[refused])

    run(gateway, _tools(), store)

    assert store.held == []
    assert _answer(gateway.requests[2], "call_c") == UNNAMED_REASON


# --- the question shows the current details -------------------------------------------


def test_a_move_shows_the_department_left_and_joined() -> None:
    """Catches a move line that shows only the department joined, so the
    person cannot see which one the person leaves.

    The person row says department Sales and the department row says
    Procurement. The line holds `Dana Fake (department now Sales)` and
    `new department Procurement`, and both Sales and Procurement are facts the
    written question must copy.
    """
    results = [_person_found(PERSON_ID, "Dana Fake", "Sales"), _department_found("Procurement")]

    summary, facts = _texts([_transfer()], results)

    assert "Dana Fake (department now Sales)" in summary
    assert "new department Procurement" in summary
    assert "Sales" in facts
    assert "Procurement" in facts
    assert str(PERSON_ID) not in summary


def _onboarding() -> ValidatedCall:
    return _call(
        "onboard_user",
        email="mara.quill@cuan.example",
        display_name="Mara Quill",
        department_id=str(DEPARTMENT_ID),
        start_date="2026-11-02",
        manager_user_id=str(PERSON_ID),
    )


def _offboarding() -> ValidatedCall:
    return _call("offboard_user", user_id=str(PERSON_ID), end_date="2026-11-30")


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(_onboarding(), id="manager_is_not_the_first_id"),
        pytest.param(_offboarding(), id="no_field_of_the_first_record_is_set"),
    ],
)
def test_details_follow_the_first_id_only(call: ValidatedCall) -> None:
    """Catches a line that adds a "now" detail to every id (an onboarding's
    manager would show the manager's department as if it were being changed),
    or one that adds details when the change sets no field of its first record.

    The person row has department Finance. In the onboarding the argument
    `department` is set, but the first id is the department, not the manager;
    in the offboarding nothing sets a department. Neither line shows "now" and
    Finance is not a fact.
    """
    results = [_person_found(PERSON_ID, "Dana Fake", "Finance"), _department_found("Sales")]

    summary, facts = _texts([call], results)

    assert "now" not in summary
    assert "Finance" not in summary
    assert "Finance" not in facts
    assert "Dana Fake" in summary


def test_a_written_move_without_the_old_department_goes_back() -> None:
    """Catches a written question that may leave out the department the person
    leaves, because only the one they join is a fact.

    The first written question names Procurement but not Sales; it goes back
    once with Sales named as missing and Procurement not. The second names both
    and is the question held.
    """
    first = f"Soll ich Dana Fake zum 2026-11-02 nach Procurement versetzen? {CONFIRM_WORDS}"
    second = (
        f"Soll ich Dana Fake (Sales) zum 2026-11-02 nach Procurement versetzen? {CONFIRM_WORDS}"
    )
    gateway = ScriptedGateway(
        answer_body(
            None,
            wire_call("call_p", "find_person", {"words": "dana"}),
            wire_call("call_d", "find_department", {"words": "procurement"}),
        ),
        answer_body(
            None,
            wire_call(
                "call_c",
                "transfer_user",
                {
                    "user_id": str(PERSON_ID),
                    "new_department_id": str(DEPARTMENT_ID),
                    "effective_date": "2026-11-02",
                },
            ),
        ),
        question_body(first),
        question_body(second),
    )
    store = FakeStore(
        principals=["hr"],
        people=[PersonRow(id=PERSON_ID, display_name="Dana Fake", department="Sales")],
        departments=[DepartmentRow(id=DEPARTMENT_ID, name="Procurement")],
    )

    state = run(gateway, FakeTools(), store)

    assert len(gateway.requests) == 4
    note = gateway.requests[3]["messages"][-1]["content"]
    assert '"Sales" (missing)' in note
    assert '"Procurement" (missing)' not in note
    assert state.outcome is TurnOutcome.CONFIRM
    assert store.held[0].asked == second


# --- the reply check: what still needs this turn ---------------------------------------


def test_status_words_still_need_this_turn() -> None:
    """Unhappy path. Catches a reply check that lets a status word through
    because a result kept from an earlier turn said it (a status can have
    changed since), or one that stops accepting a request number a kept result
    holds.

    The kept listing says REQ-0001 is pending. This turn runs no lookup. A
    reply saying "pending" is sent back (the reason names the word and not the
    number); the corrected reply keeps the number and passes.
    """
    gateway = ScriptedGateway(
        answer_body("Your request REQ-0001 is pending."),
        answer_body("Your request REQ-0001 is waiting for a decision."),
    )
    store = FakeStore(kept=[ToolResult(tool="list_my_requests", ok=True, payload=PENDING_LISTING)])

    state = run(gateway, FakeTools(), store)

    assert len(gateway.requests) == 2
    note = gateway.requests[1]["messages"][-1]
    assert note["role"] == "system"
    assert note["content"].startswith(CHECK_NOTE_MARK)
    assert "pending" in note["content"]
    assert "REQ-0001" not in note["content"]
    assert state.reply == "Your request REQ-0001 is waiting for a decision."


def test_a_decision_still_needs_this_turns_listing() -> None:
    """Unhappy path. Catches a hold that takes a request id from a
    `list_pending_approvals` result kept from an earlier turn (the request may
    since be decided), now that other ids count from the whole chat.

    The kept listing holds the request id. This turn does not list. The
    approval is refused with the exact "list them first" reason and nothing
    is held; the id is named by the kept listing, so only that check refuses.
    """
    gateway = ScriptedGateway(
        answer_body(None, wire_call("call_d", "approve_request", {"request_id": str(REQUEST_ID)})),
        answer_body("Let me list them first."),
    )
    store = FakeStore(principals=["manager"], kept=[LISTING_OF_APPROVALS])

    run(gateway, FakeTools(), store)

    assert store.held == []
    assert _answer(gateway.requests[1], "call_d") == {
        "error": "bad_arguments",
        "reason": "request_id: not in this turn's list_pending_approvals result; list them first",
    }


# --- one copy ---------------------------------------------------------------------


def test_no_second_copy_of_found_facts() -> None:
    """Catches a refactor that leaves a second copy of what the chat found: the
    old id list, the per-turn role and people maps, the per-turn id check, or a
    second `_found_names` beside the record.

    The graph module has none of `ID_FIELDS`, `roles_found`, `people_found` or
    `_unfound_id`, the confirmation module has no `_found_names`, and the
    record module is where `_found_names` lives.
    """
    from helpdesk.agent_api.agent import found

    graph_source = Path(graph.__file__).read_text(encoding="utf-8")
    for name in ("ID_FIELDS", "roles_found", "people_found", "_unfound_id", "_found_names"):
        assert re.search(rf"\b{name}\b", graph_source) is None, name
    assert not hasattr(confirmation, "_found_names")
    assert hasattr(found, "_found_names")
