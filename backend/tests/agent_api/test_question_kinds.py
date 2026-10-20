"""Find the role first, then ask only what is missing.

Names the implementation adds (fixed by the plan):

* ``clarify_cap.QuestionKind`` (StrEnum: ``SUGGEST="suggest"``, ``NAME="name"``,
  ``DETAILS="details"``, ``OTHER="other"``). A reason-only question reads
  ``"Before I file this, tell me the business reason for <role name>."``.
* ``TurnState.asked_kind`` (the kind of the question the person just answered)
  and ``TurnState.question_kind`` (the kind of the question this turn ends on).
* ``sessions.get_asked_kind`` / ``set_asked_kind`` (the kind next to its time on
  the chat row; unreadable, older than 24 hours or unknown reads as none).
* For ``request_access`` the intent step asks only for a missing system or low
  confidence; the details question comes after the role is found.

The first group runs single nodes on plain fakes. The second group runs whole
turns through the graph on the shared ``agent_db`` fixture, because only that
shows the kind stored on the chat row and read back by the next message.
"""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.agent_api.agent.clarify_cap import BEST_READING_FALLBACK, QuestionKind
from helpdesk.agent_api.agent.nodes.extract_intent import extract_intent
from helpdesk.agent_api.agent.nodes.resolve_entities import (
    NOTHING_MATCHED_QUESTION,
    SUGGESTION_PREFIX,
    resolve_entities,
)
from helpdesk.agent_api.agent.state import (
    Intent,
    IntentEntities,
    IntentExtraction,
    TurnOutcome,
    TurnState,
)
from helpdesk.agent_api.agent.tool_args import SUGGEST_ROLES
from helpdesk.agent_api.agent.turn_deps import TurnDeps
from helpdesk.agent_api.services.mcp_client import RemoteInternalError, ToolCaller
from helpdesk.agent_api.services.model_gateway.client import ModelClient
from helpdesk.shared.core.db import tenant_session
from helpdesk.shared.crud.sessions import get_asked_kind, set_asked_kind
from helpdesk.shared.models import Role
from tests.agent_api.conftest import AgentDb
from tests.agent_api.test_agent_clarify_cap import (
    DAYS,
    DETAILS_QUESTION,
    REASON,
    UNCLEAR,
    _add_second_viewer,
    _request_without_role,
    _unclear,
)
from tests.agent_api.test_agent_confirm import (
    COMPOSED,
    NOW,
    Env,
    FakeTools,
    _seed,
    _turn,
)
from tests.agent_api.test_resolve_entities_suggestions import (
    _FakeDb,
    _StubTools,
    _suggested,
    _system,
)

REASON_ONLY_QUESTION = "Before I file this, tell me the business reason for Editor."
TURN_NOW = datetime(2026, 6, 2, tzinfo=UTC)

# --- the intent step ------------------------------------------------------------


def _extraction(
    *,
    system: str | None,
    role: str | None = None,
    justification: str | None = None,
    expiry_days: int | None = None,
    confidence: float = 0.95,
) -> IntentExtraction:
    return IntentExtraction(
        intent=Intent.REQUEST_ACCESS,
        entities=IntentEntities(
            system=system,
            role=role,
            target_user=None,
            department=None,
            expiry_days=expiry_days,
            justification=justification,
        ),
        confidence=confidence,
        clarifying_question=None,
    )


class _FakeModel:
    """Answers `complete_json` with the one extraction the case holds."""

    def __init__(self, extraction: IntentExtraction) -> None:
        self._extraction = extraction

    async def complete_json(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(value=self._extraction, usage=SimpleNamespace(total_tokens=2))


def _extract(extraction: IntentExtraction) -> TurnState:
    state = TurnState(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        tenant_slug="fake-tenant",
        user_message="i want doc access",
        started_at=TURN_NOW,
    )
    deps = TurnDeps(
        db=cast(AsyncSession, None),
        model=cast(ModelClient, _FakeModel(extraction)),
        tools=cast(ToolCaller, None),
        now=lambda: TURN_NOW,
    )
    return asyncio.run(extract_intent(state, deps))


def test_a_request_naming_a_system_goes_on_without_a_reason() -> None:
    """Catches an intent step that still asks "how long and why" before the name
    lookup: a confident request that names a system but no reason and no length
    must go on to the resolving step, so "i want doc access" reaches the lookup."""
    state = _extract(_extraction(system="doc"))

    assert state.outcome is TurnOutcome.CONTINUE


def test_a_request_with_no_system_still_asks_at_extraction() -> None:
    """Catches a narrowed extraction check that drops the system test too: the
    reason and length are given and confidence is high, so the missing system is
    the only thing that can make this a question."""
    state = _extract(
        _extraction(system=None, role="editor", justification=REASON, expiry_days=DAYS)
    )

    assert state.outcome is TurnOutcome.CLARIFY


def test_a_low_confidence_request_still_asks_at_extraction() -> None:
    """Catches a narrowed extraction check that stops looking at confidence: the
    system is named, so only the unsure intent can make this a question."""
    state = _extract(_extraction(system="doc", confidence=0.5))

    assert state.outcome is TurnOutcome.CLARIFY


# --- the resolving step ---------------------------------------------------------

WIKI = _system("wiki", "Internal Wiki")
EDITOR = Role(
    id=uuid.uuid4(),
    tenant_id=WIKI.tenant_id,
    system_id=WIKI.id,
    slug="editor",
    name="Editor",
    privilege="write",
    default_expiry_days=90,
)
GOOD_SUGGESTION = {"roles": [_suggested("wiki", "Internal Wiki", "editor", "Editor")]}


def _resolve(
    tools: _StubTools,
    *,
    system: str | None,
    role: str | None,
    asked_kind: QuestionKind | None = None,
    justification: str | None = None,
    expiry_days: int | None = None,
) -> TurnState:
    """One resolving step; `asked_before` is true whenever a kind was read, as
    the loader sets both from the same stored question."""
    state = TurnState(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        tenant_slug="fake-tenant",
        user_message="fake message",
        extraction=_extraction(
            system=system, role=role, justification=justification, expiry_days=expiry_days
        ),
        started_at=TURN_NOW,
        asked_before=asked_kind is not None,
        asked_kind=asked_kind,
    )
    deps = TurnDeps(
        db=cast(AsyncSession, _FakeDb([WIKI], [EDITOR])),
        model=cast(ModelClient, None),
        tools=cast(ToolCaller, tools),
        now=lambda: TURN_NOW,
    )
    return asyncio.run(resolve_entities(state, deps))


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param(QuestionKind.DETAILS, id="after-details"),
        pytest.param(QuestionKind.OTHER, id="after-another-question"),
    ],
)
def test_a_miss_after_a_details_question_still_suggests(kind: QuestionKind) -> None:
    """Catches a cap that still counts any earlier question: after a question that
    is not a suggestion or the exact-name question, a name that matches nothing
    must still be looked up and offered, not answered with no tools."""
    tools = _StubTools(payload=GOOD_SUGGESTION)

    state = _resolve(tools, system="docs", role="scribe", asked_kind=kind)

    assert [name for name, _args in tools.calls] == [SUGGEST_ROLES]
    assert state.outcome is TurnOutcome.DISAMBIGUATE
    assert state.reply is not None
    assert state.reply.startswith(SUGGESTION_PREFIX)
    assert state.question_kind is QuestionKind.SUGGEST


def test_a_miss_after_a_suggestion_asks_for_the_exact_name() -> None:
    """Catches a resolver that suggests again, or answers, right after a
    suggestion: the payload is a good one, so only the stored kind can stop the
    second offer, and the question must be the exact-name one, kind `name`."""
    tools = _StubTools(payload=GOOD_SUGGESTION)

    state = _resolve(tools, system="docs", role="scribe", asked_kind=QuestionKind.SUGGEST)

    assert tools.calls == []
    assert state.outcome is TurnOutcome.CLARIFY
    assert state.reply == NOTHING_MATCHED_QUESTION
    assert state.question_kind is QuestionKind.NAME


def test_a_miss_after_the_exact_name_question_stops() -> None:
    """Catches a resolver that asks for the exact name a second time (a loop the
    person cannot leave): after the `name` question the turn must answer with no
    tools. The payload is good, so only the stored kind can stop the call."""
    tools = _StubTools(payload=GOOD_SUGGESTION)

    state = _resolve(tools, system="docs", role="scribe", asked_kind=QuestionKind.NAME)

    assert tools.calls == []
    assert state.outcome is TurnOutcome.ANSWER


@pytest.mark.parametrize(
    "tools",
    [
        pytest.param(_StubTools(payload={"roles": []}), id="empty"),
        pytest.param(_StubTools(error=RemoteInternalError("fake outage")), id="failed"),
    ],
)
def test_an_empty_or_failed_search_goes_straight_to_the_exact_name_question(
    tools: _StubTools,
) -> None:
    """Catches a resolver that stores kind `suggest` for the exact-name question
    when the search finds nothing: the next miss would then ask the same question
    again instead of stopping, so the kind must be `name`."""
    state = _resolve(tools, system="docs", role="scribe")

    assert len(tools.calls) == 1, "the premise: the search was asked first"
    assert state.outcome is TurnOutcome.CLARIFY
    assert state.reply == NOTHING_MATCHED_QUESTION
    assert state.question_kind is QuestionKind.NAME


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param(None, id="first-message"),
        pytest.param(QuestionKind.SUGGEST, id="after-a-suggestion"),
    ],
)
def test_a_found_role_with_no_reason_asks_the_details_question(kind: QuestionKind | None) -> None:
    """Catches a resolver that skips the details question after a suggestion
    because a question was just asked (the old one-question cap), and one that
    never asks it: a found role with no reason and no length must ask the
    code-worded question, kind `details`."""
    tools = _StubTools(payload=GOOD_SUGGESTION)

    state = _resolve(tools, system="wiki", role="editor", asked_kind=kind)

    assert tools.calls == []
    assert state.outcome is TurnOutcome.CLARIFY
    assert state.reply == DETAILS_QUESTION
    assert state.question_kind is QuestionKind.DETAILS


@pytest.mark.parametrize(
    "expiry_days",
    [pytest.param(None, id="no-length"), pytest.param(14, id="length-given")],
)
def test_a_reason_still_missing_after_the_details_question_is_asked_by_name(
    expiry_days: int | None,
) -> None:
    """Catches a resolver that, after the details question, ends on the
    best-reading fallback or repeats the long question: the reason is still
    missing, so it must be asked for alone, by role name."""
    tools = _StubTools()

    state = _resolve(
        tools,
        system="wiki",
        role="editor",
        asked_kind=QuestionKind.DETAILS,
        expiry_days=expiry_days,
    )

    assert state.outcome is TurnOutcome.CLARIFY
    assert state.reply == REASON_ONLY_QUESTION
    assert state.reply != BEST_READING_FALLBACK


def test_a_length_still_missing_after_the_details_question_goes_on() -> None:
    """Catches a resolver that asks for the length a second time: the reason is
    given, so after the details question the role's default length applies and
    the turn continues with the role resolved."""
    tools = _StubTools()

    state = _resolve(
        tools,
        system="wiki",
        role="editor",
        asked_kind=QuestionKind.DETAILS,
        justification=REASON,
    )

    assert state.outcome is TurnOutcome.CONTINUE
    assert state.resolved is not None
    assert state.resolved.role_slug == "editor"


# --- the kind stored on the chat row --------------------------------------------


@pytest.fixture
def env(agent_db: AgentDb) -> Env:
    """A new tenant with one system and two roles, for one case only."""
    return asyncio.run(_seed(agent_db))


def _stored_kind(env: Env, session_id: uuid.UUID) -> object:
    async def read() -> object:
        async with tenant_session(env.db.factory, env.tenant_id) as db:
            return await get_asked_kind(db, session_id=session_id)

    return asyncio.run(read())


def _wire(raw: object) -> str:
    """The stored value as text, whatever shape the implementation gives it."""
    return json.dumps(raw)


def _missing_details(env: Env) -> str:
    return _unclear(
        "request_access",
        confidence=0.95,
        question=None,
        system="internal-wiki",
        role="wiki-viewer",
    )


def _unknown_system() -> str:
    return _unclear("request_access", confidence=0.95, question=None, system="docs")


def _suggesting_tools() -> FakeTools:
    tools = FakeTools()
    tools._answers[SUGGEST_ROLES] = {
        "roles": [_suggested("internal-wiki", "Internal Wiki", "wiki-viewer", "Viewer")]
    }
    return tools


def _first_turn(env: Env, session_id: uuid.UUID, kind: str) -> FakeTools:
    """A turn that ends on a question of the given kind."""
    tools = _suggesting_tools()
    if kind == "details":
        _turn(
            env,
            session_id=session_id,
            message="I need viewer access to the internal wiki",
            answers=(_missing_details(env),),
            tools=tools,
        )
    elif kind == "suggest":
        _turn(
            env,
            session_id=session_id,
            message="i want doc access",
            answers=(_unknown_system(),),
            tools=tools,
        )
    elif kind == "other-intent":
        _turn(env, session_id=session_id, message=UNCLEAR, answers=(_unclear(),), tools=tools)
    elif kind == "other-compose":
        _request_without_role(env, session_id, tools)
    else:
        raise AssertionError(kind)
    return tools


@pytest.mark.db
@pytest.mark.parametrize(
    ("how", "kind"),
    [
        pytest.param("details", QuestionKind.DETAILS, id="details-question"),
        pytest.param("suggest", QuestionKind.SUGGEST, id="suggestions"),
        pytest.param("other-intent", QuestionKind.OTHER, id="intent-question"),
        pytest.param("other-compose", QuestionKind.OTHER, id="composed-question"),
    ],
)
def test_the_kind_is_stored_with_each_question_end(env: Env, how: str, kind: QuestionKind) -> None:
    """Catches an ending that forgets to store the kind (the next message could
    not tell a suggestion from any other question), one that stores the wrong
    kind for a question end, and one that stores the kind without its time (the
    24-hour limit would have nothing to read)."""
    session_id = uuid.uuid4()

    _first_turn(env, session_id, how)

    stored = _stored_kind(env, session_id)
    assert stored is not None
    assert kind.value in _wire(stored)
    assert NOW.isoformat() in _wire(stored)


@pytest.mark.db
def test_the_disambiguation_end_stores_kind_other(env: Env) -> None:
    """Catches the "which one do you mean" ending storing `suggest`: two roles
    share the name Viewer, so the options are an ordinary question, and a later
    miss must not be read as coming right after a suggestion."""
    asyncio.run(_add_second_viewer(env))
    session_id = uuid.uuid4()

    _turn(
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

    stored = _stored_kind(env, session_id)
    assert stored is not None
    assert QuestionKind.OTHER.value in _wire(stored)
    assert QuestionKind.SUGGEST.value not in _wire(stored)


@pytest.mark.db
def test_the_kind_is_cleared_by_the_next_message(env: Env) -> None:
    """Catches a loader that reads the kind and leaves it on the chat row: a
    clear message after a suggestion must leave nothing stored, or a much later
    miss would be read as coming right after that old suggestion."""
    session_id = uuid.uuid4()
    _first_turn(env, session_id, "suggest")
    assert _stored_kind(env, session_id) is not None, "the premise: a kind was stored"

    _turn(
        env,
        session_id=session_id,
        message="How do I reset my password?",
        answers=(_unclear("ask_policy", confidence=0.9, question=None), COMPOSED),
        tools=FakeTools(),
    )

    assert _stored_kind(env, session_id) is None


@pytest.mark.db
@pytest.mark.parametrize(
    ("age", "tool_called"),
    [
        pytest.param(timedelta(hours=24) - timedelta(seconds=1), False, id="just-inside-24h"),
        pytest.param(timedelta(hours=24) + timedelta(seconds=1), True, id="just-over-24h"),
    ],
)
def test_a_stored_kind_older_than_24_hours_reads_as_none(
    env: Env, age: timedelta, tool_called: bool
) -> None:
    """Catches a kind that never expires, and one that expires early: a miss
    right after a suggestion asks for the exact name without a search, but once
    the suggestion is over 24 hours old the person has moved on and the same miss
    is searched and offered again."""
    session_id = uuid.uuid4()
    _first_turn(env, session_id, "suggest")
    tools = _suggesting_tools()

    state = _turn(
        env,
        session_id=session_id,
        message="i want doc access",
        answers=(_unknown_system(),),
        tools=tools,
        now=NOW + age,
    )

    assert (SUGGEST_ROLES in tools.names) is tool_called
    if tool_called:
        assert state.outcome is TurnOutcome.DISAMBIGUATE
    else:
        assert state.reply == NOTHING_MATCHED_QUESTION


@pytest.mark.db
def test_an_unknown_stored_kind_reads_as_none(env: Env) -> None:
    """Catches a reader that trusts any stored value: a real `suggest` is turned
    into a kind outside the four, and the next miss must then be treated as
    coming after no question at all (searched and offered), not as a
    suggestion (exact-name question) or another kind."""
    session_id = uuid.uuid4()
    _first_turn(env, session_id, "suggest")
    raw = _stored_kind(env, session_id)
    assert QuestionKind.SUGGEST.value in _wire(raw), "the premise: a suggestion was stored"
    tampered: Any = json.loads(_wire(raw).replace(QuestionKind.SUGGEST.value, "bogus"))

    async def plant() -> None:
        async with tenant_session(env.db.factory, env.tenant_id) as db:
            await set_asked_kind(db, session_id=session_id, value=tampered)

    asyncio.run(plant())
    tools = _suggesting_tools()

    state = _turn(
        env,
        session_id=session_id,
        message="i want doc access",
        answers=(_unknown_system(),),
        tools=tools,
    )

    assert SUGGEST_ROLES in tools.names
    assert state.outcome is TurnOutcome.DISAMBIGUATE


@pytest.mark.db
def test_a_suggestion_then_a_second_miss_asks_for_the_name_then_stops(env: Env) -> None:
    """Catches a chain that loops on suggestions or on the exact-name question:
    miss, miss, miss must give the offer, the exact-name question, then an answer
    that asks nothing (the third is composed, so it is not a question)."""
    session_id = uuid.uuid4()
    tools = _suggesting_tools()
    first = _turn(
        env,
        session_id=session_id,
        message="i want doc access",
        answers=(_unknown_system(),),
        tools=tools,
    )
    second = _turn(
        env,
        session_id=session_id,
        message="docs",
        answers=(_unknown_system(),),
        tools=tools,
    )
    third = _turn(
        env,
        session_id=session_id,
        message="docs again",
        answers=(_unknown_system(), COMPOSED),
        tools=tools,
    )

    assert first.outcome is TurnOutcome.DISAMBIGUATE
    assert second.reply == NOTHING_MATCHED_QUESTION
    assert third.outcome is not TurnOutcome.CLARIFY
    assert tools.names == [SUGGEST_ROLES]
