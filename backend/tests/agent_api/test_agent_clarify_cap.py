"""One clarifying question in a row, through the graph.

Expected implementation surface (names fixed by the plan):

``state.TurnOutcome.ANSWER``; ``TurnState.asked_before`` and ``best_reading``;
``clarify_cap.py`` (``CORRECT_ME_LINE``, ``BEST_READING_FALLBACK``);
``sessions.get_asked_clarifying`` / ``set_asked_clarifying`` (key
``asked_clarifying_at`` in the session metadata, an ISO string).

Cost and isolation: the same as `test_agent_confirm.py`, whose seeded tenant,
staged gateway, recording tools and one-turn runner are reused. Postgres is the
shared `agent_db` fixture because only it shows the JSONB round trip across
turns. Each case seeds its own tenant and a fresh `session_id`. A turn that asks
for more model answers than a case staged fails the run, which is how "no model
call here" is asserted.
"""

import asyncio
import json
import uuid

import pytest

from helpdesk.agent_api.agent.clarify_cap import BEST_READING_FALLBACK, CORRECT_ME_LINE
from helpdesk.agent_api.agent.state import TurnOutcome, TurnState
from helpdesk.agent_api.agent.tool_args import SEARCH_POLICY
from helpdesk.shared.core.db import tenant_session
from helpdesk.shared.crud.sessions import get_asked_clarifying
from tests.agent_api.conftest import AgentDb
from tests.agent_api.test_agent_confirm import (
    ASK_PREFIX,
    COMPOSED,
    CREATE,
    NOW,
    Env,
    FakeTools,
    _ask,
    _ask_answers,
    _classify_round,
    _create_round,
    _seed,
    _turn,
)

FIRST_QUESTION = "Are you asking about the wiki or the portal?"
UNCLEAR = "Can I get that thing from before?"


@pytest.fixture
def env(agent_db: AgentDb) -> Env:
    """A new tenant with one system and two roles, for one case only."""
    return asyncio.run(_seed(agent_db))


def _unclear(
    intent: str = "ask_policy",
    *,
    confidence: float = 0.5,
    question: str | None = FIRST_QUESTION,
    role: str | None = None,
    system: str | None = None,
) -> str:
    return json.dumps(
        {
            "intent": intent,
            "entities": {
                "system": system,
                "role": role,
                "target_user": None,
                "department": None,
                "expiry_days": None,
                "justification": None,
            },
            "confidence": confidence,
            "clarifying_question": question,
        }
    )


def _stored_time(env: Env, session_id: uuid.UUID) -> object:
    async def read() -> object:
        async with tenant_session(env.db.factory, env.tenant_id) as db:
            return await get_asked_clarifying(db, session_id=session_id)

    return asyncio.run(read())


def _first_question(env: Env, session_id: uuid.UUID, tools: FakeTools) -> TurnState:
    return _turn(
        env,
        session_id=session_id,
        message=UNCLEAR,
        answers=(_unclear(),),
        tools=tools,
    )


def test_an_unclear_message_asks_and_stores_the_time(env: Env) -> None:
    """Catches an `ask_clarifying` that forgets to store the time (the next
    message could then ask again, which is the loop this plan ends), and one that
    stores something other than the turn clock's ISO string."""
    session_id = uuid.uuid4()
    tools = FakeTools()

    state = _first_question(env, session_id, tools)

    assert state.outcome is TurnOutcome.CLARIFY
    assert state.reply == FIRST_QUESTION
    assert _stored_time(env, session_id) == NOW.isoformat()
    assert tools.calls == []


def test_a_second_unclear_message_gets_an_answer_and_the_correct_me_line(env: Env) -> None:
    """Catches a cap that does not hold (a second question is asked), one that
    answers but leaves out the correct-me line, one that adds the line anywhere but
    the end, and one that leaves the stored time behind so a third message is
    capped too."""
    session_id = uuid.uuid4()
    _first_question(env, session_id, FakeTools())
    tools = FakeTools()

    state = _turn(
        env,
        session_id=session_id,
        message=UNCLEAR,
        answers=(_unclear("ask_policy"), COMPOSED),
        tools=tools,
    )

    assert state.outcome is not TurnOutcome.CLARIFY
    assert state.asked_before is True
    assert state.best_reading is True
    assert state.reply == f"{COMPOSED}\n\n{CORRECT_ME_LINE}"
    assert _stored_time(env, session_id) is None


def test_a_second_message_missing_the_system_calls_no_tool(env: Env) -> None:
    """Catches a cap that, with a required entity missing, still goes on to search
    or plan tools on a guess: the outcome must be ANSWER, so the fake tools see no
    call and the gateway gets only the extraction and the composition (a third
    request is not staged and fails the run)."""
    session_id = uuid.uuid4()
    _first_question(env, session_id, FakeTools())
    tools = FakeTools()

    state = _turn(
        env,
        session_id=session_id,
        message=UNCLEAR,
        answers=(_unclear("request_access", confidence=0.9, role="wiki-viewer"), COMPOSED),
        tools=tools,
    )

    assert tools.calls == []
    assert state.outcome is not TurnOutcome.CLARIFY
    assert state.reply == f"{COMPOSED}\n\n{CORRECT_ME_LINE}"
    assert _stored_time(env, session_id) is None


def test_a_composed_question_on_a_best_reading_is_retried_then_replaced(env: Env) -> None:
    """Catches a best-reading turn whose composed reply still asks a question (a
    second clarifying question in disguise): two staged replies with a "?" must
    end on the fixed fallback plus the correct-me line, after exactly one retry."""
    session_id = uuid.uuid4()
    _first_question(env, session_id, FakeTools())

    state = _turn(
        env,
        session_id=session_id,
        message=UNCLEAR,
        answers=(_unclear("ask_policy"), "Do you mean the wiki?", "Or maybe the portal?"),
        tools=FakeTools(),
    )

    assert state.reply == f"{BEST_READING_FALLBACK}\n\n{CORRECT_ME_LINE}"


def test_a_question_then_a_clean_retry_is_kept(env: Env) -> None:
    """Catches a fallback used straight after the first composed question, with no
    retry: the second staged reply is a statement and must be the one shown."""
    session_id = uuid.uuid4()
    _first_question(env, session_id, FakeTools())

    state = _turn(
        env,
        session_id=session_id,
        message=UNCLEAR,
        answers=(_unclear("ask_policy"), "Do you mean the wiki?", COMPOSED),
        tools=FakeTools(),
    )

    assert state.reply == f"{COMPOSED}\n\n{CORRECT_ME_LINE}"


def test_a_best_reading_request_still_asks_do_you_mean(env: Env) -> None:
    """Catches a cap that lets a guessed request through to filing: a second
    unclear message read as an access request for a named system must still end on
    the "Do you mean ...?" check, file nothing, and carry no correct-me line."""
    session_id = uuid.uuid4()
    _first_question(env, session_id, FakeTools())
    tools = FakeTools()
    answers = (
        _unclear("request_access", question=None, role="wiki-viewer", system="internal-wiki"),
        _classify_round(env),
        _create_round(env, env.viewer_id),
    )

    state = _turn(env, session_id=session_id, message=UNCLEAR, answers=answers, tools=tools)

    assert state.outcome is TurnOutcome.CONFIRM
    assert state.reply is not None
    assert state.reply.startswith(ASK_PREFIX)
    assert CORRECT_ME_LINE not in state.reply
    assert CREATE not in tools.names


def test_a_clear_message_after_a_question_runs_as_normal(env: Env) -> None:
    """Catches a correct-me line (or best-reading flag) added to every message that
    follows a question, even a clear one: at confidence 0.9 the reply is the
    composed text alone."""
    session_id = uuid.uuid4()
    _first_question(env, session_id, FakeTools())

    state = _turn(
        env,
        session_id=session_id,
        message="How do I reset my password?",
        answers=(_unclear("ask_policy", confidence=0.9, question=None), COMPOSED),
        tools=FakeTools(),
    )

    assert state.asked_before is True
    assert state.best_reading is False
    assert state.reply == COMPOSED
    assert _stored_time(env, session_id) is None


def test_a_third_unclear_message_may_ask_again(env: Env) -> None:
    """Catches a cap that holds forever once used (the session would never ask
    again): question, answer, then unclear again must be a question."""
    session_id = uuid.uuid4()
    _first_question(env, session_id, FakeTools())
    _turn(
        env,
        session_id=session_id,
        message=UNCLEAR,
        answers=(_unclear("ask_policy"), COMPOSED),
        tools=FakeTools(),
    )

    state = _turn(
        env,
        session_id=session_id,
        message=UNCLEAR,
        answers=(_unclear(question="Which one do you mean, A or B?"),),
        tools=FakeTools(),
    )

    assert state.outcome is TurnOutcome.CLARIFY
    assert state.reply == "Which one do you mean, A or B?"
    assert _stored_time(env, session_id) == NOW.isoformat()


def test_the_message_after_a_bare_no_is_answered(env: Env) -> None:
    """Catches a bare no that is not counted as a clarifying question: the "no"
    reply must store the time, so the next unclear message is answered, not asked
    about."""
    session_id = uuid.uuid4()
    _ask(env, session_id, FakeTools())
    _turn(env, session_id=session_id, message="no", answers=(), tools=FakeTools())
    assert _stored_time(env, session_id) == NOW.isoformat()
    tools = FakeTools()

    state = _turn(
        env,
        session_id=session_id,
        message=UNCLEAR,
        answers=(_unclear("ask_policy"), COMPOSED),
        tools=tools,
    )

    assert state.outcome is not TurnOutcome.CLARIFY
    assert state.reply == f"{COMPOSED}\n\n{CORRECT_ME_LINE}"
    assert SEARCH_POLICY in tools.names


def test_a_confirm_question_alone_stores_no_time(env: Env) -> None:
    """Catches an `ask_confirmation` that also stores the clarifying time (the
    "Do you mean ...?" check is not a clarifying question, a design note), which would
    cap the very next unclear message."""
    session_id = uuid.uuid4()

    state = _turn(
        env,
        session_id=session_id,
        message="I need viewer access to the internal wiki",
        answers=_ask_answers(env),
        tools=FakeTools(),
    )

    assert state.outcome is TurnOutcome.CONFIRM
    assert _stored_time(env, session_id) is None
