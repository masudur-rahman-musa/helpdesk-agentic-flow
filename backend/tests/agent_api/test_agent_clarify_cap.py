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
from sqlalchemy import select

from helpdesk.agent_api.agent.clarify_cap import (
    BEST_READING_FALLBACK,
    CORRECT_ME_LINE,
    QuestionKind,
)
from helpdesk.agent_api.agent.nodes.resolve_entities import DISAMBIGUATION_PREFIX, SUGGESTION_PREFIX
from helpdesk.agent_api.agent.state import TurnOutcome, TurnState
from helpdesk.agent_api.agent.tool_args import SEARCH_POLICY, SUGGEST_ROLES
from helpdesk.shared.core.db import tenant_session
from helpdesk.shared.crud.sessions import get_asked_clarifying
from helpdesk.shared.models import Role, System
from tests.agent_api.conftest import AgentDb
from tests.agent_api.test_agent_confirm import (
    ASK_PREFIX,
    COMPOSED,
    CREATE,
    NOW,
    YES,
    Env,
    FakeTools,
    _ask,
    _ask_answers,
    _classify_round,
    _create_round,
    _seed,
    _turn,
    _yes_answers,
)
from tests.agent_api.test_resolve_entities_suggestions import _suggested

FIRST_QUESTION = "Are you asking about the wiki or the portal?"
UNCLEAR = "Can I get that thing from before?"
REASON = "Obviously fake reason for the wiki."
DAYS = 30
DETAILS_QUESTION = (
    "That's Internal Wiki wiki viewer. "
    "Before I check this, tell me how long you need it for and the business reason."
)


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
    justification: str | None = None,
    expiry_days: int | None = None,
) -> str:
    return json.dumps(
        {
            "intent": intent,
            "entities": {
                "system": system,
                "role": role,
                "target_user": None,
                "department": None,
                "expiry_days": expiry_days,
                "justification": justification,
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
    the summary check, file nothing, and carry no correct-me line."""
    session_id = uuid.uuid4()
    _first_question(env, session_id, FakeTools())
    tools = FakeTools()
    answers = (
        _unclear(
            "request_access",
            question=None,
            role="wiki-viewer",
            system="internal-wiki",
            justification=REASON,
            expiry_days=DAYS,
        ),
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
    "Do you mean ...?" check is not a clarifying question), which would
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


# --- every question before filing is recorded -------------------------

EDITOR_OR_VIEWER = "Do you want the viewer role or the editor role?"
"""A question the reply writer asks, not the extraction."""


def _request_without_role(env: Env, session_id: uuid.UUID, tools: FakeTools) -> TurnState:
    """A request turn naming the system but no role: nothing to file, so the
    planner returns an empty round and the reply writer asks which role."""
    return _turn(
        env,
        session_id=session_id,
        message="I need access to the internal wiki",
        answers=(
            _unclear(
                "request_access",
                confidence=0.95,
                question=None,
                system="internal-wiki",
                justification=REASON,
                expiry_days=DAYS,
            ),
            json.dumps({"calls": []}),
            EDITOR_OR_VIEWER,
        ),
        tools=tools,
    )


def test_a_question_from_compose_on_a_request_turn_is_recorded(env: Env) -> None:
    """Catches a compose step that asks "viewer or editor?" without recording it
    (the live chat asked a second question right after, because the first one
    left no mark). The premise is asserted: the turn did not end in a clarifying
    node, nothing was filed, and the question is the composed reply."""
    session_id = uuid.uuid4()
    tools = FakeTools()

    state = _request_without_role(env, session_id, tools)

    assert state.outcome is not TurnOutcome.CLARIFY
    assert CREATE not in tools.names
    assert state.reply == EDITOR_OR_VIEWER
    assert _stored_time(env, session_id) == NOW.isoformat()


async def _add_second_viewer(env: Env) -> None:
    """A second role also named Viewer in the same system, so "viewer" is two."""
    async with tenant_session(env.db.factory, env.tenant_id) as db:
        system = (
            await db.execute(select(System).where(System.slug == "internal-wiki"))
        ).scalar_one()
        db.add(
            Role(
                tenant_id=env.tenant_id,
                system_id=system.id,
                slug="wiki-viewer-two",
                name="Viewer",
                privilege="read",
                default_expiry_days=90,
            )
        )


def test_a_disambiguation_is_recorded(env: Env) -> None:
    """Catches an `ask_disambiguation` that offers "Which one do you mean?" and
    records nothing, so the next message could ask a second question. Two roles
    are both named Viewer, so the term resolves to two and no node but the
    disambiguation ending can have stored the time."""
    asyncio.run(_add_second_viewer(env))
    session_id = uuid.uuid4()

    state = _turn(
        env,
        session_id=session_id,
        message="I need viewer access to the internal wiki",
        answers=(
            _unclear(
                "request_access",
                confidence=0.95,
                question=None,
                role="viewer",
                system="internal-wiki",
                justification=REASON,
                expiry_days=DAYS,
            ),
        ),
        tools=FakeTools(),
    )

    assert state.outcome is TurnOutcome.DISAMBIGUATE
    assert state.reply is not None
    assert state.reply.startswith(DISAMBIGUATION_PREFIX)
    assert _stored_time(env, session_id) == NOW.isoformat()


def test_the_next_message_gets_the_best_reading(env: Env) -> None:
    """Catches the loop from the live chat, as an earlier change reads it: after a
    recorded compose question, a role that matches nothing is offered the
    closest roles (any other question before does not stop a suggestion), not
    met with a second "could not find" question, and nothing is filed."""
    session_id = uuid.uuid4()
    _request_without_role(env, session_id, FakeTools())
    tools = FakeTools()
    tools._answers[SUGGEST_ROLES] = {
        "roles": [_suggested("internal-wiki", "Internal Wiki", "wiki-viewer", "Viewer")]
    }

    state = _turn(
        env,
        session_id=session_id,
        message="the fake-nonexistent-role one",
        answers=(
            _unclear(
                "request_access",
                confidence=0.95,
                question=None,
                role="fake-nonexistent-role",
                system="internal-wiki",
                justification=REASON,
                expiry_days=DAYS,
            ),
        ),
        tools=tools,
    )

    assert state.outcome is TurnOutcome.DISAMBIGUATE
    assert state.reply is not None
    assert state.reply.startswith(SUGGESTION_PREFIX)
    assert CREATE not in tools.names


def test_a_question_after_filing_is_not_recorded(env: Env) -> None:
    """Catches a recording rule that stores the time for any composed question,
    filed or not: the yes turn files the request, and the writer asks a question
    both times it is asked (it is then replaced by the fixed reply), yet nothing
    may be recorded, since the next message must be free to ask."""
    session_id = uuid.uuid4()
    _turn(
        env,
        session_id=session_id,
        message="I need viewer access to the internal wiki",
        answers=_ask_answers(env),
        tools=FakeTools(),
    )
    tools = FakeTools()

    _turn(
        env,
        session_id=session_id,
        message=YES,
        answers=(
            _yes_answers()[0],
            "It is filed. Do you need anything else?",
            "It is filed. Shall I help with more?",
        ),
        tools=tools,
    )

    assert CREATE in tools.names
    assert _stored_time(env, session_id) is None


# --- the writer's mark decides what is recorded ---------------------


def _request_without_role_composing(
    env: Env, session_id: uuid.UUID, reply: str, *, asks: bool
) -> TurnState:
    """The same turn as `_request_without_role`, with the composed answer sent in
    the shape and its mark set by the case, not read from a "?"."""
    return _turn(
        env,
        session_id=session_id,
        message="I need access to the internal wiki",
        answers=(
            _unclear(
                "request_access",
                confidence=0.95,
                question=None,
                system="internal-wiki",
                justification=REASON,
                expiry_days=DAYS,
            ),
            json.dumps({"calls": []}),
            json.dumps({"text": reply, "asks_question": asks, "used_passages": [1]}),
        ),
        tools=FakeTools(),
    )


@pytest.mark.db
def test_a_marked_question_without_a_question_mark_is_recorded(env: Env) -> None:
    """Catches a recording rule that still looks for "?": the answer asks, worded
    without one and marked true, so nothing but the mark can have stored the time."""
    session_id = uuid.uuid4()

    state = _request_without_role_composing(env, session_id, "Say viewer or editor", asks=True)

    assert state.reply == "Say viewer or editor"
    assert _stored_time(env, session_id) == NOW.isoformat()


@pytest.mark.db
def test_an_unmarked_question_mark_is_not_recorded(env: Env) -> None:
    """Catches a recording rule that still looks for "?": the answer holds one but
    is marked false (a quoted question), so recording it would cap the next message."""
    session_id = uuid.uuid4()

    state = _request_without_role_composing(
        env, session_id, "Viewer or editor? Either works for me.", asks=False
    )

    assert state.reply == "Viewer or editor? Either works for me."
    assert _stored_time(env, session_id) is None


# --- the details question -------------------------------------------------------


def test_a_confident_request_without_details_asks_the_code_question(env: Env) -> None:
    """Catches a confident request that lets the model word the question (it wrote
    "How long do you need it?", one detail only) or that goes on to tools with no
    reason; also one that forgets to store the asked time. Since an earlier change the
    question comes after the role is found, so the turn must carry the resolved
    role and mark the question `details`."""
    session_id = uuid.uuid4()
    tools = FakeTools()

    state = _turn(
        env,
        session_id=session_id,
        message="I need viewer access to the internal wiki",
        answers=(
            _unclear(
                "request_access",
                confidence=0.95,
                question="How long do you need it?",
                role="wiki-viewer",
                system="internal-wiki",
            ),
        ),
        tools=tools,
    )

    assert state.outcome is TurnOutcome.CLARIFY
    assert state.reply == DETAILS_QUESTION
    assert state.resolved is not None
    assert state.resolved.role_slug == "wiki-viewer"
    assert state.question_kind is QuestionKind.DETAILS
    assert tools.calls == []
    assert _stored_time(env, session_id) == NOW.isoformat()


def test_a_low_confidence_request_keeps_the_models_question(env: Env) -> None:
    """Catches a code question that replaces the model's own on every request
    turn: at confidence 0.5 the intent itself is unsure, so the model's question
    about it must be the reply."""
    state = _turn(
        env,
        session_id=uuid.uuid4(),
        message=UNCLEAR,
        answers=(
            _unclear(
                "request_access",
                confidence=0.5,
                question=FIRST_QUESTION,
                role="wiki-viewer",
                system="internal-wiki",
            ),
        ),
        tools=FakeTools(),
    )

    assert state.outcome is TurnOutcome.CLARIFY
    assert state.reply == FIRST_QUESTION
