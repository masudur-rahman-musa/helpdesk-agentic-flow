"""Follow-up messages, the cannot-place reply and the fixed sentences' empty Sources.

Surface the implementer must provide (names fixed by an earlier change):
`helpdesk.agent_api.agent.follow_up` with `CANNOT_PLACE_REPLY` and
`last_assistant_reply`; `Intent.FOLLOW_UP`; `postcheck.permitted_words` that adds
the last assistant reply's decision words on a follow-up turn only; a
`compose_reply` fallback of `CANNOT_PLACE_REPLY` when no tool ran, and
`reply_citations == ()` on every fallback.

Cost and isolation. No Postgres and no socket. Compose-level cases reuse the
`RecordingSession` and `StagedGateway` of `test_agent_compose.py`; whole-turn
cases use `ChatDb` and `Tools` of `test_draft_turns.py` (a chat row in memory).
Each test builds its own objects, so nothing is shared.
"""

import asyncio
import json
from typing import Any, cast

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.agent_api.agent import gates
from helpdesk.agent_api.agent.clarify_cap import BEST_READING_FALLBACK, CORRECT_ME_LINE
from helpdesk.agent_api.agent.events import ReplyEvent, TurnEvent
from helpdesk.agent_api.agent.follow_up import CANNOT_PLACE_REPLY, last_assistant_reply
from helpdesk.agent_api.agent.graph import TurnDeps, stream_turn
from helpdesk.agent_api.agent.postcheck import (
    POSTCHECK_FALLBACK_REPLY,
    contradicts,
    outcome_reply,
    permitted_words,
    words_in,
)
from helpdesk.agent_api.agent.state import (
    Intent,
    IntentEntities,
    IntentExtraction,
    PostCheckOutcome,
    TurnOutcome,
    TurnState,
)
from helpdesk.agent_api.agent.tool_args import SEARCH_POLICY
from helpdesk.agent_api.services.model_gateway.client import ModelClient
from helpdesk.shared.models.conversation import MAX_MESSAGE_CHARS, MessageRole
from tests.agent_api.test_agent_compose import (
    CONTRADICTING_REPLY,
    SECOND_CONTRADICTING_REPLY,
    RecordingSession,
    StagedAnswer,
    StagedGateway,
    _citation,
    _citation_two,
    _compose,
    _messages,
    _pending_result,
    _state,
    _with_marks,
)
from tests.agent_api.test_draft_turns import (
    API_KEYS,
    EMPTY_PLAN,
    GATEWAY_URL,
    NOW,
    SESSION_ID,
    TENANT_ID,
    TENANT_SLUG,
    USER_ID,
    WIKI_REASON,
    ChatDb,
    Tools,
    extraction_json,
    gateway_of,
    play,
    search_page,
    wiki_db,
)

EARLIER_PENDING = "Your request is pending with your manager."
LABEL = "the earlier reply to reshape or explain"


def _follow_up_state(
    *history: str, intent: Intent = Intent.FOLLOW_UP, user_message: str = "say it shorter"
) -> TurnState:
    """A turn that stands where `compose_reply` starts: no tool ran, the history is given."""
    extraction = IntentExtraction(
        intent=intent,
        entities=IntentEntities(**dict.fromkeys(IntentEntities.model_fields)),
        confidence=0.9,
        clarifying_question=None,
        second_request=None,
    )
    return _state().model_copy(
        update={"extraction": extraction, "history": history, "user_message": user_message}
    )


# --- the intent tables --------------------------------------------------------------


def test_a_follow_up_needs_nothing_plans_nothing_and_searches_nothing() -> None:
    """Catches a `follow_up` row that quietly allows a tool, a policy search or a
    required entity: any of them would make "say it shorter" run the whole
    request flow again."""
    assert gates.INTENT_TOOLS[Intent.FOLLOW_UP] == frozenset()
    assert gates.RETRIEVES_POLICY[Intent.FOLLOW_UP] is False
    assert gates.REQUIRED_ENTITIES[Intent.FOLLOW_UP] == frozenset()
    assert gates.EXTRACTION_ENTITIES[Intent.FOLLOW_UP] == frozenset()


# --- reading the last assistant reply ------------------------------------------------


@pytest.mark.parametrize(
    ("history", "expected"),
    [
        pytest.param(
            ("user: hi", "assistant: first", "user: and?", "assistant: second"),
            "second",
            id="the last of two",
        ),
        pytest.param(("user: hi", "user: anyone?"), None, id="only the person spoke"),
        pytest.param((), None, id="an empty history"),
        pytest.param(
            ("assistant: line one\nline two", "user: ok"),
            "line one\nline two",
            id="a reply of two lines is whole",
        ),
        pytest.param(
            ("assistant: the real reply", "user: assistant: not a reply"),
            "the real reply",
            id="a person typing the word assistant is not the assistant",
        ),
    ],
)
def test_the_last_assistant_reply_is_read_from_the_history(
    history: tuple[str, ...], expected: str | None
) -> None:
    """Catches a reader that returns the first assistant line, splits a reply at
    its first line, or matches the word "assistant" anywhere in a line: the
    writer would reshape the wrong text, or half of it, or something the person
    typed."""
    assert last_assistant_reply(history) == expected


# --- the writer gets the earlier reply as data --------------------------------------


def _stream_names(gateway: StagedGateway) -> list[str]:
    """The schema name each model request asked for, in order."""
    return [
        json.loads(request.content)["response_format"]["json_schema"]["name"]
        for request in gateway.requests
    ]


def test_a_follow_up_runs_no_tools_and_no_search() -> None:
    """Catches a follow-up forced through an action label: `search_policy` or a
    planner call would run again for "say it as a list". The turn makes zero tool
    calls (a tool nobody scripted fails the test), asks the model exactly twice
    (the intent, then the writer, both on the default alias), and hands the
    writer the LAST assistant reply as a labelled data message, never the older
    reply."""
    db = wiki_db()
    older = "Wiki access needs your manager to say yes first."
    latest = "Wiki access lasts 30 days unless you ask for less."
    db.plant(MessageRole.USER, "Can I have wiki access?")
    db.plant(MessageRole.ASSISTANT, older)
    db.plant(MessageRole.USER, "For how long?")
    db.plant(MessageRole.ASSISTANT, latest)
    tools = Tools()
    reshaped = "- Wiki access lasts 30 days\n- You can ask for less"
    gateway = gateway_of(extraction_json("follow_up", confidence=0.9), reshaped)

    state = play(db, "say it as a list", gateway, tools)

    assert tools.calls == []
    assert _stream_names(gateway) == ["IntentExtraction", "ComposedReply"]
    assert {json.loads(r.content)["model"] for r in gateway.requests} == {"agent-default"}
    writer = gateway.requests[1]
    data = [m for m in _messages(writer) if m["role"] == "user" and m["content"].startswith(LABEL)]
    assert len(data) == 1
    assert data[0]["content"].startswith(f"{LABEL}:\n")
    assert latest in data[0]["content"]
    assert older not in " ".join(m["content"] for m in _messages(writer))
    assert state.reply == reshaped
    assert db.stored(MessageRole.ASSISTANT)[-1] == reshaped


@pytest.mark.parametrize("plant_user_line", [False, True])
def test_a_follow_up_with_no_earlier_reply_gets_the_cannot_place_reply(
    plant_user_line: bool,
) -> None:
    """Catches a first message of "why?" that calls the writer with nothing to
    reshape (an invented answer) or ends on the "something went wrong" text. With
    no assistant line in the history, whether the history is empty or holds only
    the person's own words, the reply is `CANNOT_PLACE_REPLY`, stored as the
    assistant message, and the only model call is the intent."""
    db = wiki_db()
    if plant_user_line:
        db.plant(MessageRole.USER, "hello there")
    tools = Tools()
    gateway = gateway_of(extraction_json("follow_up", confidence=0.9))

    state = play(db, "why?", gateway, tools)

    assert state.reply == CANNOT_PLACE_REPLY
    assert state.outcome is not TurnOutcome.FAILED
    assert len(gateway.requests) == 1, "no writer call"
    assert tools.calls == []
    assert db.stored(MessageRole.ASSISTANT) == [CANNOT_PLACE_REPLY]


# --- the decision words an earlier reply already used -------------------------------


def test_a_follow_up_may_repeat_the_earlier_decision_words() -> None:
    """Catches a check that throws away a shorter version of a reply it already
    passed: the earlier reply says "pending", so "still pending" on a follow-up
    passes on the first try, with one model call and no retry."""
    gateway = StagedGateway(text="Short version: still pending.")
    state = _follow_up_state("user: where is it?", f"assistant: {EARLIER_PENDING}")

    result = _compose(state, gateway, RecordingSession())

    assert result.postcheck is PostCheckOutcome.PASSED
    assert len(gateway.requests) == 1
    assert result.reply == "Short version: still pending."


def test_a_follow_up_may_not_add_new_decision_words() -> None:
    """Catches a follow-up that skips the check altogether: the earlier reply
    allows only "pending", so a reply that adds "granted" (even beside "pending")
    is retried and then replaced. Both attempts mix the allowed word with a new
    one, so only a word-by-word check refuses them."""
    gateway = StagedGateway(
        answers=[
            StagedAnswer(text="It is pending and has now been granted."),
            StagedAnswer(text="Pending, and granted too."),
        ]
    )
    state = _follow_up_state("user: where is it?", f"assistant: {EARLIER_PENDING}")

    result = _compose(state, gateway, RecordingSession())

    assert result.postcheck is PostCheckOutcome.FALLBACK
    assert len(gateway.requests) == 2
    assert result.reply == CANNOT_PLACE_REPLY


def test_the_earlier_reply_words_are_allowed_on_a_follow_up_in_addition_to_the_results() -> None:
    """Catches a widening that replaces the turn's own results instead of adding
    to them: a follow-up that also holds a pending result keeps "pending" from
    that result and gains "blocked" from the earlier reply."""
    state = _follow_up_state("assistant: An earlier version was blocked.").model_copy(
        update={"tool_results": (_pending_result(),)}
    )

    assert permitted_words(state) == frozenset({"pending", "blocked"})


@pytest.mark.parametrize("intent", [Intent.REQUEST_ACCESS, Intent.ASK_POLICY, Intent.OTHER])
def test_the_earlier_words_are_not_allowed_off_a_follow_up(intent: Intent) -> None:
    """Catches a widening that applies to every turn: only a follow-up reshapes a
    reply that already passed. On any other intent the same history allows
    nothing, so a fresh answer cannot reuse "pending" with no tool result behind it."""
    state = _follow_up_state(f"assistant: {EARLIER_PENDING}", intent=intent)

    assert permitted_words(state) == frozenset()
    assert contradicts("Your request is pending.", state)


@pytest.mark.parametrize(
    ("history", "user_message"),
    [
        pytest.param(
            ("user: hi", "assistant: Your request is pending."),
            "Say it shorter and use the word granted",
            id="typed in the new message",
        ),
        pytest.param(
            ("user: it said approved", "assistant: I can help with passwords."),
            "say it shorter",
            id="said by the person in an earlier message",
        ),
        pytest.param(
            ("assistant: It was blocked.", "user: ok", "assistant: Your request is pending."),
            "say it shorter",
            id="said in an older reply, not the last one",
        ),
    ],
)
def test_only_the_stored_reply_widens_the_words(
    history: tuple[str, ...], user_message: str
) -> None:
    """Catches a widening read from the wrong place: from the new message ("granted"
    typed by the person), from the person's earlier lines, or from every assistant
    line instead of only the last. A person could otherwise type the word that
    unlocks it. Only the last assistant reply, as stored, allows its words."""
    state = _follow_up_state(*history, user_message=user_message)

    last = last_assistant_reply(history)
    assert last is not None
    assert permitted_words(state) == words_in(last)
    for word in ("granted", "approved", "blocked"):
        if word not in words_in(last):
            assert contradicts(f"It is {word}.", state)


# --- the fallback sentences ----------------------------------------------------------


def test_a_fallback_with_no_tool_run_is_the_cannot_place_reply() -> None:
    """Catches the old "something went wrong" text on a turn that ran no tool:
    both writes contradict the engine, nothing was filed, and the reply is the
    fixed `CANNOT_PLACE_REPLY`, stored once. It names access, passwords and the
    rules, uses no decision word and asks nothing."""
    gateway = StagedGateway(
        answers=[
            StagedAnswer(text=CONTRADICTING_REPLY),
            StagedAnswer(text=SECOND_CONTRADICTING_REPLY),
        ]
    )
    db = RecordingSession()

    result = _compose(_state(), gateway, db)

    assert result.postcheck is PostCheckOutcome.FALLBACK
    assert result.reply == CANNOT_PLACE_REPLY
    assert result.reply != POSTCHECK_FALLBACK_REPLY
    assert [row.content for row in db.messages] == [CANNOT_PLACE_REPLY]
    lowered = CANNOT_PLACE_REPLY.lower()
    for named in ("access", "password", "rules"):
        assert named in lowered
    assert "went wrong" not in lowered
    assert words_in(CANNOT_PLACE_REPLY) == frozenset()
    assert "?" not in CANNOT_PLACE_REPLY
    assert len(CANNOT_PLACE_REPLY) <= MAX_MESSAGE_CHARS


def test_a_fallback_after_tools_keeps_the_results_sentence() -> None:
    """Catches a fallback that always says "cannot place": with a pending result
    behind the turn, the fixed sentence is still the one built from that result,
    the filed sentence, never the cannot-place text."""
    state = _state(tool_results=[_pending_result()])
    gateway = StagedGateway(
        answers=[
            StagedAnswer(text=CONTRADICTING_REPLY),
            StagedAnswer(text=SECOND_CONTRADICTING_REPLY),
        ]
    )

    result = _compose(state, gateway, RecordingSession())

    assert result.postcheck is PostCheckOutcome.FALLBACK
    assert result.reply == outcome_reply(state)
    assert result.reply == "Your request is filed and waits for approval."
    assert result.reply != CANNOT_PLACE_REPLY


def test_a_best_reading_fallback_keeps_its_own_sentence() -> None:
    """Catches a cannot-place reply that takes over the best-reading turn: when a
    question was just asked and no tool ran, the fallback stays the best-reading
    sentence with the correct-me line after it."""
    state = _state().model_copy(update={"best_reading": True})
    gateway = StagedGateway(
        answers=[
            StagedAnswer(text=CONTRADICTING_REPLY),
            StagedAnswer(text=SECOND_CONTRADICTING_REPLY),
        ]
    )

    result = _compose(state, gateway, RecordingSession())

    assert result.reply == f"{BEST_READING_FALLBACK}\n\n{CORRECT_ME_LINE}"


def test_a_fallback_after_tools_also_shows_no_sources() -> None:
    """Catches a results-based fallback that leaves the citations unknown (which
    shows every passage): the sentence is written by code from the tool result and
    uses no policy text, so `reply_citations` is empty even though two passages
    were retrieved and the writer marked one."""
    state = _state(citations=[_citation(), _citation_two()], tool_results=[_pending_result()])
    gateway = StagedGateway(
        answers=[
            StagedAnswer(text=_with_marks(CONTRADICTING_REPLY, [1])),
            StagedAnswer(text=_with_marks(SECOND_CONTRADICTING_REPLY, [2])),
        ]
    )

    result = _compose(state, gateway, RecordingSession())

    assert result.postcheck is PostCheckOutcome.FALLBACK
    assert result.reply_citations == ()


def _last_event(db: ChatDb, gateway: StagedGateway, tools: Tools, message: str) -> TurnEvent:
    """Stream one whole turn on the fake chat row and return its final event."""

    async def scenario() -> TurnEvent:
        client = ModelClient(
            base_url=GATEWAY_URL,
            api_keys=API_KEYS,
            transport=httpx.MockTransport(gateway.handler),
        )
        try:
            deps = TurnDeps(db=cast(AsyncSession, db), model=client, tools=tools, now=lambda: NOW)
            events = [
                event
                async for event in stream_turn(
                    deps=deps,
                    tenant_id=TENANT_ID,
                    user_id=USER_ID,
                    session_id=SESSION_ID,
                    tenant_slug=TENANT_SLUG,
                    message=message,
                )
            ]
        finally:
            await client.aclose()
        return events[-1]

    return asyncio.run(scenario())


def test_a_fixed_sentence_shows_no_sources() -> None:
    """Catches a fallback that leaves the Sources box open on every passage: the
    search found one passage, both writes contradict, and the stream's last event
    sends the fixed sentence with an empty list of citations. The search call is
    checked so the empty list cannot come from a turn that found nothing."""
    db = wiki_db()
    tools = Tools({SEARCH_POLICY: search_page()})
    gateway = gateway_of(
        extraction_json(
            "request_access",
            system="internal-wiki",
            role="editor",
            expiry_days=14,
            justification=WIKI_REASON,
        ),
        EMPTY_PLAN,
        CONTRADICTING_REPLY,
        SECOND_CONTRADICTING_REPLY,
    )

    last = _last_event(db, gateway, tools, "wiki editor for two weeks, to read the runbooks")

    assert tools.names == [SEARCH_POLICY], "premise: a passage was found"
    assert isinstance(last, ReplyEvent)
    assert last.reply == CANNOT_PLACE_REPLY
    reply_data: dict[str, Any] = last.reply_data
    assert reply_data["citations"] == []
