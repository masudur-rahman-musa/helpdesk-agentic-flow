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
from pydantic import ValidationError

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
from helpdesk.agent_api.agent.state import ResolvedEntities, TurnOutcome, TurnState, ValidatedCall
from helpdesk.agent_api.agent.tool_args import (
    TOOL_INPUTS,
    CreateAccessRequestArgs,
    RejectRequestArgs,
)
from tests.agent_api.test_agent_loop import (
    FILED_PAYLOAD,
    FILED_REPLY,
    REQUEST_ID,
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
    assert sent[-1] == {"role": "user", "content": f"{graph.today_line(START)}\nnicht doch"}
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


# --- a yes that only repeats the cancel -----------------------------------------------

CANCEL_ASKED = "Shall I cancel your request REQ-0001?"
CANCEL_REPLY = "All set, that request is closed."


def held_cancel() -> OpenQuestion:
    """A held cancel from an earlier message, under a stored key."""
    arguments = TOOL_INPUTS["cancel_request"].model_validate(
        {"request_id": str(REQUEST_ID), "idempotency_key": STORED_KEY}
    )
    call = ValidatedCall(tool="cancel_request", arguments=arguments.model_dump(mode="json"))
    return held_question().model_copy(update={"calls": (call,), "asked": CANCEL_ASKED})


def test_yes_to_a_cancel_question_cancels() -> None:
    """Catches a held cancel that answers a "yes, please cancel REQ-0001" by
    asking the same question again, or that clears the question and runs
    nothing.

    The reading says yes: the cancel runs once under its stored key, the
    question is gone, nothing new is held, and the reply is not a question.
    """
    gateway = ScriptedGateway(reading("yes"), answer_body(CANCEL_REPLY))
    tools = FakeTools({"cancel_request": {"request_id": str(REQUEST_ID)}})
    store = FakeStore(question=held_cancel())

    state = run(gateway, tools, store, message="Yes, please cancel REQ-0001")

    assert tools.names == ["cancel_request"]
    assert tools.calls[0][1]["idempotency_key"] == STORED_KEY
    assert len(gateway.requests) == 2
    assert store.question is None
    assert store.held == []
    assert state.outcome is not TurnOutcome.CONFIRM
    assert state.reply == CANCEL_REPLY


def test_the_reading_prompt_counts_a_repeated_change_as_yes() -> None:
    """Catches a reading prompt that sends "yes, please cancel REQ-0001" to
    new_info because the message names the change, which asks the person the
    same question again.

    The prompt names that very wording and, around it, says a message that
    only repeats the proposed change is a yes.
    """
    text = load_prompt("read_reply").text.lower()

    start = text.index("please cancel req-0001")
    around = text[max(0, start - 300) : start + 300]
    assert "repeat" in around or "restat" in around
    assert "`yes`" in around


# --- a correction to a held request ----------------------------------------------------

OLD_REASON = "Obviously fake reason for a test."
NEW_REASON = "Obviously fake audit preparation."
VIEWER = "Internal Wiki viewer"
EDITOR = "Internal Wiki editor"


def reading_with(**fields: Any) -> dict[str, Any]:
    """One scripted gateway answer: new_info with the corrected values."""
    return answer_body(json.dumps({"answer": "new_info", **fields}))


def held_request() -> OpenQuestion:
    """The held filing: 30 days, the old reason, the viewer role by name."""
    return held_question().model_copy(
        update={"asked": ASKED, "resolved": ResolvedEntities(role_name=VIEWER)}
    )


def proposal(*, days: int = 30, reason: str = OLD_REASON) -> dict[str, Any]:
    """A model step that proposes the filing with the given days and reason."""
    return answer_body(
        None,
        wire_call(
            "call_new",
            "create_access_request",
            {
                "target_user_id": str(USER_ID),
                "role_id": str(WIKI_VIEWER_ID),
                "justification": reason,
                "requested_expiry_days": days,
            },
        ),
    )


def lookups_step() -> dict[str, Any]:
    """The model step that finds the person and the role again."""
    return answer_body(
        None,
        wire_call("call_me", "get_me", {}),
        wire_call("call_r", "find_role", {"words": "wiki viewer"}),
    )


def question_with(days: int, reason: str) -> str:
    """A written question that shows every fact of the filing."""
    return (
        f'Soll ich "{VIEWER}" für {days} Tage beantragen? Grund: {reason.rstrip(".")}. '
        "Bitte antworte mit Ja."
    )


def correct(
    gateway: ScriptedGateway, store: FakeStore | None = None, message: str = "90 days, not 30"
) -> tuple[TurnState, FakeStore]:
    """One correction turn over a held 30-day request."""
    kept = store or FakeStore(question=held_request())
    tools = FakeTools({"get_me": ME_PAYLOAD, "list_requestable_roles": ROLES_PAYLOAD})
    return run(gateway, tools, kept, message=message), kept


def writer_facts(gateway: ScriptedGateway) -> list[str]:
    """The facts the last writing call was given."""
    facts: list[str] = json.loads(gateway.requests[-1]["messages"][-1]["content"])["facts"]
    return facts


def test_a_new_length_updates_the_held_request() -> None:
    """Catches a correction that asks the old 30 again because the model
    proposed the old length, or one that files 30 after the person said 90.

    The reading gives 90 days; the model still proposes 30. The writer is
    given 90 and not 30, the question the person sees says 90, and the stored
    call says 90.
    """
    gateway = ScriptedGateway(
        reading_with(days=90),
        lookups_step(),
        proposal(days=30),
        answer_body(question_with(90, OLD_REASON)),
    )

    state, store = correct(gateway)

    assert state.outcome is TurnOutcome.CONFIRM
    assert state.reply == question_with(90, OLD_REASON)
    facts = writer_facts(gateway)
    assert "90" in facts
    assert "30" not in facts
    assert store.held[0].calls[0].arguments["requested_expiry_days"] == 90
    assert store.held[0].asked == state.reply


def test_a_new_reason_updates_the_held_request() -> None:
    """Catches a correction that keeps the old reason because the model
    proposed it again.

    The reading gives a new reason; the model still proposes the old one. The
    writer is given the new reason only, and the stored call carries it.
    """
    gateway = ScriptedGateway(
        reading_with(reason=NEW_REASON),
        lookups_step(),
        proposal(reason=OLD_REASON),
        answer_body(question_with(30, NEW_REASON)),
    )

    state, store = correct(gateway, message="the reason is audit preparation")

    assert state.outcome is TurnOutcome.CONFIRM
    facts = writer_facts(gateway)
    assert NEW_REASON.rstrip(".") in facts
    assert OLD_REASON.rstrip(".") not in facts
    assert store.held[0].calls[0].arguments["justification"] == NEW_REASON


def test_a_corrected_reason_leaves_the_days_the_model_proposed() -> None:
    """Catches a correction that forces every field of the held request, so a
    new reason would also put the old 30 days back over the 10 the model
    proposed.

    Only the reason is corrected; the model proposes 10 days; 10 is stored.
    """
    gateway = ScriptedGateway(
        reading_with(reason=NEW_REASON),
        lookups_step(),
        proposal(days=10, reason=OLD_REASON),
        answer_body(question_with(10, NEW_REASON)),
    )

    _state, store = correct(gateway, message="the reason is audit preparation")

    assert store.held[0].calls[0].arguments["requested_expiry_days"] == 10
    assert store.held[0].calls[0].arguments["justification"] == NEW_REASON


def test_the_old_value_is_never_asked_back() -> None:
    """Unhappy path. Catches a hold that shows a question with the old 30 after
    the person said 90, because the facts check ran on the old call.

    The first written question says 30: it is sent back naming 90, and the
    second question (90) is shown and stored.
    """
    gateway = ScriptedGateway(
        reading_with(days=90),
        lookups_step(),
        proposal(days=30),
        answer_body(question_with(30, OLD_REASON)),
        answer_body(question_with(90, OLD_REASON)),
    )

    state, store = correct(gateway)

    assert len(gateway.requests) == 5
    assert "90" in gateway.requests[4]["messages"][-1]["content"]
    assert state.reply == question_with(90, OLD_REASON)
    assert store.held[0].asked == state.reply
    assert store.held[0].calls[0].arguments["requested_expiry_days"] == 90


def test_two_questions_with_the_old_value_fall_back_to_the_new_one() -> None:
    """Unhappy path. Catches a fallback built from the old call, which would
    show 30 days after the person said 90.

    Both written questions say 30: the code-written line is shown, and it says
    90 days and never 30.
    """
    gateway = ScriptedGateway(
        reading_with(days=90),
        lookups_step(),
        proposal(days=30),
        answer_body(question_with(30, OLD_REASON)),
        answer_body(question_with(30, OLD_REASON)),
    )

    state, store = correct(gateway)

    assert state.reply is not None
    assert state.reply.startswith("I'll file this:")
    assert "90 days" in state.reply
    assert "30" not in state.reply
    assert store.held[0].asked == state.reply
    assert store.held[0].calls[0].arguments["requested_expiry_days"] == 90


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"days": 90}, (90, OLD_REASON, VIEWER)),
        ({"reason": NEW_REASON}, (30, NEW_REASON, VIEWER)),
        ({"role": EDITOR}, (30, OLD_REASON, EDITOR)),
        ({"days": 90, "reason": NEW_REASON, "role": EDITOR}, (90, NEW_REASON, EDITOR)),
    ],
    ids=["days", "reason", "role", "all_three"],
)
def test_the_draft_saved_after_a_correction_holds_the_held_values_and_the_new_ones(
    fields: dict[str, Any], expected: tuple[int, str, str]
) -> None:
    """Catches a draft saved from the correction alone (the held request's other
    values lost), one never seeded from the held call, or one that drops a
    corrected value.

    The model only answers in words after the reading, so the draft is what
    the turn saved: the held days, reason and role, with the corrected ones
    laid over them.
    """
    gateway = ScriptedGateway(reading_with(**fields), answer_body("Okay, noted."))

    _state, store = correct(gateway)

    assert store.draft is not None
    assert (store.draft.days, store.draft.reason, store.draft.role) == expected


def test_a_correction_to_a_held_cancel_saves_no_draft() -> None:
    """Catches a draft seeded from a held call that is not a filing, which
    would invent a request the person never started.

    A held cancel gets a new_info reading with days: no draft is saved.
    """
    gateway = ScriptedGateway(reading_with(days=90), answer_body("Okay, noted."))
    store = FakeStore(question=held_cancel())

    correct(gateway, store)

    assert store.draft is None


def test_values_in_a_reading_that_is_not_new_info_save_no_draft() -> None:
    """Catches a draft saved from a no (or a yes) that happens to carry days.

    The reading says no with days 90: the change is dropped and no draft
    exists afterwards.
    """
    gateway = ScriptedGateway(
        answer_body(json.dumps({"answer": "no", "days": 90})), answer_body("Okay, dropped.")
    )

    _state, store = correct(gateway)

    assert store.draft is None


def test_the_reading_may_carry_corrected_values() -> None:
    """Catches a reading model that cannot carry days, reason or role (the
    correction would be dropped before the graph sees it), one that requires
    them, or one that lets another field through.

    Each defaults to none, a full reading validates, an unknown field is
    refused, and the schema sent to the gateway lists all three.
    """
    plain = ReplyReading.model_validate({"answer": "yes"})
    assert (plain.days, plain.reason, plain.role) == (None, None, None)
    full = ReplyReading.model_validate(
        {"answer": "new_info", "days": 90, "reason": NEW_REASON, "role": EDITOR}
    )
    assert (full.days, full.reason, full.role) == (90, NEW_REASON, EDITOR)
    with pytest.raises(ValidationError):
        ReplyReading.model_validate({"answer": "yes", "who": "Dana"})
    assert ReplyReading.model_config["frozen"] is True

    gateway = ScriptedGateway(reading("yes"), answer_body(FILED_REPLY))
    run(
        gateway,
        FakeTools({"create_access_request": FILED_PAYLOAD}),
        FakeStore(question=held()),
        message="go ahead",
    )
    schema = gateway.requests[0]["response_format"]["json_schema"]["schema"]
    assert {"answer", "days", "reason", "role"} <= set(schema["properties"])
