"""The model writes the confirm question; code makes sure the facts are in it.

Cost and isolation. Pure: the real ``ModelClient`` over ``httpx.MockTransport``,
the fakes of ``test_agent_loop.py`` for the turns, and plain objects for the
fact rules. No service, no ``db`` marker, no socket, no waiting.

Expected implementation surface: ``confirmation.Fact``,
``confirmation.confirm_facts``, ``confirmation.missing_facts``, the
``write_confirm`` prompt, and the writing call in ``graph``.
"""

import json
import uuid
from typing import Any

import pytest

from helpdesk.agent_api.agent import confirmation
from helpdesk.agent_api.agent.confirmation import (
    Fact,
    confirm_facts,
    missing_facts,
)
from helpdesk.agent_api.agent.loop_tools import PersonRow
from helpdesk.agent_api.agent.prompts import load_prompt
from helpdesk.agent_api.agent.state import ResolvedEntities, ToolResult, TurnOutcome, ValidatedCall
from tests.agent_api.test_agent_loop import (
    WIKI_VIEWER_ID,
    FakeStore,
    FakeTools,
    ScriptedGateway,
    answer_body,
    role_row,
    run,
    wire_call,
)
from tests.agent_api.test_budget_refusal import RefusingGateway

REASON = "Obviously fake reason: audit preparation."
DANA = uuid.UUID(int=201)
ROLE = "Internal Wiki viewer"
PERSON = "Dana Fake"
MESSAGE = "File wiki viewer access for Dana."

GOOD = (
    f'Soll ich "{ROLE}" für {PERSON} für 30 Tage beantragen? Grund: {REASON} Bitte antworte mit Ja.'
)
NO_DAYS = f'Soll ich "{ROLE}" für {PERSON} beantragen? Grund: {REASON} Bitte antworte mit Ja.'
NO_REASON = f'Soll ich "{ROLE}" für {PERSON} für 30 Tage beantragen? Bitte antworte mit Ja.'
CLAIMS_GRANT = f"{GOOD} Der Zugang ist granted."

SUMMARY_START = f'I\'ll file this: {ROLE} for {PERSON}, 30 days, reason "{REASON}".'


def lookups() -> list[dict[str, Any]]:
    """The two model steps before the hold: the lookups, then the filing call."""
    return [
        answer_body(
            None,
            wire_call("call_r", "find_role", {"words": "wiki viewer"}),
            wire_call("call_p", "find_person", {"words": "dana"}),
        ),
        answer_body(
            None,
            wire_call(
                "call_c",
                "create_access_request",
                {
                    "target_user_id": str(DANA),
                    "role_id": str(WIKI_VIEWER_ID),
                    "justification": REASON,
                    "requested_expiry_days": 30,
                },
            ),
        ),
    ]


def manager_store() -> FakeStore:
    """A manager whose directory knows Dana."""
    return FakeStore(
        principals=["manager"],
        people=[PersonRow(id=DANA, display_name=PERSON, department="Sales")],
    )


def manager_tools() -> FakeTools:
    """The tool server for the lookups above."""
    return FakeTools(
        {"list_requestable_roles": {"roles": [role_row("wiki", "Internal Wiki", "viewer")]}}
    )


def written(*questions: str) -> ScriptedGateway:
    """A gateway playing the two lookup steps, then one answer per question."""
    return ScriptedGateway(*lookups(), *(answer_body(question) for question in questions))


# --- the turn: the model writes, code checks ------------------------------------------


def test_a_question_with_every_fact_is_shown_as_written() -> None:
    """Catches a hold that still shows the code-written English summary, that
    edits or wraps the model's text, that ends on another outcome, or that
    stores the English summary as `asked` (the reading of the reply would then
    see a question the person never saw).

    The German question names the role, the days, the reason and the person:
    it is the reply, byte for byte, the outcome is CONFIRM and it is `asked`.
    """
    gateway = written(GOOD)
    store = manager_store()

    state = run(gateway, manager_tools(), store, message=MESSAGE)

    assert state.reply == GOOD
    assert state.outcome is TurnOutcome.CONFIRM
    assert len(gateway.requests) == 3
    assert len(store.held) == 1
    assert store.held[0].asked == GOOD


def test_the_writing_call_carries_the_facts_and_the_persons_words() -> None:
    """Catches a writing call on another alias, one that offers tools (the
    model could propose a second change while writing), one that leaves out the
    English summary, the facts or the person's latest words, or one that sends
    the loop prompt's version instead of the writing prompt's.

    The third request is checked field by field against the wire shape.
    """
    gateway = written(GOOD)

    run(gateway, manager_tools(), manager_store(), message=MESSAGE)

    request = gateway.requests[2]
    prompt = load_prompt("write_confirm")
    assert request["model"] == "agent-default"
    assert "tools" not in request
    assert "tool_choice" not in request
    assert request["messages"][0] == {"role": "system", "content": prompt.text}
    last = request["messages"][-1]
    assert last["role"] == "user"
    sent = json.loads(last["content"])
    assert set(sent) == {"summary", "facts", "message", "first_message"}
    assert sent["summary"].startswith(SUMMARY_START)
    assert sent["message"] == MESSAGE
    assert sorted(sent["facts"]) == sorted([ROLE, "30", REASON.rstrip("."), PERSON])
    assert gateway.headers[2].get("X-Prompt-Version") == prompt.sha256


def test_a_missing_fact_goes_back_once_with_its_name() -> None:
    """Catches a hold that shows a question missing the days, that retries
    without saying what is missing, that names facts which were present, or that
    shows the first question anyway.

    The first question lacks the days: the second request keeps the first
    one's messages, ends on a user message naming "30", and the second
    question (all facts) is shown and stored.
    """
    gateway = written(NO_DAYS, GOOD)
    store = manager_store()

    state = run(gateway, manager_tools(), store, message=MESSAGE)

    first, second = gateway.requests[2], gateway.requests[3]
    assert second["messages"][: len(first["messages"])] == first["messages"]
    assert len(second["messages"]) > len(first["messages"])
    assert second["messages"][-1]["role"] == "user"
    assert "30" in second["messages"][-1]["content"]
    assert "tools" not in second
    assert len(gateway.requests) == 4
    assert state.reply == GOOD
    assert state.outcome is TurnOutcome.CONFIRM
    assert store.held[0].asked == GOOD


def test_two_misses_show_the_english_summary() -> None:
    """Unhappy path. Catches a hold that shows a question without the reason
    after the retry, that retries a second time, or that ends on another
    outcome than CONFIRM.

    Both questions lack the reason (the role, days and person are there, so
    only the reason decides): the English summary is shown and stored, the
    outcome is CONFIRM and exactly two writing requests were made.
    """
    gateway = written(NO_REASON, NO_REASON)
    store = manager_store()

    state = run(gateway, manager_tools(), store, message=MESSAGE)

    assert len(gateway.requests) == 4
    assert state.reply is not None
    assert state.reply.startswith(SUMMARY_START)
    assert state.outcome is TurnOutcome.CONFIRM
    assert len(store.held) == 1
    assert store.held[0].asked == state.reply


def test_a_question_claiming_a_grant_is_sent_back() -> None:
    """Unhappy path. Catches a written question that skips the reply check, so
    "granted" reaches the person with no tool result behind it.

    The first question has every fact but says "granted": the retry names the
    word, and the clean second question is shown.
    """
    gateway = written(CLAIMS_GRANT, GOOD)

    state = run(gateway, manager_tools(), manager_store(), message=MESSAGE)

    assert len(gateway.requests) == 4
    assert "granted" in gateway.requests[3]["messages"][-1]["content"]
    assert state.reply == GOOD
    assert state.outcome is TurnOutcome.CONFIRM


def test_a_gateway_outage_while_writing_shows_the_english_summary() -> None:
    """Unhappy path. Catches a hold that fails the turn (or loses the held
    change) because the writing call is down.

    The lookups work, the writing call answers 503: the English summary, the
    outcome CONFIRM, and the change is held.
    """
    steps = [(200, body) for body in lookups()]
    gateway = RefusingGateway(*steps, (503, {"error": "down"}))
    store = manager_store()

    state = run(gateway, manager_tools(), store, message=MESSAGE)

    assert state.reply is not None
    assert state.reply.startswith(SUMMARY_START)
    assert state.outcome is TurnOutcome.CONFIRM
    assert len(gateway.requests) == 3
    assert len(store.held) == 1
    assert store.held[0].asked == state.reply


# --- the facts of each change ---------------------------------------------------------


def call(tool: str, **arguments: object) -> ValidatedCall:
    """One held call."""
    return ValidatedCall(tool=tool, arguments=dict(arguments))


def texts(facts: tuple[Fact, ...]) -> set[str]:
    """The fact texts, for set comparison."""
    return {fact.text for fact in facts}


def test_the_facts_of_each_change() -> None:
    """Catches a fact list that leaves out a field the person must see before a
    yes (the role, the days, the reason, the person, the request numbers, the
    access name, the dates, the title, the period), that marks a non-number as a
    number, or that asks for facts on a change that has none.
    """
    create = confirm_facts(
        [
            call(
                "create_access_request",
                justification=REASON,
                requested_expiry_days=30,
            )
        ],
        [],
        ResolvedEntities(role_name=ROLE),
        PERSON,
    )
    assert set(create) == {
        Fact(ROLE),
        Fact("30", number=True),
        Fact(REASON.rstrip(".")),
        Fact(PERSON),
    }

    listing = ToolResult(
        tool="list_pending_approvals",
        ok=True,
        payload={
            "approvals": [
                {
                    "request_id": str(uuid.UUID(int=7)),
                    "request_number": "REQ-0006",
                    "system_name": "Data Warehouse",
                    "role_slug": "engineer",
                },
                {
                    "request_id": str(uuid.UUID(int=8)),
                    "request_number": "REQ-0019",
                    "system_name": "CRM",
                    "role_slug": "viewer",
                },
            ]
        },
    )
    approve = confirm_facts(
        [
            call("approve_request", request_id=str(uuid.UUID(int=7))),
            call("approve_request", request_id=str(uuid.UUID(int=8))),
        ],
        [listing],
        None,
    )
    assert texts(approve) == {"REQ-0006", "REQ-0019"}

    access = ToolResult(
        tool="list_my_access",
        ok=True,
        payload={
            "grants": [
                {"grant_id": str(uuid.UUID(int=9)), "system_name": "CRM", "role_slug": "viewer"}
            ]
        },
    )
    revoke = confirm_facts([call("revoke_access", grant_id=str(uuid.UUID(int=9)))], [access], None)
    assert texts(revoke) == {"CRM viewer"}

    onboard = confirm_facts(
        [
            call(
                "onboard_user",
                display_name="Kai Fake",
                email="kai.fake@example.invalid",
                start_date="2026-11-09",
            )
        ],
        [],
        None,
    )
    assert texts(onboard) == {"Kai Fake", "kai.fake@example.invalid", "2026-11-09"}

    transfer = confirm_facts([call("transfer_user", effective_date="2026-12-01")], [], None)
    assert texts(transfer) == {"2026-12-01"}
    offboard = confirm_facts([call("offboard_user", end_date="2026-12-31")], [], None)
    assert texts(offboard) == {"2026-12-31"}
    ticket = confirm_facts([call("create_ticket", title="Printer is down")], [], None)
    assert texts(ticket) == {"Printer is down"}
    review = confirm_facts([call("open_access_review", period="2026-Q4")], [], None)
    assert texts(review) == {"2026-Q4"}

    assert not any(fact.number for fact in (*approve, *revoke, *onboard, *transfer, *review))


@pytest.mark.parametrize("tool", ["reset_own_password", "unlock_own_account", "decide_review_item"])
def test_a_change_with_nothing_to_name_has_no_facts(tool: str) -> None:
    """Catches a fact list that invents a fact for a change that has none, which
    would send every such question back and then to the English summary.
    """
    assert confirm_facts([call(tool)], [], None) == ()


def test_a_decision_on_a_request_this_turn_did_not_list_has_no_number_to_check() -> None:
    """Catches a fact list that asks for a request number it never saw (the
    question could not have it, so it would always fall back to English).
    """
    assert (
        confirm_facts([call("approve_request", request_id=str(uuid.UUID(int=7)))], [], None) == ()
    )


def test_a_revoke_not_listed_falls_back_to_the_system_name() -> None:
    """Catches a revoke with no listed access that names nothing, so a question
    could leave out which access goes.
    """
    facts = confirm_facts(
        [call("revoke_access", grant_id=str(uuid.UUID(int=9)))],
        [],
        ResolvedEntities(system_name="CRM"),
    )

    assert texts(facts) == {"CRM"}


def test_the_classified_days_win_over_the_asked_days() -> None:
    """Catches a days fact taken from the request when the policy shortened it:
    the person would be asked about 30 days and get 7.
    """
    verdict = ToolResult(
        tool="classify_access_request",
        ok=True,
        payload={"decision": "approval", "required_approvers": [], "effective_expiry_days": 7},
    )

    facts = confirm_facts(
        [call("create_access_request", justification=REASON, requested_expiry_days=30)],
        [verdict],
        ResolvedEntities(role_name=ROLE),
    )

    assert Fact("7", number=True) in facts
    assert Fact("30", number=True) not in facts
    assert Fact(PERSON) not in facts


def test_unknown_days_are_not_a_fact() -> None:
    """Catches a days fact built from nothing (a made-up "usual length" the
    question could never contain), which would always fall back to English.

    No classification and no asked days: the other facts are there and none is
    a number.
    """
    facts = confirm_facts(
        [call("create_access_request", justification=REASON)],
        [],
        ResolvedEntities(role_name=ROLE),
    )

    assert texts(facts) == {ROLE, REASON.rstrip(".")}
    assert not any(fact.number for fact in facts)


# --- finding a fact in a question ------------------------------------------------------


@pytest.mark.parametrize(
    ("fact", "question", "found"),
    [
        (Fact("Kristian Mühle"), "kristian muhle bekommt Zugang", True),
        (Fact("Kristian Muhle"), "KRISTIAN MÜHLE bekommt Zugang", True),
        (Fact("Data Warehouse"), "Data-Ware", False),
        (Fact("Data Warehouse"), "Zugang", False),
        (Fact("30", number=True), "für 30 Tage", True),
        (Fact("30", number=True), "pour 30 jours", True),
        (Fact("30", number=True), "30", True),
        (Fact("30", number=True), "Dauer: 30.", True),
        (Fact("30", number=True), "für 130 Tage", False),
        (Fact("30", number=True), "für 300 Tage", False),
        (Fact("30", number=True), "für 3 Tage", False),
    ],
    ids=[
        "accents_in_question",
        "accents_in_fact",
        "cut_name",
        "absent",
        "days_german",
        "days_french",
        "days_alone",
        "days_before_a_dot",
        "digits_inside_130",
        "digits_inside_300",
        "other_number",
    ],
)
def test_a_fact_matches_without_case_or_accents(fact: Fact, question: str, found: bool) -> None:
    """Catches a name match that is case or accent sensitive, a name found in a
    cut-off spelling, and a days number found inside a longer number (130 or
    300 would then pass for 30).
    """
    assert fact.found_in(question) is found


def test_missing_facts_names_only_the_facts_not_found() -> None:
    """Catches a missing list that names facts that are there, or drops one that
    is not.
    """
    facts = [Fact(ROLE), Fact("30", number=True), Fact(REASON.rstrip(".")), Fact(PERSON)]

    assert missing_facts(NO_DAYS, facts) == ["30"]
    assert missing_facts(NO_REASON, facts) == [REASON.rstrip(".")]
    assert missing_facts(GOOD, facts) == []
    assert missing_facts("Ja?", facts) == [ROLE, "30", REASON.rstrip("."), PERSON]


# --- the prompt ------------------------------------------------------------------------


def test_the_writing_prompt_says_copy_facts_exactly() -> None:
    """Catches a prompt that leaves the language to the model's habit, lets it
    reword names, numbers, dates or the reason (the facts check would then fail
    every time), forgets to end on a request for a yes, or never says the
    summary and the message are data.
    """
    text = load_prompt("write_confirm").text.lower()

    for word in ("language", "exactly", "name", "number", "date", "reason", "yes", "data"):
        assert word in text, word


def test_the_writing_prompt_follows_the_first_message_language() -> None:
    """Catches a writing prompt that still follows the latest message (one
    German word later in an English chat would flip the language), that has no
    word for a first message which shows no language, or that lets the model
    switch on its own.

    The rule is read around the phrase "first message": it must name English
    and the greeting case there, and say never to switch.
    """
    text = load_prompt("write_confirm").text.lower()

    start = text.index("first message")
    around = text[start : start + 400]
    assert "english" in around
    assert "greeting" in around
    assert "never switch" in text


# --- the language the writer is told about -------------------------------------------


def _writer_input(gateway: ScriptedGateway) -> dict[str, Any]:
    """The JSON the writing call was given."""
    parsed: dict[str, Any] = json.loads(gateway.requests[2]["messages"][-1]["content"])
    return parsed


def test_the_question_follows_the_first_message_language() -> None:
    """Catches a writing call that sends only the latest message (one German
    word after an English opening would turn the question German), or one that
    sends the latest message under the first-message key.

    An English opening, then a German latest message: the writer is given the
    English opening as `first_message` next to the German `message`.
    """
    history = [("user", MESSAGE), ("assistant", "Which role do you mean?")]
    later = "Bitte die Wiki-Viewer-Rolle."
    gateway = written(GOOD)
    store = FakeStore(
        principals=["manager"],
        people=[PersonRow(id=DANA, display_name=PERSON, department="Sales")],
        history=history,
    )

    run(gateway, manager_tools(), store, message=later)

    sent = _writer_input(gateway)
    assert set(sent) == {"summary", "facts", "message", "first_message"}
    assert sent["first_message"] == MESSAGE
    assert sent["message"] == later


def test_a_greeting_alone_means_english() -> None:
    """Catches a writing call that sends the latest message as the language
    sample when the chat opened with a greeting, and a prompt that leaves a
    greeting-only opening to the model's guess.

    "hi" opens the chat and a German message follows: the writer is given "hi"
    as the first message, and the prompt says such an opening means English.
    """
    history = [("user", "hi"), ("assistant", "Hello! How can I help?")]
    gateway = written(GOOD)
    store = FakeStore(
        principals=["manager"],
        people=[PersonRow(id=DANA, display_name=PERSON, department="Sales")],
        history=history,
    )

    run(gateway, manager_tools(), store, message="Ich brauche Wiki-Zugang für Dana.")

    assert _writer_input(gateway)["first_message"] == "hi"
    text = load_prompt("write_confirm").text.lower()
    start = text.index("greeting")
    assert "english" in text[start : start + 200]


def test_the_first_message_is_the_message_when_the_chat_just_started() -> None:
    """Catches a first-message key that is empty or missing on the first turn
    of a chat, where no earlier message exists.

    No history: the first message and the latest message are the same text.
    """
    gateway = written(GOOD)

    run(gateway, manager_tools(), manager_store(), message=MESSAGE)

    sent = _writer_input(gateway)
    assert sent["first_message"] == MESSAGE
    assert sent["message"] == MESSAGE


# --- a question holds only the question ----------------------------------------------

ONE_QUESTION = "Shall I file this for 30 days? Please answer yes."


def test_a_clean_question_has_no_problems() -> None:
    """Catches a guard that refuses an ordinary question (one question mark, no
    self-correction), which would send every question back and then to the
    fixed line.
    """
    assert confirmation.question_problems(ONE_QUESTION) == []
    assert confirmation.question_problems(GOOD) == []
    assert confirmation.question_problems("Shall I file this for 30 days, yes or no?") == []


@pytest.mark.parametrize(
    "question",
    [
        "Shall I file this? Is that right?",
        "Shall I file this?? Please answer yes.",
        "Shall I file this? Really? Please answer yes.",
    ],
    ids=["two_questions", "doubled_mark", "three_marks"],
)
def test_more_than_one_question_mark_is_a_problem(question: str) -> None:
    """Catches a guard that allows a second question in the confirm message,
    where the person would not know which one to answer.

    None of these holds a self-correction word, so only the count can refuse
    them.
    """
    assert confirmation.question_problems(question)


@pytest.mark.parametrize(
    "marker",
    ["hmm", "wait", "oops", "scratch that", "i mean", "let me rephrase", "correction", "sorry"],
)
def test_a_self_correction_marker_is_a_problem(marker: str) -> None:
    """Catches a guard that misses one self-correction marker, or one that
    matches only in lower case (a model writes "Oops" and "WAIT" too).

    Each row has exactly one question mark and one marker, so only the marker
    can refuse it: written lower, capitalised and upper case.
    """
    for shown in (marker, marker.capitalize(), marker.upper()):
        question = f"Shall I file this for 30 days? {shown}, please answer yes."
        assert confirmation.question_problems(question), shown


def test_a_self_correction_marker_inside_a_sentence_is_a_problem() -> None:
    """Catches a guard that matches only a marker at the start of the text.

    The marker sits in the middle and the question mark count is one.
    """
    question = "Shall I file this for 30 days, oops, I mean for 90 days? Please answer yes."

    assert confirmation.question_problems(question)


BAD_SELF = f"{GOOD} Hmm, wait, scratch that."
BAD_TWO = f"{GOOD} Ist das richtig? Wirklich?"
ALL_FACTS = [Fact(ROLE), Fact("30", number=True), Fact(REASON.rstrip(".")), Fact(PERSON)]


@pytest.mark.parametrize("bad", [BAD_SELF, BAD_TWO], ids=["self_correction", "two_questions"])
def test_a_question_with_a_self_correction_is_written_again(bad: str) -> None:
    """Catches a hold that shows a question talking to itself or asking twice
    because every fact was in it (the fact check alone lets it through), or one
    that retries more than once.

    Both bad answers carry every fact, so only the question-only guard can
    refuse them. The second fake answer is shown and stored.
    """
    assert missing_facts(bad, ALL_FACTS) == []
    gateway = written(bad, GOOD)
    store = manager_store()

    state = run(gateway, manager_tools(), store, message=MESSAGE)

    first, second = gateway.requests[2], gateway.requests[3]
    assert second["messages"][: len(first["messages"])] == first["messages"]
    assert second["messages"][-1]["role"] == "user"
    assert "tools" not in second
    assert len(gateway.requests) == 4
    assert state.reply == GOOD
    assert state.outcome is TurnOutcome.CONFIRM
    assert store.held[0].asked == GOOD


def test_two_bad_questions_fall_back_to_the_fixed_line() -> None:
    """Unhappy path. Catches a hold that shows the second bad question, that
    asks the model a third time, or that ends on another outcome.

    Both answers carry every fact but are not only a question: the fixed
    English line is shown and stored, after exactly two writing requests.
    """
    gateway = written(BAD_SELF, BAD_TWO)
    store = manager_store()

    state = run(gateway, manager_tools(), store, message=MESSAGE)

    assert len(gateway.requests) == 4
    assert state.reply is not None
    assert state.reply.startswith(SUMMARY_START)
    assert state.outcome is TurnOutcome.CONFIRM
    assert len(store.held) == 1
    assert store.held[0].asked == state.reply
