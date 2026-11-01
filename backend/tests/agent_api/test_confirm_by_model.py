"""The model reads the person's reply to a held change.

Cost and isolation. Pure: the real ``ModelClient`` over ``httpx.MockTransport``
and the fakes of ``test_agent_loop.py``. No service, no ``db`` marker, no
socket, no waiting.

Expected implementation surface: ``confirmation.Reading``,
``confirmation.ReplyReading``, ``confirmation.UNCLEAR_REPLY``,
``OpenQuestion.asked``, the ``read_reply`` prompt, and the reading call in
``graph``. The word lists and ``answer_words`` are gone.
"""

import importlib
import inspect
import json
import uuid
from typing import Any

import pytest

from helpdesk.agent_api.agent import confirmation, graph
from helpdesk.agent_api.agent.confirmation import (
    UNCLEAR_REPLY,
    OpenQuestion,
    Reading,
    ReplyReading,
    parse_open_question,
)
from helpdesk.agent_api.agent.graph import FAILURE_REPLY, PICK_AGAIN_REPLY
from helpdesk.agent_api.agent.prompts import load_prompt
from helpdesk.agent_api.agent.state import TurnOutcome
from helpdesk.agent_api.agent.tool_args import CreateAccessRequestArgs, RejectRequestArgs
from tests.agent_api.test_agent_loop import (
    FILED_PAYLOAD,
    FILED_REPLY,
    START,
    STORED_KEY,
    USER_ID,
    WIKI_VIEWER_ID,
    FakeStore,
    FakeTools,
    ScriptedGateway,
    answer_body,
    held_question,
    role_row,
    run,
    wire_call,
)
from tests.agent_api.test_budget_refusal import RefusingGateway

ASKED = "Shall I file Internal Wiki viewer access for 30 days?"
OUTAGE = (503, {"error": "down"})
PICK_HISTORY = [
    ("user", "I need wiki access"),
    ("assistant", "Do you mean the viewer role or the editor role?"),
]


def reading(answer: str) -> dict[str, Any]:
    """One scripted gateway answer holding the model's reading."""
    return answer_body(json.dumps({"answer": answer}))


def held() -> OpenQuestion:
    """The held change, with the shown question text set."""
    return held_question().model_copy(update={"asked": ASKED})


ME_PAYLOAD: dict[str, Any] = {"user_id": str(USER_ID), "display_name": "Fake Person"}
ROLES_PAYLOAD: dict[str, Any] = {"roles": [role_row("wiki", "Internal Wiki", "viewer")]}


def new_proposal() -> dict[str, Any]:
    """A model step that proposes a fresh filing call."""
    return answer_body(
        None,
        wire_call(
            "call_new",
            "create_access_request",
            {
                "target_user_id": str(USER_ID),
                "role_id": str(WIKI_VIEWER_ID),
                "justification": "Obviously fake reason for a test.",
                "requested_expiry_days": 10,
            },
        ),
    )


def _enums(node: object) -> list[list[str]]:
    """Every enum list found anywhere in a JSON schema."""
    found: list[list[str]] = []
    if isinstance(node, dict):
        values = node.get("enum")
        if isinstance(values, list):
            found.append([str(v) for v in values])
        for child in node.values():
            found.extend(_enums(child))
    elif isinstance(node, list):
        for child in node:
            found.extend(_enums(child))
    return found


@pytest.mark.parametrize("message", ["go ahead", "passt, mach das", "va bene"])
def test_a_model_yes_runs_the_held_calls_exactly(message: str) -> None:
    """Catches a yes that is still decided by word lists (these wordings are in
    none), that runs the model's idea of the call instead of the stored one,
    that files with a new key, that offers tools on the reply step, or that
    leaves the question on the chat row.

    The reading says yes; the stored call must run once with its stored key,
    nothing else may run, and the reply step must be answer-only.
    """
    gateway = ScriptedGateway(reading("yes"), answer_body(FILED_REPLY))
    tools = FakeTools({"create_access_request": FILED_PAYLOAD})
    question = held()
    store = FakeStore(question=question)

    state = run(gateway, tools, store, message=message)

    stored = CreateAccessRequestArgs.model_validate(question.calls[0].arguments)
    assert tools.calls == [("create_access_request", stored.model_dump(mode="json"))]
    assert tools.calls[0][1]["idempotency_key"] == STORED_KEY
    assert len(gateway.requests) == 2
    assert gateway.requests[1]["tool_choice"] == "none"
    assert "REQ-0001" in json.dumps(gateway.requests[1]["messages"])
    assert store.question is None
    assert store.held == []
    assert state.reply == FILED_REPLY


def test_the_reading_is_a_strict_schema_call_on_agent_default() -> None:
    """Catches a reading sent as free text (the answer could be anything), on
    the wrong alias, with tools offered (the model could act while reading), a
    schema that is not strict or lacks one of the four answers, one that leaves
    out the shown question or the person's words, or one that sends the wrong
    prompt version.

    The first request is checked field by field against the wire shape.
    """
    gateway = ScriptedGateway(reading("yes"), answer_body(FILED_REPLY))
    tools = FakeTools({"create_access_request": FILED_PAYLOAD})
    store = FakeStore(question=held())

    run(gateway, tools, store, message="go ahead")

    first = gateway.requests[0]
    prompt = load_prompt("read_reply")
    assert first["model"] == "agent-default"
    assert "tools" not in first
    assert first["response_format"]["type"] == "json_schema"
    assert first["response_format"]["json_schema"]["name"] == "ReplyReading"
    assert first["response_format"]["json_schema"]["strict"] is True
    enums = _enums(first["response_format"]["json_schema"]["schema"])
    assert sorted(["yes", "no", "new_info", "unclear"]) in [sorted(e) for e in enums]
    messages = first["messages"]
    assert [turn["role"] for turn in messages] == ["system", "user"]
    assert messages[0]["content"] == prompt.text
    assert json.loads(messages[1]["content"]) == {"question": ASKED, "message": "go ahead"}
    assert gateway.headers[0].get("X-Prompt-Version") == prompt.sha256


def test_a_model_no_drops_the_change_and_says_so() -> None:
    """Catches a no that still runs the held call, leaves the question on the
    chat row, hides the denial from the model, offers tools on the reply step,
    or answers with a fixed line instead of the model's own words.

    The reading says no; nothing runs, and the reply step is answer-only and
    carries the `denied_by_person` turn for the held call.
    """
    gateway = ScriptedGateway(reading("no"), answer_body("Okay, I dropped it."))
    tools = FakeTools()
    store = FakeStore(question=held())

    state = run(gateway, tools, store, message="nicht doch")

    assert tools.calls == []
    assert store.question is None
    assert store.held == []
    assert len(gateway.requests) == 2
    reply_step = gateway.requests[1]
    assert reply_step["tool_choice"] == "none"
    sent = reply_step["messages"]
    assert [call["id"] for call in sent[-3]["tool_calls"]] == ["held_0"]
    assert sent[-2]["role"] == "tool"
    assert sent[-2]["tool_call_id"] == "held_0"
    assert json.loads(sent[-2]["content"])["denied_by_person"] is True
    assert sent[-1] == {"role": "user", "content": "nicht doch"}
    assert state.reply == "Okay, I dropped it."


@pytest.mark.parametrize(
    "message",
    ["make it 10 days", "who approves it?", "yes, but make it 10 days"],
    ids=["a_change", "a_question", "yes_but"],
)
def test_new_info_goes_back_to_the_model_with_tools(message: str) -> None:
    """Catches a "yes, but ..." that runs the old change, a new fact that is
    dropped as a no (the model gets no tools to propose again), a denial that
    changes the person's words, or a question left on the chat row.

    The reading says new_info; nothing runs, the question is cleared, the next
    request offers tools on auto with the exact words in the denial, and the
    model's new proposal, made from ids its own lookups found, is held after
    one written question.
    """
    gateway = ScriptedGateway(
        reading("new_info"),
        answer_body(
            None,
            wire_call("call_me", "get_me", {}),
            wire_call("call_r", "find_role", {"words": "wiki viewer"}),
        ),
        new_proposal(),
        answer_body(
            "Soll ich Internal Wiki viewer für 10 Tage beantragen? "
            "Grund: Obviously fake reason for a test. Bitte antworte mit Ja."
        ),
    )
    tools = FakeTools({"get_me": ME_PAYLOAD, "list_requestable_roles": ROLES_PAYLOAD})
    store = FakeStore(question=held())

    state = run(gateway, tools, store, message=message)

    assert "create_access_request" not in tools.names
    assert store.question is None
    assert len(gateway.requests) == 4
    next_step = gateway.requests[1]
    assert next_step["tools"]
    assert next_step["tool_choice"] == "auto"
    denial = [turn for turn in next_step["messages"] if turn["role"] == "tool"]
    assert json.loads(denial[0]["content"]) == {"denied_by_person": True, "reason": message}
    assert state.outcome is TurnOutcome.CONFIRM
    assert len(store.held) == 1


def test_unclear_keeps_the_change_and_asks_yes_or_no() -> None:
    """Catches an unclear that clears the question (a later yes would find
    nothing), restarts its 24 hours by storing a new one, runs a tool, calls
    the model a second time, or words the reply differently.

    The reading says unclear; the reply is exactly the owner's sentence, one
    request is made, and the very same question is still held.
    """
    gateway = ScriptedGateway(reading("unclear"))
    tools = FakeTools()
    question = held()
    store = FakeStore(question=question)

    state = run(gateway, tools, store, message="hmm, maybe")

    assert state.reply == ("I didn't quite get that. Should I go ahead? Please reply yes or no.")
    assert state.reply == UNCLEAR_REPLY
    assert state.outcome is TurnOutcome.CONFIRM
    assert len(gateway.requests) == 1
    assert tools.calls == []
    assert store.cleared == 0
    assert store.question == question
    assert store.question is not None
    assert store.question.asked_at == START
    assert store.held == []


def test_a_yes_after_unclear_runs_the_held_change() -> None:
    """Catches an unclear that loses the held change, so the yes that follows
    runs nothing.

    Turn one reads unclear, turn two reads yes, on the same chat row: the
    stored call runs once.
    """
    store = FakeStore(question=held())
    tools = FakeTools({"create_access_request": FILED_PAYLOAD})

    run(ScriptedGateway(reading("unclear")), tools, store, message="hmm")
    assert tools.calls == []
    state = run(
        ScriptedGateway(reading("yes"), answer_body(FILED_REPLY)),
        tools,
        store,
        message="yes please",
    )

    assert tools.names == ["create_access_request"]
    assert tools.calls[0][1]["idempotency_key"] == STORED_KEY
    assert state.reply == FILED_REPLY
    assert store.question is None


@pytest.mark.parametrize("body", ["not json", '{"answer": "maybe"}'], ids=["not_json", "bad_value"])
def test_a_reading_with_a_bad_shape_twice_is_unclear(body: str) -> None:
    """Unhappy path. Catches a reading that fails the shape and then counts as
    a yes (or a no), that gives up with a failure, or that clears the question.

    Two bodies that do not fit the schema: the unclear reply, nothing runs, the
    question stays held.
    """
    gateway = ScriptedGateway(answer_body(body), answer_body(body))
    tools = FakeTools()
    question = held()
    store = FakeStore(question=question)

    state = run(gateway, tools, store, message="go ahead")

    assert state.reply == UNCLEAR_REPLY
    assert state.outcome is TurnOutcome.CONFIRM
    assert len(gateway.requests) == 2
    assert tools.calls == []
    assert store.question == question
    assert store.cleared == 0


def test_a_gateway_outage_while_reading_runs_nothing() -> None:
    """Unhappy path. Catches a reading outage that runs the held change
    anyway, that reads as unclear (it is a fault, not a person's answer), or
    that clears the question so the person must start over.

    The gateway answers 503 to the reading: the failure reply, outcome FAILED,
    no tool, and the question is kept.
    """
    gateway = RefusingGateway(OUTAGE)
    tools = FakeTools()
    question = held()
    store = FakeStore(question=question)

    state = run(gateway, tools, store, message="go ahead")

    assert state.reply == FAILURE_REPLY
    assert state.outcome is TurnOutcome.FAILED
    assert tools.calls == []
    assert store.question == question
    assert store.cleared == 0


def test_an_old_stored_question_without_the_shown_text_still_parses() -> None:
    """Catches a stored question that now requires `asked`, so every row saved
    before the change reads back as none and the held change is lost, or an
    `asked` that defaults to something other than empty.

    A stored row with no `asked` key validates and parses with `asked` empty;
    a row that has one keeps it.
    """
    raw = held_question().model_dump(mode="json")
    raw.pop("asked", None)

    assert OpenQuestion.model_validate(raw).asked == ""
    parsed = parse_open_question(raw, now=START)
    assert parsed is not None
    assert parsed.asked == ""
    raw["asked"] = ASKED
    kept = parse_open_question(raw, now=START)
    assert kept is not None
    assert kept.asked == ASKED


def test_the_word_lists_are_gone() -> None:
    """Catches a leftover word-list reading or language guess that could still
    decide a yes next to the model.

    `answer_words` cannot be imported, `confirmation` lost its reading and
    language lines, and `graph` has no language guess.
    """
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("helpdesk.agent_api.agent.answer_words")
    for name in ("read_answer", "FILE_LINES", "CHANGE_LINES"):
        assert not hasattr(confirmation, name), name
    assert "guess_language" not in inspect.getsource(graph)


@pytest.mark.parametrize("reason", ["oui", "nein"])
def test_a_reject_reason_is_kept_as_given(reason: str) -> None:
    """Catches a reject reason that is refused for being a yes or no word in
    some language, which blocks a real reason that happens to be one word.

    The arguments validate and the reason comes back unchanged.
    """
    args = RejectRequestArgs.model_validate(
        {"request_id": str(uuid.UUID(int=7)), "reason": reason, "idempotency_key": "key-0001"}
    )

    assert args.reason == reason


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("passt so", PICK_AGAIN_REPLY),
        ("si", PICK_AGAIN_REPLY),
        ("passt so ja", PICK_AGAIN_REPLY),
        ("passt so ja gut", FAILURE_REPLY),
    ],
    ids=["two_words", "one_word", "three_words_edge", "four_words_edge"],
)
def test_a_short_reply_with_nothing_held_and_a_failed_step_asks_which_one(
    message: str, expected: str
) -> None:
    """Catches a pick-again line chosen by a yes word list (these are in none)
    instead of by length, and a length cut at the wrong number.

    Nothing is held and the gateway is down: 3 words or fewer get the
    pick-again line, 4 get the plain failure line.
    """
    store = FakeStore(history=PICK_HISTORY)

    state = run(RefusingGateway(OUTAGE), FakeTools(), store, message=message)

    assert state.reply == expected
    assert state.outcome is TurnOutcome.FAILED


def test_the_reading_prompt_names_the_four_answers() -> None:
    """Catches a reading prompt that leaves out an answer, files "yes, but"
    with a change under yes, or never says the person's message is data (a
    message could then give the model orders).

    The prompt loads, names the four answers, ties "yes, but" to new_info, and
    says the message is data.
    """
    text = load_prompt("read_reply").text.lower()

    for answer in ("yes", "no", "new_info", "unclear"):
        assert answer in text, answer
    start = text.index("yes, but")
    assert "new_info" in text[start : start + 200]
    assert "data" in text
    assert [member.value for member in Reading] == ["yes", "no", "new_info", "unclear"]
    assert ReplyReading.model_fields["answer"].annotation is Reading
