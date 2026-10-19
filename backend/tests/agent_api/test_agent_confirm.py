"""Asking before filing, through the graph.

Expected implementation surface (names fixed by the plan):

``state.TurnOutcome.CONFIRM = "confirm"`` and ``CONFIRMED = "confirmed"``;
``state.TurnState.confirmed``; ``ResolvedEntities.system_name`` / ``role_name``;
``confirmation.py`` (``NOTHING_FILED_REPLY``, ``parse_open_question``);
``sessions.get_open_question`` / ``set_open_question``.

A request turn that plans ``create_access_request`` ends on ``confirm`` with a
question and files nothing. The next message decides: a yes runs exactly the
stored calls, then ONE more planning round (staged empty) and the composition;
a bare no ends on the fixed nothing-filed reply; anything else runs the normal
flow and drops the question.

Cost and isolation. Postgres is the shared `agent_db` session fixture: only
Postgres shows the JSONB round trip and a fresh graph reading it back. Each case
seeds its OWN tenant (a namespace) and a fresh `session_id`, so no state is
shared. The gateway is an `httpx.MockTransport` and the tools are a recording
fake, so no socket is opened. A turn that asks for more model answers than a
case staged fails the run, which is how "no model call here" is asserted.
"""

import asyncio
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel

from helpdesk.agent_api.agent.confirmation import (
    NOTHING_FILED_REPLY,
    OPEN_QUESTION_LIFE,
    parse_open_question,
)
from helpdesk.agent_api.agent.graph import Saver, TurnDeps, run_turn
from helpdesk.agent_api.agent.state import TurnOutcome, TurnState
from helpdesk.agent_api.agent.tool_args import SEARCH_POLICY
from helpdesk.agent_api.services.decision_card import decision_card
from helpdesk.agent_api.services.mcp_client import ToolCaller
from helpdesk.agent_api.services.model_gateway.client import ModelClient
from helpdesk.agent_api.services.model_gateway.strict_schema import strict_schema_problems
from helpdesk.shared.core.db import tenant_session
from helpdesk.shared.crud.sessions import (
    get_open_question,
    get_or_create_session,
    set_open_question,
)
from helpdesk.shared.models import Department, Role, System, Tenant, User
from tests.agent_api.conftest import AgentDb

NOW = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
GATEWAY_URL = "http://gateway.invalid:8080"

CREATE = "create_access_request"
CLASSIFY = "classify_access_request"

REQUEST_ID = uuid.UUID("77777777-7777-5777-8777-777777777777")
"""What the fake `create_access_request` answers, obviously fake."""

YES = "Yes, that is right."
ASK_PREFIX = "Do you mean Viewer access in Internal Wiki?"
ASK_PREFIX_EDITOR = "Do you mean Editor access in Internal Wiki?"
COMPOSED = "Your request was filed."


# --- the seeded tenant ----------------------------------------------------------


@dataclass(frozen=True)
class Env:
    db: AgentDb
    tenant_id: uuid.UUID
    tenant_slug: str
    user_id: uuid.UUID
    viewer_id: uuid.UUID
    editor_id: uuid.UUID


async def _seed(db: AgentDb) -> Env:
    slug = f"confirm-{uuid.uuid4().hex[:12]}"
    async with db.factory() as session:
        tenant = Tenant(slug=slug, name="Fake Confirm Tenant", residency="eu", settings={})
        session.add(tenant)
        await session.commit()
        tenant_id = tenant.id

    async with tenant_session(db.factory, tenant_id) as session:
        department = Department(tenant_id=tenant_id, name="fake-dept", manager_user_id=None)
        session.add(department)
        await session.flush()
        person = User(
            tenant_id=tenant_id,
            department_id=department.id,
            email=f"fake.person@{slug}.example.test",
            display_name="Fake Person",
            password_hash="argon2id$obviously-fake-test-hash",
            principals=["employee"],
            status="active",
            start_date=date(2026, 1, 5),
            end_date=None,
        )
        session.add(person)
        await session.flush()
        system = System(
            tenant_id=tenant_id,
            slug="internal-wiki",
            name="Internal Wiki",
            sensitivity="low",
            owner_user_id=person.id,
        )
        session.add(system)
        await session.flush()
        role_ids: dict[str, uuid.UUID] = {}
        for role_slug, role_name in (("wiki-viewer", "Viewer"), ("wiki-editor", "Editor")):
            role = Role(
                tenant_id=tenant_id,
                system_id=system.id,
                slug=role_slug,
                name=role_name,
                privilege="read",
                default_expiry_days=90,
            )
            session.add(role)
            await session.flush()
            role_ids[role_slug] = role.id
        return Env(
            db=db,
            tenant_id=tenant_id,
            tenant_slug=slug,
            user_id=person.id,
            viewer_id=role_ids["wiki-viewer"],
            editor_id=role_ids["wiki-editor"],
        )


@pytest.fixture
def env(agent_db: AgentDb) -> Env:
    """A new tenant with one system and two roles, for one case only."""
    return asyncio.run(_seed(agent_db))


# --- the stubbed gateway and the recording tools --------------------------------


def _as_composed(answer: str, request: httpx.Request) -> str:
    """What the gateway sends for a staged string. A request for `ComposedReply`
    gets a plain staged reply as the shape, marked a question when it holds "?"
    (a writer that marks honestly); a string that is already JSON goes as it is."""
    body = json.loads(request.content)
    if body.get("response_format", {}).get("json_schema", {}).get("name") != "ComposedReply":
        return answer
    try:
        parsed = json.loads(answer)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        return answer
    return json.dumps({"text": answer, "asks_question": "?" in answer})


class StagedGateway:
    """Answers staged in order; a request past the last one fails the test."""

    def __init__(self, *answers: str) -> None:
        self._answers = list(answers)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        schema = json.loads(request.content).get("response_format", {}).get("json_schema", {})
        if strict_schema_problems(schema.get("schema", {})):
            return httpx.Response(400, json={"error": "the schema breaks strict mode"})
        index = len(self.requests) - 1
        if index >= len(self._answers):
            raise AssertionError(
                f"the turn made {len(self.requests)} model requests; "
                f"only {len(self._answers)} were staged"
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": _as_composed(self._answers[index], request)}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )


def _page() -> dict[str, Any]:
    return {
        "citations": [
            {
                "document": "fake-access-policy",
                "title": "Fake Access Policy",
                "version": "v1",
                "heading_path": ["Fake Section"],
                "chunk_id": str(uuid.uuid4()),
                "text": "Obviously fake policy text about fake wiki access.",
                "score": 0.42,
            }
        ],
        "refused": False,
    }


def _classify_payload() -> dict[str, Any]:
    return {
        "risk_class": "low",
        "decision": "approval",
        "reasons": [],
        "required_approvers": [
            {"slot": "department_manager", "user_id": None},
            {"slot": "system_owner", "user_id": None},
        ],
        "effective_expiry_days": 30,
        "sod_conflicts": [],
        "policy_refs": [],
    }


def _create_payload() -> dict[str, Any]:
    """An auto grant, so the turn does not pause for an approval."""
    return {
        "request_id": str(REQUEST_ID),
        "status": "approved",
        "risk_class": "low",
        "decision": "auto",
        "reasons": [],
        "required_approvers": [],
        "approver_user_ids": [],
        "effective_expiry_days": 30,
        "sod_conflicts": [],
        "policy_refs": [],
        "grant_id": str(uuid.uuid4()),
        "expires_at": "2026-11-18T09:00:00Z",
    }


class FakeTools:
    """A `ToolCaller` that answers by tool name and records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._answers: dict[str, dict[str, Any]] = {
            SEARCH_POLICY: _page(),
            CLASSIFY: _classify_payload(),
            CREATE: _create_payload(),
        }

    async def __aenter__(self) -> ToolCaller:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def call_tool(self, name: str, arguments: BaseModel) -> Mapping[str, Any]:
        self.calls.append((name, arguments.model_dump(mode="json")))
        if name not in self._answers:
            raise AssertionError(f"the turn called an unscripted tool: {name}")
        return self._answers[name]

    @property
    def names(self) -> list[str]:
        return [name for name, _arguments in self.calls]

    def arguments_of(self, tool: str) -> list[dict[str, Any]]:
        return [arguments for name, arguments in self.calls if name == tool]


# --- staged model answers -------------------------------------------------------


def _extraction(intent: str, *, role: str | None = None, system: str | None = None) -> str:
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
            "confidence": 0.95,
            "clarifying_question": None,
        }
    )


def _access_extraction(role: str = "wiki-viewer") -> str:
    return _extraction("request_access", role=role, system="internal-wiki")


def _classify_round(env: Env) -> str:
    return json.dumps(
        {
            "calls": [
                {
                    "tool": CLASSIFY,
                    "arguments": json.dumps(
                        {
                            "target_user_id": str(env.user_id),
                            "role_id": str(env.viewer_id),
                            "requested_expiry_days": None,
                        }
                    ),
                }
            ]
        }
    )


def _create_round(env: Env, role_id: uuid.UUID) -> str:
    return json.dumps(
        {
            "calls": [
                {
                    "tool": CREATE,
                    "arguments": json.dumps(
                        {
                            "target_user_id": str(env.user_id),
                            "role_id": str(role_id),
                            "justification": "Obviously fake justification.",
                            "requested_expiry_days": None,
                            "citations": [],
                        }
                    ),
                }
            ]
        }
    )


EMPTY_ROUND = json.dumps({"calls": []})
"""The planning round a yes turn ends on: nothing more to do."""


def _ask_answers(env: Env) -> tuple[str, ...]:
    """Extraction, a classification round, then the create round that is held."""
    return (_access_extraction(), _classify_round(env), _create_round(env, env.viewer_id))


def _yes_answers() -> tuple[str, ...]:
    """The yes turn: the one more planning round (empty), then the composition."""
    return (EMPTY_ROUND, COMPOSED)


def _other_answers() -> tuple[str, ...]:
    """A turn about something else: extraction, no tools to plan, the composition."""
    return (_extraction("ask_policy"), COMPOSED)


# --- running one turn -----------------------------------------------------------


def _turn(
    env: Env,
    *,
    session_id: uuid.UUID,
    message: str,
    answers: Sequence[str],
    tools: FakeTools,
    now: datetime = NOW,
    checkpointer: Saver | None = None,
) -> TurnState:
    """One whole turn over its own database session and its own clock."""

    async def scenario() -> TurnState:
        gateway = StagedGateway(*answers)
        client = ModelClient(
            base_url=GATEWAY_URL,
            api_keys={env.tenant_slug: f"fake-key-{env.tenant_slug}"},
            transport=httpx.MockTransport(gateway.handler),
        )
        try:
            async with tenant_session(env.db.factory, env.tenant_id) as db:
                deps = TurnDeps(db=db, model=client, now=lambda: now, tools=tools)
                return await run_turn(
                    deps=deps,
                    tenant_id=env.tenant_id,
                    user_id=env.user_id,
                    session_id=session_id,
                    tenant_slug=env.tenant_slug,
                    message=message,
                    checkpointer=checkpointer,
                )
        finally:
            await client.aclose()

    return asyncio.run(scenario())


def _stored(env: Env, session_id: uuid.UUID) -> object:
    """The raw open question on the chat row, read through a new session."""

    async def read() -> object:
        async with tenant_session(env.db.factory, env.tenant_id) as db:
            return await get_open_question(db, session_id=session_id)

    return asyncio.run(read())


def _ask(env: Env, session_id: uuid.UUID, tools: FakeTools, *, now: datetime = NOW) -> TurnState:
    return _turn(
        env,
        session_id=session_id,
        message="I need viewer access to the internal wiki",
        answers=_ask_answers(env),
        tools=tools,
        now=now,
    )


# --- the asking turn ------------------------------------------------------------


def test_a_request_turn_asks_and_files_nothing(env: Env) -> None:
    """Catches a `validate_tool_calls` that lets a `create_access_request` round
    through before the person said yes (the request is filed on the first message),
    one that asks but still shows the decision card, and one that asks but forgets to
    store the question, so a yes would have nothing to file."""
    session_id = uuid.uuid4()
    tools = FakeTools()

    state = _ask(env, session_id, tools)

    assert state.outcome is TurnOutcome.CONFIRM
    assert state.reply is not None
    assert state.reply.startswith(ASK_PREFIX)
    assert CREATE not in tools.names
    assert tools.names == [SEARCH_POLICY, CLASSIFY]
    assert decision_card(state) is None
    stored = parse_open_question(_stored(env, session_id), now=NOW)
    assert stored is not None
    assert [call.tool for call in stored.calls] == [CREATE]
    assert stored.calls[0].arguments["role_id"] == str(env.viewer_id)
    assert stored.message == "I need viewer access to the internal wiki"


def test_the_question_says_what_happens_next_from_this_turns_classification(env: Env) -> None:
    """Catches a question that ignores the turn's `classify_access_request` result:
    the staged verdict needs the manager and the system owner, so a fixed sentence
    or a missing one is red."""
    state = _ask(env, uuid.uuid4(), FakeTools())

    assert state.reply is not None
    assert "your manager and the system owner" in state.reply


# --- the yes turn ---------------------------------------------------------------


def test_yes_files_the_stored_call_once(env: Env) -> None:
    """Catches a yes that plans again instead of replaying the stored call (the
    model could pick another role, and the key would differ), one that files twice,
    one that leaves the question behind, and one that gives no card: the call must
    carry the stored arguments and the stored idempotency key."""
    session_id = uuid.uuid4()
    ask_tools = FakeTools()
    _ask(env, session_id, ask_tools)
    raw = _stored(env, session_id)
    assert isinstance(raw, dict)
    stored_arguments = raw["calls"][0]["arguments"]
    assert stored_arguments["idempotency_key"]

    tools = FakeTools()
    state = _turn(env, session_id=session_id, message=YES, answers=_yes_answers(), tools=tools)

    assert tools.arguments_of(CREATE) == [stored_arguments]
    assert tools.names == [CREATE]
    assert _stored(env, session_id) is None
    card = decision_card(state)
    assert card is not None
    assert card.request_id == REQUEST_ID
    assert state.reply == COMPOSED


def test_a_second_yes_files_nothing_more(env: Env) -> None:
    """Catches a yes that does not clear the question, or a handler that files on
    the word alone: a second "yes" with no open question must run the normal flow,
    so across both yes turns `create_access_request` is called exactly once."""
    session_id = uuid.uuid4()
    tools = FakeTools()
    _ask(env, session_id, tools)
    _turn(env, session_id=session_id, message=YES, answers=_yes_answers(), tools=tools)
    assert tools.names.count(CREATE) == 1

    state = _turn(env, session_id=session_id, message=YES, answers=_other_answers(), tools=tools)

    assert tools.names.count(CREATE) == 1
    assert state.reply == COMPOSED
    assert _stored(env, session_id) is None


# --- a no, a correction, a change of subject ------------------------------------


def test_a_bare_no_files_nothing_and_clears_the_question(env: Env) -> None:
    """Catches a no that falls through to the normal flow (a model call and a
    possible second question), one that leaves the question stored so a later
    "yes" files it anyway, and one that replies in the model's words: no answer is
    staged, so any model call fails the run."""
    session_id = uuid.uuid4()
    _ask(env, session_id, FakeTools())
    tools = FakeTools()

    state = _turn(env, session_id=session_id, message="no", answers=(), tools=tools)

    assert state.reply == NOTHING_FILED_REPLY
    assert tools.calls == []
    assert _stored(env, session_id) is None


def test_a_correction_asks_again_with_the_new_role(env: Env) -> None:
    """Catches a "no, ..." of four words that is read as a bare no (the correction
    is lost) and one that re-asks with the OLD stored calls: the new question must
    name the editor role and hold a call for the editor role, and nothing is
    filed."""
    session_id = uuid.uuid4()
    _ask(env, session_id, FakeTools())
    tools = FakeTools()
    answers = (_access_extraction("wiki-editor"), _create_round(env, env.editor_id))

    state = _turn(
        env,
        session_id=session_id,
        message="no, I meant the editor role",
        answers=answers,
        tools=tools,
    )

    assert state.outcome is TurnOutcome.CONFIRM
    assert state.reply is not None
    assert state.reply.startswith(ASK_PREFIX_EDITOR)
    assert CREATE not in tools.names
    stored = parse_open_question(_stored(env, session_id), now=NOW)
    assert stored is not None
    assert stored.calls[0].arguments["role_id"] == str(env.editor_id)


def test_an_unrelated_message_drops_the_question(env: Env) -> None:
    """Catches a `load_session` that keeps the question when the person moved on:
    a password question must clear it, or a "yes" sent later in the chat files an
    access request the person no longer means."""
    session_id = uuid.uuid4()
    _ask(env, session_id, FakeTools())
    tools = FakeTools()

    _turn(
        env,
        session_id=session_id,
        message="How do I reset my password?",
        answers=_other_answers(),
        tools=tools,
    )

    assert _stored(env, session_id) is None
    assert CREATE not in tools.names


# --- surviving a reload ---------------------------------------------------------


def test_the_question_survives_a_fresh_graph(env: Env) -> None:
    """Catches a question kept anywhere but the chat row (an in-process variable, or
    the graph checkpoint): each turn gets a graph compiled over its own new saver
    with no checkpoint, so only the stored row can carry the question to the yes."""
    session_id = uuid.uuid4()
    ask_tools = FakeTools()
    _turn(
        env,
        session_id=session_id,
        message="I need viewer access to the internal wiki",
        answers=_ask_answers(env),
        tools=ask_tools,
        checkpointer=InMemorySaver(),
    )
    tools = FakeTools()

    state = _turn(
        env,
        session_id=session_id,
        message=YES,
        answers=_yes_answers(),
        tools=tools,
        checkpointer=InMemorySaver(),
    )

    assert tools.names == [CREATE]
    assert tools.arguments_of(CREATE)[0]["role_id"] == str(env.viewer_id)
    card = decision_card(state)
    assert card is not None
    assert card.request_id == REQUEST_ID


# --- unhappy paths --------------------------------------------------------------


@pytest.mark.parametrize(
    ("age", "files"),
    [
        (OPEN_QUESTION_LIFE - timedelta(seconds=1), True),
        (OPEN_QUESTION_LIFE, False),
    ],
)
def test_a_question_at_24_hours_is_dropped_and_just_under_is_kept(
    env: Env, age: timedelta, files: bool
) -> None:
    """Catches an age check that is off by one: a `>` keeps a question at exactly
    24 hours and files it, and a life shorter than 24 hours drops one at 24 hours
    minus a second. The question was asked with the same clock the yes is judged
    against, so only the age differs between the rows."""
    session_id = uuid.uuid4()
    _ask(env, session_id, FakeTools())
    tools = FakeTools()
    answers = _yes_answers() if files else _other_answers()

    _turn(
        env,
        session_id=session_id,
        message=YES,
        answers=answers,
        tools=tools,
        now=NOW + age,
    )

    assert tools.names.count(CREATE) == (1 if files else 0)
    assert _stored(env, session_id) is None


def test_a_bad_stored_question_counts_as_none(env: Env) -> None:
    """Catches a `load_session` that trusts whatever is in the metadata: a value
    with `calls` as a string must not crash the turn or file anything, and it must
    be cleared so it cannot come back on the next message."""
    session_id = uuid.uuid4()

    async def plant() -> None:
        async with tenant_session(env.db.factory, env.tenant_id) as db:
            await get_or_create_session(db, session_id=session_id, user_id=env.user_id, now=NOW)
            await set_open_question(db, session_id=session_id, value={"calls": "x"})

    asyncio.run(plant())
    assert _stored(env, session_id) == {"calls": "x"}
    tools = FakeTools()

    _turn(env, session_id=session_id, message=YES, answers=_other_answers(), tools=tools)

    assert CREATE not in tools.names
    assert _stored(env, session_id) is None
