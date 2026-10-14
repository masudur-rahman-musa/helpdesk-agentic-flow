"""An earlier change §6: the compiled turn graph, end to end.

Expected implementation surface (the implementer must provide exactly this):

``agent_api.graph.build``
    ``@dataclass(frozen=True) class TurnDeps`` — ``db: AsyncSession``,
        ``model: ModelClient``, ``now: Callable[[], datetime]``.
    ``FAILURE_REPLY: str`` — the templated reply a failed extraction ends on.
    ``def build_graph() -> CompiledStateGraph`` — compiled with NO
        checkpointer (decision F).
    ``async def run_turn(*, deps, tenant_id, user_id, session_id, tenant_slug,
        message) -> TurnState`` — runs ``check_message_content(message)`` and
        ``check_slug(str(session_id))`` BEFORE anything is written or the
        graph is invoked, then invokes the compiled graph.

Nodes: ``load_session`` → ``extract_intent`` → ``resolve_entities``, with the
END nodes ``ask_clarifying`` and ``ask_disambiguation``.

An earlier change extends the same file. ``TurnDeps`` gains ``tools: ToolCaller``, the
turn continues from ``resolve_entities`` into ``retrieve_policy``, and
``refuse_and_ticket`` is a fourth END node. Every case here drives a STUB
caller: the real MCP session is proven once, in
``test_agent_mcp_client.py``, so nothing in this file opens a socket or
reaches Qdrant.

``agent_api.graph.nodes.retrieve_policy``
    ``MAX_QUERY_CHARS = 1000`` · ``def query_for(message: str) -> str``
    ``async def retrieve_policy(state, deps) -> TurnState`` — never raises.

``agent_api.graph.nodes.endings``
    ``REFUSAL_REPLY: str`` · ``async def refuse_and_ticket(state, deps)``

An earlier change extends it again. The turn continues from ``retrieve_policy`` into
``plan_tools`` and ``validate_tool_calls``, and ends at the validation
outcome; nothing is EXECUTED until 17d, so a turn still makes at most the one
``search_policy`` call.

``agent_api.graph.nodes.plan_tools``
    ``PLANNING_PROMPT = "tool_planning"``
    ``async def plan_tools(state: TurnState, deps: TurnDeps) -> TurnState`` —
        one ``complete_json`` call per round under ``ModelAlias.AGENT_STRONG``
        (decision G), constrained to ``ToolPlanAnswer``; an intent with an empty
        ``INTENT_TOOLS`` row makes NO call at all; a turn past any of the three
        budgets proposes nothing and records ``TurnOutcome.REFUSE``. Never
        raises: a gateway failure ends the turn as state, like every other
        node.
    ``run_turn`` stamps ``started_at`` from ``deps.now()``.

Every case that stages model answers for an intent whose row is non-empty now
stages a SECOND answer, the planning round. Those are fixture
changes: no assertion in them moved.

An earlier change closes the turn. The graph gains ``execute_tools`` (which edges BACK
to ``plan_tools``) and ``compose_reply``, so a turn that used to end at the
validation handover now executes its round, replans, and composes one
assistant message:

``agent_api.graph.nodes.execute_tools``
    ``async def execute_tools(state, deps) -> TurnState`` — every validated
        call through ``deps.tools``, ``tool_calls_made`` up by the number
        ATTEMPTED (decision F).
``agent_api.graph.nodes.compose_reply``
    ``COMPOSITION_PROMPT = "reply_composition"``
    ``async def compose_reply(state, deps) -> TurnState`` — one ``complete``
        call under ``agent-default``, one assistant row, the reply on the
        state, the answer's ``total_tokens`` added to ``tokens_used``.
``agent_api.graph.gates``
    ``execution_outcome`` · ``exhausted_outcome`` · ``TurnOutcome.DONE``
``agent_api.graph.build``
    ``GRAPH_RECURSION_LIMIT = 50`` — passed by ``run_turn`` in the invocation
        config (decision G).

Two fixture changes carry the whole of an earlier change §8: every case that reaches
``plan_tools`` stages one more answer, the composition (``COMPOSED_REPLY``),
and a turn that ran to the end now finishes on ``TurnOutcome.DONE`` with an
assistant row rather than on ``CONTINUE`` with none.

An earlier change changes one line of that surface, and nothing else in this file:
``build_graph(checkpointer=None)`` now TAKES a saver and compiles with it, so
the argument-free call every case here makes still carries none. The
checkpointer's own module, the approval interrupt and the paused state live in
``test_agent_checkpointer.py`` and ``test_agent_approval_interrupt.py``;
nothing here opens a psycopg pool.

Cost and isolation. Postgres is the compose stack's, one throwaway database
built once for the whole module with three obviously fake
tenants seeded into it once; per-case isolation is a namespace, a fresh
``session_id`` per case, plus a dedicated tenant for the cases that need a
different candidate list. The gateway is an ``httpx.MockTransport``, so no
socket is opened and no model is called; nothing here touches Qdrant or
Redis. Fixtures live in this module rather than a conftest, because a second
``conftest`` under ``services/agent-api/tests`` would collide with
``packages/helpdesk/tests/conftest.py`` under the repo-wide ``mypy`` run.

An engine is built per ``asyncio.run`` (each case drives its own event loop),
exactly as ``packages/helpdesk/tests/test_policy_models.py`` does; the
DATABASE and its schema are what the session-scoped fixture builds once.
"""

import asyncio
import json
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, cast

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel, ValidationError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from helpdesk.agent_api.graph import build as build_module
from helpdesk.agent_api.graph.build import (
    FAILURE_REPLY,
    GRAPH_RECURSION_LIMIT,
    TurnDeps,
    build_graph,
    run_turn,
)
from helpdesk.agent_api.graph.gates import (
    MAX_CALLS_PER_ROUND,
    MAX_DISAMBIGUATION_OPTIONS,
    MAX_TOOL_CALLS,
    MAX_TURN_TOKENS,
)
from helpdesk.agent_api.graph.nodes.endings import REFUSAL_REPLY
from helpdesk.agent_api.graph.nodes.plan_tools import PLANNING_PROMPT, plan_tools
from helpdesk.agent_api.graph.nodes.retrieve_policy import MAX_QUERY_CHARS, query_for
from helpdesk.agent_api.graph.state import (
    Citation,
    Intent,
    IntentEntities,
    IntentExtraction,
    TurnOutcome,
    TurnState,
)
from helpdesk.agent_api.mcp_client import RemoteInternalError, ToolCaller
from helpdesk.agent_api.prompts import load_prompt
from helpdesk.agent_api.services.model_gateway.client import ModelAlias, ModelClient
from helpdesk.agent_api.services.model_gateway.strict_schema import strict_schema_problems
from helpdesk.agent_api.tools.catalogue import SEARCH_POLICY
from helpdesk.shared.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.shared.core.repository import (
    HISTORY_LIMIT,
    Repository,
    append_message,
    get_or_create_session,
)
from helpdesk.shared.core.settings import load_settings
from helpdesk.shared.errors import ErrorCode
from helpdesk.shared.models import Base, Department, Message, Role, Session, System, Tenant, User
from helpdesk.shared.models.conversation import MAX_MESSAGE_CHARS, MessageRole, MessageTooLongError
from helpdesk.shared.rag.naming import check_slug

NOW = datetime(2026, 6, 2, 10, 15, tzinfo=UTC)

GATEWAY_URL = "http://gateway.invalid:8080"

TENANT_A = "turn-tenant-a"
TENANT_B = "turn-tenant-b"
TENANT_C = "turn-tenant-c"

# Obviously fake, tests only: these are not credentials.
API_KEYS: dict[str, str] = {slug: f"fake-key-{slug}" for slug in (TENANT_A, TENANT_B, TENANT_C)}

PLANTED_MESSAGES = 25
"""More than `HISTORY_LIMIT`, so the window has something to cut off."""

_UNREACHABLE_MSG = (
    "Postgres is unreachable at the configured DATABASE_URL.\n"
    "Plan 17a turn tests require the compose stack: run `docker compose up -d` first.\n"
    "Original error: {error!r}"
)


# --- the throwaway database ---------------------------------------------------


async def _admin_execute(admin_url: str, sql: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


class TempDatabaseFactory:
    def __init__(self, admin_url: str) -> None:
        self._admin_url = admin_url

    def create(self) -> str:
        name = f"helpdesk_turn_test_{uuid.uuid4().hex[:12]}"
        asyncio.run(_admin_execute(self._admin_url, f'CREATE DATABASE "{name}"'))
        return make_url(self._admin_url).set(database=name).render_as_string(hide_password=False)

    def drop(self, url: str) -> None:
        name = make_url(url).database
        assert name is not None and name.startswith("helpdesk_turn_test_")
        asyncio.run(_admin_execute(self._admin_url, f'DROP DATABASE "{name}" WITH (FORCE)'))


@pytest.fixture(scope="session")
def pg() -> TempDatabaseFactory:
    """Connectivity gate: fails loudly (never skips) when Postgres is down."""
    try:
        settings = load_settings()
    except ValidationError as exc:
        pytest.fail(
            f"DATABASE_URL is not configured (env or .env). Copy .env.example to .env.\n{exc}",
            pytrace=False,
        )
    try:
        asyncio.run(_admin_execute(settings.database_url, "SELECT 1"))
    except Exception as exc:
        pytest.fail(_UNREACHABLE_MSG.format(error=exc), pytrace=False)
    return TempDatabaseFactory(settings.database_url)


@dataclass(frozen=True)
class SeededTurnTenant:
    """One fake tenant: two people and a named candidate list."""

    tenant_id: uuid.UUID
    tenant_slug: str
    user_id: uuid.UUID
    other_user_id: uuid.UUID
    system_ids: dict[str, uuid.UUID] = field(default_factory=dict)
    role_ids: dict[str, uuid.UUID] = field(default_factory=dict)


@dataclass(frozen=True)
class TurnEnv:
    database_url: str
    tenant_a: SeededTurnTenant
    tenant_b: SeededTurnTenant
    tenant_c: SeededTurnTenant


async def _seed(
    factory: async_sessionmaker[AsyncSession],
    slug: str,
    systems: Sequence[tuple[str, str]],
    roles: Sequence[tuple[str, str]],
) -> SeededTurnTenant:
    """One tenant, two users, and the systems/roles a case matches against.

    Seeded by hand rather than through `scripts/seed.py`: these cases turn on
    exactly which candidates a tenant owns, and the demo corpus would put
    other systems in the way of the near-miss and disambiguation rows.
    """
    session_factory = factory

    async with session_factory() as session:
        tenant = Tenant(slug=slug, name=f"Fake Tenant {slug}", residency="eu", settings={})
        session.add(tenant)
        await session.commit()
        tenant_id = tenant.id

    async with tenant_session(session_factory, tenant_id) as session:
        department = Department(tenant_id=tenant_id, name=f"dept-{slug}", manager_user_id=None)
        session.add(department)
        await session.flush()

        people: list[User] = []
        for index in (1, 2):
            person = User(
                tenant_id=tenant_id,
                department_id=department.id,
                email=f"fake.person{index}@{slug}.example.test",
                display_name=f"Fake Person {index} ({slug})",
                password_hash="argon2id$obviously-fake-test-hash",
                principals=["employee"],
                status="active",
                start_date=date(2026, 1, 5),
                end_date=None,
            )
            people.append(person)
        session.add_all(people)
        await session.flush()

        system_ids: dict[str, uuid.UUID] = {}
        first_system: System | None = None
        for system_slug, system_name in systems:
            system = System(
                tenant_id=tenant_id,
                slug=system_slug,
                name=system_name,
                sensitivity="low",
                owner_user_id=people[0].id,
            )
            session.add(system)
            await session.flush()
            system_ids[system_slug] = system.id
            first_system = first_system or system

        role_ids: dict[str, uuid.UUID] = {}
        for role_slug, role_name in roles:
            assert first_system is not None
            role = Role(
                tenant_id=tenant_id,
                system_id=first_system.id,
                slug=role_slug,
                name=role_name,
                privilege="read",
                default_expiry_days=90,
            )
            session.add(role)
            await session.flush()
            role_ids[role_slug] = role.id

        return SeededTurnTenant(
            tenant_id=tenant_id,
            tenant_slug=slug,
            user_id=people[0].id,
            other_user_id=people[1].id,
            system_ids=system_ids,
            role_ids=role_ids,
        )


@pytest.fixture(scope="session")
def turn_env(pg: TempDatabaseFactory) -> Iterator[TurnEnv]:
    """One database, schema and three seeded tenants, built exactly once.

    Tenant B holds a system with the SAME slug as tenant A's, which is what
    the cross-tenant candidate case needs; tenant C holds two systems sharing
    one display name, which is what makes a turn ambiguous.
    """
    url = pg.create()

    async def prepare() -> TurnEnv:
        engine = create_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            factory = create_session_factory(engine)
            tenant_a = await _seed(
                factory,
                TENANT_A,
                [("payroll", "Fake Payroll")],
                [("payroll-read", "Fake Payroll Read")],
            )
            tenant_b = await _seed(factory, TENANT_B, [("payroll", "Fake Payroll")], [])
            tenant_c = await _seed(
                factory,
                TENANT_C,
                [("billing-eu", "Fake Billing"), ("billing-us", "Fake Billing")],
                [],
            )
            return TurnEnv(
                database_url=url, tenant_a=tenant_a, tenant_b=tenant_b, tenant_c=tenant_c
            )
        finally:
            await engine.dispose()

    yield asyncio.run(prepare())
    pg.drop(url)


# --- the stubbed gateway ------------------------------------------------------


TOKENS_PER_ANSWER = 2
"""What one staged answer bills unless the case says otherwise.

Every turn's `tokens_used` is then `TOKENS_PER_ANSWER` times the number of
gateway calls it made, which is what
`test_tokens_used_is_the_sum_of_every_staged_answers_total_tokens` reads.
"""


@dataclass(frozen=True)
class StagedAnswer:
    """One staged model answer and the bill the gateway reports for it.

    The bill is separable from the text because two cases turn on it: the
    token budget can only be spent through a gateway answer, and `tokens_used`
    can only be told from a hard-coded number by answers that differ.
    """

    text: str
    total_tokens: int = TOKENS_PER_ANSWER


def _completion(answer: str | StagedAnswer) -> httpx.Response:
    staged = StagedAnswer(answer) if isinstance(answer, str) else answer
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": staged.text}}],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": staged.total_tokens - 1,
                "total_tokens": staged.total_tokens,
            },
        },
    )


def _extraction_json(
    *,
    intent: str,
    confidence: float,
    entities: dict[str, object] | None = None,
    clarifying_question: str | None = None,
) -> str:
    return json.dumps(
        {
            "intent": intent,
            "entities": {**dict.fromkeys(IntentEntities.model_fields), **(entities or {})},
            "confidence": confidence,
            "clarifying_question": clarifying_question,
        }
    )


class StagedGateway:
    """Answers staged in order; a request past the last one fails the test.

    That is how "the transport was never called a second time" is asserted as
    a hard stop rather than as a count read afterwards.
    """

    def __init__(self, answers: Sequence[str | StagedAnswer]) -> None:
        self._answers = list(answers)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        schema = json.loads(request.content).get("response_format", {}).get("json_schema", {})
        if strict_schema_problems(schema.get("schema", {})):
            # As OpenAI answers a schema strict mode refuses, so a
            # non-strict schema cannot pass this suite unseen.
            return httpx.Response(400, json={"error": "the schema breaks strict mode"})
        index = len(self.requests) - 1
        if index >= len(self._answers):
            raise AssertionError(
                f"the turn made {len(self.requests)} model requests; "
                f"only {len(self._answers)} were staged"
            )
        return _completion(self._answers[index])

    @property
    def call_count(self) -> int:
        return len(self.requests)

    @property
    def staged_tokens(self) -> int:
        """What every answer this gateway actually served billed, in total."""
        served = self._answers[: len(self.requests)]
        return sum(
            TOKENS_PER_ANSWER if isinstance(answer, str) else answer.total_tokens
            for answer in served
        )


# --- the stubbed tool caller ---------------------------------------


class StubToolCaller:
    """A `ToolCaller` that records every call and answers what the case says.

    The recording is what makes "zero tool calls" and "exactly one" assertable
    as counts rather than inferred from the state, and what lets the refusal
    case prove NO ticket was created (decision E) instead of assuming it.

    `entered` and `exited` are counted separately so a turn that opened the
    session and never closed it is a red case, not an invisible leak.
    """

    def __init__(
        self,
        *,
        payload: Mapping[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self._payload: Mapping[str, Any] = (
            payload if payload is not None else {"citations": [], "refused": False}
        )
        self._error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> ToolCaller:
        self.entered += 1
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.exited += 1

    async def call_tool(self, name: str, arguments: BaseModel) -> Mapping[str, Any]:
        self.calls.append((name, arguments.model_dump()))
        if self._error is not None:
            raise self._error
        return self._payload

    @property
    def names(self) -> list[str]:
        return [name for name, _arguments in self.calls]


def _citation(document: str, *, score: float) -> dict[str, Any]:
    """One citation as the tool puts it on the wire (json mode, obviously fake)."""
    return {
        "document": document,
        "title": f"Fake {document.replace('-', ' ').title()} Policy",
        "version": "v1",
        "heading_path": ["Fake Section", "Fake Subsection"],
        "chunk_id": str(uuid.uuid4()),
        "text": f"Obviously fake policy text about {document}.",
        "score": score,
    }


def _page(*citations: dict[str, Any], refused: bool = False) -> dict[str, Any]:
    return {"citations": list(citations), "refused": refused}


# --- helpers ------------------------------------------------------------------


def _run_turn(
    env: TurnEnv,
    tenant: SeededTurnTenant,
    *,
    session_id: uuid.UUID,
    message: str,
    gateway: StagedGateway,
    user_id: uuid.UUID | None = None,
    plant: Sequence[tuple[MessageRole, str]] = (),
    tools: StubToolCaller | None = None,
) -> TurnState:
    """Run one turn, optionally after planting a conversation history."""
    caller = tools if tools is not None else StubToolCaller()

    async def scenario() -> TurnState:
        engine = create_engine(env.database_url)
        try:
            factory = create_session_factory(engine)
            if plant:
                async with tenant_session(factory, tenant.tenant_id) as db:
                    await get_or_create_session(
                        db,
                        session_id=session_id,
                        user_id=user_id or tenant.user_id,
                        now=NOW,
                    )
                    for role, content in plant:
                        await append_message(db, session_id=session_id, role=role, content=content)
            client = ModelClient(
                base_url=GATEWAY_URL,
                api_keys=API_KEYS,
                transport=httpx.MockTransport(gateway.handler),
            )
            try:
                async with tenant_session(factory, tenant.tenant_id) as db:
                    deps = TurnDeps(db=db, model=client, now=lambda: NOW, tools=caller)
                    return await run_turn(
                        deps=deps,
                        tenant_id=tenant.tenant_id,
                        user_id=user_id or tenant.user_id,
                        session_id=session_id,
                        tenant_slug=tenant.tenant_slug,
                        message=message,
                    )
            finally:
                await client.aclose()
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


@dataclass(frozen=True)
class StoredMessage:
    role: str
    content: str
    ordinal: int
    tenant_id: uuid.UUID


def _stored_messages(
    env: TurnEnv, tenant: SeededTurnTenant, session_id: uuid.UUID
) -> list[StoredMessage]:
    """Every message row of one conversation, in `ordinal` order."""

    async def scenario() -> list[StoredMessage]:
        engine = create_engine(env.database_url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant.tenant_id) as db:
                rows = await Repository(db, Message).list()
                stored = [
                    StoredMessage(
                        role=str(row.role),
                        content=row.content,
                        ordinal=row.ordinal,
                        tenant_id=row.tenant_id,
                    )
                    for row in rows
                    if row.session_id == session_id
                ]
            return sorted(stored, key=lambda row: row.ordinal)
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _stored_session(
    env: TurnEnv, tenant: SeededTurnTenant, session_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID] | None:
    """`(tenant_id, user_id)` of the stored session row, or None."""

    async def scenario() -> tuple[uuid.UUID, uuid.UUID] | None:
        engine = create_engine(env.database_url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant.tenant_id) as db:
                row = await Repository(db, Session).get(session_id)
                return None if row is None else (row.tenant_id, row.user_id)
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _payroll_answer(confidence: float = 0.95) -> str:
    return _extraction_json(
        intent="request_access",
        confidence=confidence,
        entities={"system": "payroll", "role": "payroll-read"},
    )


# --- an earlier change: the planning round the model answers with ----------------------

TARGET_USER_ID = "44444444-4444-5444-8444-444444444444"
ROLE_ID = "55555555-5555-5555-8555-555555555555"


def _plan_json(*calls: dict[str, Any]) -> str:
    """One planning answer, in the shape `ToolPlanAnswer` fixes."""
    return json.dumps({"calls": list(calls)})


def _proposal(tool: str, **arguments: Any) -> dict[str, Any]:
    """One planned call; its arguments travel as a JSON string."""
    return {"tool": tool, "arguments": json.dumps(arguments)}


EMPTY_PLAN = _plan_json()
"""A round proposing nothing: the ordinary end of a turn under this plan.

Staged wherever an existing 17a/17b case now reaches `plan_tools` (plan §8).
Nothing is executed until 17d, so an empty round keeps every one of those
cases at the same outcome, the same message rows and the same tool calls."""

COMPOSED_REPLY = "I have put your fake payroll request in front of your manager."
"""The composition's answer, staged wherever a turn now reaches `compose_reply`.

Prose, not JSON: decision E makes the composition a plain `complete` call, so
an implementation that asked for structured output would be handed this string
and fail to parse it — which is a red this file should show rather than hide.
"""

CLASSIFY_PROPOSAL = _proposal(
    "classify_access_request", target_user_id=TARGET_USER_ID, role_id=ROLE_ID
)
"""A legal `request_access` proposal: catalogued, in the intent's row, and
carrying exactly the two arguments its model requires."""


def _model_of(request: httpx.Request) -> str:
    """The alias a recorded gateway request names."""
    body: dict[str, Any] = json.loads(request.content)
    return str(body["model"])


def _messages_of(request: httpx.Request) -> list[dict[str, str]]:
    body: dict[str, Any] = json.loads(request.content)
    messages: list[dict[str, str]] = body["messages"]
    return messages


# --- the three-node happy path ------------------------------------------------


def test_the_happy_path_runs_the_three_nodes_and_resolves_the_entities(turn_env: TurnEnv) -> None:
    """Catches a graph whose edges skip `resolve_entities` — the state would
    come back `CONTINUE` with nothing resolved, and the tool call would go out
    against a system named only as free text the model produced.

    An earlier change §8: the turn no longer ENDS after `resolve_entities`, it continues
    into `retrieve_policy`, so the same case now also catches an
    `_after_entities` left routing `CONTINUE` to `END` — the tool call count
    below is what sees it. Every 17a assertion is unchanged.

    An earlier change §8: it continues once more, through `plan_tools` and
    `validate_tool_calls`, and ends at the validation outcome. The second
    staged answer is the planning round; a graph that still stopped after
    retrieval would leave it unused, which the outcome and the call count
    below cannot see, so `test_a_request_access_turn_plans_exactly_once...`
    is what pins the new edge. No assertion here moved.

    An earlier change §8: it continues to the end. The empty round is accepted as
    `DONE`, the turn composes, and the third staged answer is the composition
    — so the outcome and the call count both move, and a graph still ending at
    the validation handover leaves the third answer unread and the user with
    no reply at all."""
    session_id = uuid.uuid4()
    gateway = StagedGateway([_payroll_answer(), EMPTY_PLAN, COMPOSED_REPLY])
    tools = StubToolCaller()

    state = _run_turn(
        turn_env,
        turn_env.tenant_a,
        session_id=session_id,
        message="I need payroll access",
        gateway=gateway,
        tools=tools,
    )

    assert tools.names == [SEARCH_POLICY]
    assert state.outcome is TurnOutcome.DONE
    assert state.reply == COMPOSED_REPLY
    assert state.extraction is not None
    assert state.extraction.intent is Intent.REQUEST_ACCESS
    assert state.resolved is not None
    assert state.resolved.system_id == turn_env.tenant_a.system_ids["payroll"]
    assert state.resolved.system_slug == "payroll"
    assert state.resolved.role_id == turn_env.tenant_a.role_ids["payroll-read"]
    assert gateway.call_count == 3


def test_the_prompt_version_is_the_extraction_prompts_own_digest(turn_env: TurnEnv) -> None:
    """Catches a `prompt_version` invented by the node — a hand-kept string or
    the model's own name — which makes every trace claim a version no file in
    `prompts/` has, so a bad turn cannot be tied to the prompt that produced
    it (the design)."""
    session_id = uuid.uuid4()
    gateway = StagedGateway([_payroll_answer(), EMPTY_PLAN, COMPOSED_REPLY])

    state = _run_turn(
        turn_env,
        turn_env.tenant_a,
        session_id=session_id,
        message="payroll please",
        gateway=gateway,
    )

    assert state.prompt_version == load_prompt("intent_extraction").sha256


def test_the_user_message_row_is_written_before_intent_runs(turn_env: TurnEnv) -> None:
    """Catches a node that stores the user's message only after a successful
    extraction: the model answers unusably twice here, so a turn that wrote
    the row late would leave the user's own words out of the transcript and
    out of the next turn's history. `ordinal` proves the ORDER, which
    `created_at` cannot: both rows share one transaction timestamp."""
    session_id = uuid.uuid4()
    gateway = StagedGateway(["not json at all", '{"nope": true}'])

    state = _run_turn(
        turn_env, turn_env.tenant_a, session_id=session_id, message="who am I", gateway=gateway
    )

    rows = _stored_messages(turn_env, turn_env.tenant_a, session_id)
    assert state.outcome is TurnOutcome.FAILED
    assert [row.role for row in rows] == [MessageRole.USER.value, MessageRole.ASSISTANT.value]
    assert rows[0].content == "who am I"
    assert rows[0].ordinal < rows[1].ordinal


def test_the_history_window_is_the_last_twenty_messages_oldest_first(turn_env: TurnEnv) -> None:
    """Catches a window ordered by `created_at` or handed to the model newest
    first. Twenty-five rows are planted in ONE transaction, so they share a
    `created_at` and only `ordinal` can order them; the plan's §1 sequence
    (load the history, THEN store the incoming message) is what fixes that the
    new message is not one of the twenty."""
    session_id = uuid.uuid4()
    gateway = StagedGateway([_payroll_answer(), EMPTY_PLAN, COMPOSED_REPLY])
    planted = [(MessageRole.USER, f"planted {index:02d}") for index in range(PLANTED_MESSAGES)]

    state = _run_turn(
        turn_env,
        turn_env.tenant_a,
        session_id=session_id,
        message="the newest message",
        gateway=gateway,
        plant=planted,
    )

    history = list(state.history)
    assert len(history) == HISTORY_LIMIT
    assert history == [
        f"{MessageRole.USER.value}: planted {index:02d}"
        for index in range(PLANTED_MESSAGES - HISTORY_LIMIT, PLANTED_MESSAGES)
    ]
    assert all("the newest message" not in line for line in history)


# --- the message-length pre-check ---------------------------------------------


def test_a_message_of_exactly_the_cap_is_accepted_and_stored(turn_env: TurnEnv) -> None:
    """The inclusive half of the boundary, end to end: catches an off-by-one
    that refuses the longest message the column can hold, and a column
    narrower than the check, which would answer the same turn with a
    truncation error after the session row was written."""
    session_id = uuid.uuid4()
    gateway = StagedGateway([_payroll_answer(), EMPTY_PLAN, COMPOSED_REPLY])
    message = "x" * MAX_MESSAGE_CHARS

    state = _run_turn(
        turn_env, turn_env.tenant_a, session_id=session_id, message=message, gateway=gateway
    )

    rows = _stored_messages(turn_env, turn_env.tenant_a, session_id)
    user_rows = [row for row in rows if row.role == MessageRole.USER.value]
    assert state.outcome is TurnOutcome.DONE
    assert len(user_rows) == 1
    assert len(user_rows[0].content) == MAX_MESSAGE_CHARS


def test_a_message_one_character_over_the_cap_writes_nothing_at_all(turn_env: TurnEnv) -> None:
    """The exclusive half, and the "before any effect" half of standing rule
    1: catches a check run inside a node instead of ahead of the graph, which
    would leave a session row and a half-written turn behind for a message the
    system then refuses. The staged gateway has NO answers, so any model call
    fails the case too."""
    session_id = uuid.uuid4()
    gateway = StagedGateway([])

    with pytest.raises(MessageTooLongError) as excinfo:
        _run_turn(
            turn_env,
            turn_env.tenant_a,
            session_id=session_id,
            message="x" * (MAX_MESSAGE_CHARS + 1),
            gateway=gateway,
        )

    assert excinfo.value.code is ErrorCode.VALIDATION
    assert gateway.call_count == 0
    assert _stored_messages(turn_env, turn_env.tenant_a, session_id) == []
    assert _stored_session(turn_env, turn_env.tenant_a, session_id) is None


def test_every_canonical_session_id_passes_the_session_id_pre_check() -> None:
    """`run_turn` gates the session id through `check_slug` before it becomes
    the gateway's `X-Session`. Catches a `SLUG_PATTERN` or cap change in
    `helpdesk.rag.naming` that stops admitting a canonical uuid string, which
    would refuse EVERY turn with `VALIDATION` — the pre-check failing open is
    impossible here (the parameter is typed `uuid.UUID`), so failing closed is
    the only way it can break."""
    for candidate in (uuid.uuid4(), uuid.UUID(int=0), uuid.UUID(int=(1 << 128) - 1)):
        assert check_slug(str(candidate)) == str(candidate)


# --- the unhappy path ---------------------------------------------------------


def test_two_schema_invalid_answers_end_the_turn_as_a_typed_internal(turn_env: TurnEnv) -> None:
    """Catches a turn that raises the gateway's error at the caller instead of
    ending as state, and one that retries more than the one retry the design
    allows — a third staged request fails this case outright. Also catches a
    half-written turn: exactly one assistant row, carrying the templated
    reply, not one per attempt."""
    session_id = uuid.uuid4()
    gateway = StagedGateway(['{"intent": "nonsense"}', "still not the schema"])

    state = _run_turn(
        turn_env, turn_env.tenant_a, session_id=session_id, message="help me", gateway=gateway
    )

    rows = _stored_messages(turn_env, turn_env.tenant_a, session_id)
    assert state.outcome is TurnOutcome.FAILED
    assert state.error_code is ErrorCode.INTERNAL
    assert state.reply == FAILURE_REPLY
    assert gateway.call_count == 2
    assistant_rows = [row for row in rows if row.role == MessageRole.ASSISTANT.value]
    assert len(assistant_rows) == 1
    assert assistant_rows[0].content == FAILURE_REPLY


def test_a_confidence_below_the_gate_asks_the_clarifying_question(turn_env: TurnEnv) -> None:
    """Catches a clarify branch that does not actually END: only ONE model
    answer is staged, so a turn that went on to retrieval or a composed reply
    would make a second request and fail here. The required entity IS present
    and the intent is confident-looking otherwise, so deleting the confidence
    comparison alone flips this case."""
    session_id = uuid.uuid4()
    question = "Which system do you need access to?"
    gateway = StagedGateway(
        [
            _extraction_json(
                intent="request_access",
                confidence=0.4,
                entities={"system": "payroll"},
                clarifying_question=question,
            )
        ]
    )

    state = _run_turn(
        turn_env, turn_env.tenant_a, session_id=session_id, message="access thing", gateway=gateway
    )

    rows = _stored_messages(turn_env, turn_env.tenant_a, session_id)
    assert state.outcome is TurnOutcome.CLARIFY
    assert state.reply == question
    assert gateway.call_count == 1
    assert [row.role for row in rows] == [MessageRole.USER.value, MessageRole.ASSISTANT.value]
    assert rows[1].content == question


def test_two_matching_systems_ask_for_disambiguation(turn_env: TurnEnv) -> None:
    """Catches a resolver that picks the first of several candidates — SPEC
    §10.1's "never guess a system or role". Tenant C owns two systems sharing
    one display name, so the model's term matches both exactly and no tier
    ordering can separate them; the only correct answer is to ask."""
    session_id = uuid.uuid4()
    gateway = StagedGateway(
        [
            _extraction_json(
                intent="request_access", confidence=0.95, entities={"system": "Fake Billing"}
            )
        ]
    )

    state = _run_turn(
        turn_env,
        turn_env.tenant_c,
        session_id=session_id,
        message="billing access",
        gateway=gateway,
    )

    options = list(state.options)
    rows = _stored_messages(turn_env, turn_env.tenant_c, session_id)
    assert state.outcome is TurnOutcome.DISAMBIGUATE
    assert len(options) == 2
    assert len(options) <= MAX_DISAMBIGUATION_OPTIONS
    offered = " ".join(options)
    assert "billing-eu" in offered and "billing-us" in offered
    assert [row.role for row in rows] == [MessageRole.USER.value, MessageRole.ASSISTANT.value]


def test_a_candidate_in_another_tenant_is_not_a_candidate(turn_env: TurnEnv) -> None:
    """Catches plan §5's third risk: the candidate read issued on an unguarded
    session. Tenant B owns a system with the SAME slug as tenant A's, so an
    unscoped read returns two candidates and the turn would ask tenant A to
    choose between its own system and another company's — or resolve to the
    wrong id outright."""
    session_id = uuid.uuid4()
    gateway = StagedGateway([_payroll_answer(), EMPTY_PLAN, COMPOSED_REPLY])
    assert turn_env.tenant_b.system_ids["payroll"] != turn_env.tenant_a.system_ids["payroll"], (
        "premise: both tenants own a system slugged 'payroll', with different ids"
    )

    state = _run_turn(
        turn_env,
        turn_env.tenant_a,
        session_id=session_id,
        message="payroll access",
        gateway=gateway,
    )

    # The gateway is staged with all three answers, so the turn runs to the end
    # and its outcome is DONE, exactly as every other full-run case here. The
    # kill is the line below it: the system resolved is tenant A's own.
    assert state.outcome is TurnOutcome.DONE
    assert state.resolved is not None
    assert state.resolved.system_id == turn_env.tenant_a.system_ids["payroll"]


def test_every_row_written_carries_the_callers_tenant_and_user(turn_env: TurnEnv) -> None:
    """Catches rows stamped from something other than the caller: a session
    row that adopts the tenant's first user, or a message written outside the
    bound tenant. The caller is tenant A's SECOND user and the first user
    exists in the SAME tenant, so the tenant guard cannot
    answer this on the rule's behalf. No kill claim for "the identity comes
    from the token, not the arguments": `run_turn` receives both as arguments
    from the route that read the token, so that property has no seam here."""
    session_id = uuid.uuid4()
    gateway = StagedGateway([_payroll_answer(), EMPTY_PLAN, COMPOSED_REPLY])
    caller = turn_env.tenant_a.other_user_id

    _run_turn(
        turn_env,
        turn_env.tenant_a,
        session_id=session_id,
        message="payroll access",
        gateway=gateway,
        user_id=caller,
    )

    stored = _stored_session(turn_env, turn_env.tenant_a, session_id)
    assert stored == (turn_env.tenant_a.tenant_id, caller)
    rows = _stored_messages(turn_env, turn_env.tenant_a, session_id)
    assert rows
    assert {row.tenant_id for row in rows} == {turn_env.tenant_a.tenant_id}


# --- decision F ---------------------------------------------------------------


def test_run_turn_compiles_with_the_checkpointer_it_is_given() -> None:
    """An earlier change §8, the edit 17a's decision F asked for by name.

    Two halves, and each catches something the other cannot. The
    argument-free call catches a `build_graph` that reached for a saver of its
    own — a `MemorySaver` default is a second, in-process persistence story
    beside the conversation tables, and every node and turn case in this file
    would then be checkpointing into it silently.

    The passed-in half catches a `build_graph(checkpointer=...)` that accepts
    the argument and drops it: the compiled graph would run and pause exactly
    as before while writing nothing to Postgres, which is the failure
    `test_agent_approval_interrupt.py::test_the_paused_state_is_readable_from_a_second_checkpointer`
    sees from the other end. Identity, not truthiness — a graph carrying some
    other saver passes a `is not None` check."""
    saver = InMemorySaver()

    assert build_graph().checkpointer is None
    assert build_graph(checkpointer=saver).checkpointer is saver


# --- an earlier change: retrieval, its refusal, and the query cut ----------------------


def test_a_request_access_turn_calls_search_policy_exactly_once(turn_env: TurnEnv) -> None:
    """Catches a node that calls the tool per candidate or per history line, and
    one that carries the citations in a re-sorted order.

    The fused order is the tool's (15a's RRF); a node that re-ranked by `score`
    would put the second citation first here, because the payload is
    deliberately ASCENDING in score. Only the node's own pass-through can keep
    this order, so a sort added anywhere flips the case."""
    session_id = uuid.uuid4()
    gateway = StagedGateway([_payroll_answer(), EMPTY_PLAN, COMPOSED_REPLY])
    page = [
        _citation("fake-access-policy", score=0.11),
        _citation("fake-payroll-policy", score=0.9),
    ]
    tools = StubToolCaller(payload=_page(*page))

    state = _run_turn(
        turn_env,
        turn_env.tenant_a,
        session_id=session_id,
        message="I need payroll access",
        gateway=gateway,
        tools=tools,
    )

    assert tools.names == [SEARCH_POLICY]
    assert state.outcome is TurnOutcome.DONE
    assert list(state.citations) == [Citation.model_validate(item) for item in page]


def test_a_retrieving_turn_writes_exactly_one_assistant_message(turn_env: TurnEnv) -> None:
    """An earlier change §8, renamed from `..._writes_no_assistant_message`: the reply
    now exists, and there must be exactly ONE of it.

    Catches a `retrieve_policy` or an `execute_tools` that stores a reply of
    its own — the user is then answered twice for one question, and the second
    answer is the one the composition never saw — and an edge routing a
    successful retrieval into an ending node, which would store a templated
    reply instead of the composed one."""
    session_id = uuid.uuid4()
    gateway = StagedGateway([_payroll_answer(), EMPTY_PLAN, COMPOSED_REPLY])
    tools = StubToolCaller(payload=_page(_citation("fake-access-policy", score=0.8)))

    state = _run_turn(
        turn_env,
        turn_env.tenant_a,
        session_id=session_id,
        message="I need payroll access",
        gateway=gateway,
        tools=tools,
    )

    rows = _stored_messages(turn_env, turn_env.tenant_a, session_id)
    assert state.reply == COMPOSED_REPLY
    assert [row.role for row in rows] == [MessageRole.USER.value, MessageRole.ASSISTANT.value]
    assert rows[1].content == COMPOSED_REPLY


def test_an_ask_policy_turn_also_retrieves(turn_env: TurnEnv) -> None:
    """The SECOND of the design step 5's two intents. Catches a
    `should_retrieve` written for `request_access` alone: `ask_policy` names no
    system and resolves no entity, so nothing else in the turn would notice
    that the one intent whose whole purpose is the corpus never reads it."""
    session_id = uuid.uuid4()
    gateway = StagedGateway(
        [_extraction_json(intent="ask_policy", confidence=0.95), COMPOSED_REPLY]
    )
    tools = StubToolCaller(payload=_page(_citation("fake-expenses-policy", score=0.7)))

    state = _run_turn(
        turn_env,
        turn_env.tenant_a,
        session_id=session_id,
        message="what is the expenses policy",
        gateway=gateway,
        tools=tools,
    )

    assert tools.names == [SEARCH_POLICY]
    assert state.outcome is TurnOutcome.DONE
    assert len(state.citations) == 1


def test_a_reset_password_turn_makes_no_tool_call_and_carries_no_citations(
    turn_env: TurnEnv,
) -> None:
    """Criterion 4's other half, one of the eleven intents §10.1 step 5 does not
    name. Catches a node that retrieves unconditionally: the turn would pay an
    embedding and a vector search for a password reset, and would carry policy
    chunks into 17c's planning for a request nobody asked a policy question
    about. The stub would ANSWER such a call, so only the count can see it.

    An earlier change §8: `reset_password` has a non-empty `INTENT_TOOLS` row, so the
    turn now plans and a second answer is staged.

    An earlier change §8: execution exists from this plan on, so the assertion is that
    no `search_policy` call was made rather than that no call was made at all.
    The planner proposes nothing here, so the count would agree either way;
    the citation half does not move, and it is the half that matters — a
    retrieval on this intent would carry policy chunks into the composition
    for a request nobody asked a policy question about."""
    session_id = uuid.uuid4()
    gateway = StagedGateway(
        [
            _extraction_json(intent="reset_password", confidence=0.95),
            EMPTY_PLAN,
            COMPOSED_REPLY,
        ]
    )
    tools = StubToolCaller(payload=_page(_citation("fake-password-policy", score=0.9)))

    state = _run_turn(
        turn_env,
        turn_env.tenant_a,
        session_id=session_id,
        message="I am locked out and need my password reset",
        gateway=gateway,
        tools=tools,
    )

    assert SEARCH_POLICY not in tools.names
    assert state.outcome is TurnOutcome.DONE
    assert state.citations == ()


def test_a_refused_page_ends_on_the_templated_refusal_and_creates_no_ticket(
    turn_env: TurnEnv,
) -> None:
    """Criterion 6, all four halves. Catches a turn that ignores `refused` and
    hands 17c an empty citation list as though it were an answer; one that
    stores citations alongside the refusal, which is how chunks the threshold
    rejected reach a reply; and — decision E — one that OPENS the ticket rather
    than offering to. The stub records every call by name, so a `create_ticket`
    is visible whether or not it succeeded.

    The refusal reply is asserted as an exact string: it is the fixed template,
    never the model's words."""
    session_id = uuid.uuid4()
    gateway = StagedGateway([_extraction_json(intent="ask_policy", confidence=0.95)])
    tools = StubToolCaller(payload=_page(refused=True))

    state = _run_turn(
        turn_env,
        turn_env.tenant_a,
        session_id=session_id,
        message="what is the policy on renting a helicopter",
        gateway=gateway,
        tools=tools,
    )

    rows = _stored_messages(turn_env, turn_env.tenant_a, session_id)
    assistant_rows = [row for row in rows if row.role == MessageRole.ASSISTANT.value]
    assert state.outcome is TurnOutcome.REFUSE
    assert state.citations == ()
    assert state.reply == REFUSAL_REPLY
    assert len(assistant_rows) == 1
    assert assistant_rows[0].content == REFUSAL_REPLY
    assert tools.names == [SEARCH_POLICY]
    assert "create_ticket" not in tools.names


def test_the_refusal_reply_is_a_fixed_string_offering_a_person() -> None:
    """a project rule on a message rather than a number, and decision E in the
    words the user reads. Catches a `REFUSAL_REPLY` that promises a ticket has
    been opened ("I have raised a ticket"), which is a mutation the user did not
    request and one this plan never performs — the turn would be lying about an
    effect. Kept as a separate case from the turn above because that one pins
    WHICH string is stored, and this one pins what the string may say."""
    assert isinstance(REFUSAL_REPLY, str)
    assert REFUSAL_REPLY.strip() == REFUSAL_REPLY
    assert REFUSAL_REPLY != FAILURE_REPLY
    lowered = REFUSAL_REPLY.lower()
    assert "ticket" in lowered
    assert "policy" in lowered


def test_a_tool_error_ends_the_turn_through_fail_turn(turn_env: TurnEnv) -> None:
    """Criterion 9. Catches a `retrieve_policy` that lets the caller's
    `ToolError` escape — `run_turn` would raise where every other failure comes
    back as state, so the route would have an exception to translate — and one
    that swallows it into `CONTINUE`, which sends 17c to plan tools against a
    corpus that answered nothing.

    Exactly one assistant row proves there is no half-written state: the user
    row plus one templated reply, not one row per attempt."""
    session_id = uuid.uuid4()
    gateway = StagedGateway([_extraction_json(intent="ask_policy", confidence=0.95)])
    tools = StubToolCaller(error=RemoteInternalError("the policy service could not be reached"))

    state = _run_turn(
        turn_env,
        turn_env.tenant_a,
        session_id=session_id,
        message="what is the travel policy",
        gateway=gateway,
        tools=tools,
    )

    rows = _stored_messages(turn_env, turn_env.tenant_a, session_id)
    assistant_rows = [row for row in rows if row.role == MessageRole.ASSISTANT.value]
    assert state.outcome is TurnOutcome.FAILED
    assert state.error_code is ErrorCode.INTERNAL
    assert state.reply == FAILURE_REPLY
    assert state.citations == ()
    assert len(assistant_rows) == 1
    assert assistant_rows[0].content == FAILURE_REPLY


# --- decision G: the query cut ------------------------------------------------


def test_the_query_cap_is_exactly_the_planned_value() -> None:
    """a project rule. Catches `MAX_QUERY_CHARS` set to `MAX_MESSAGE_CHARS`,
    which would make decision G a no-op, and a cap so small that a real
    question is cut mid-sentence. The two caps are asserted to differ, because
    decision G only exists where they do."""
    assert MAX_QUERY_CHARS == 1000
    assert MAX_QUERY_CHARS < MAX_MESSAGE_CHARS


@pytest.mark.parametrize(
    ("length", "expected"),
    [
        (0, 0),
        (999, 999),
        (MAX_QUERY_CHARS, MAX_QUERY_CHARS),
        (MAX_QUERY_CHARS + 1, MAX_QUERY_CHARS),
    ],
)
def test_query_for_cuts_at_the_cap_inclusively(length: int, expected: int) -> None:
    """The inclusive boundary as a pure call. Catches an off-by-one written as
    `message[: MAX_QUERY_CHARS - 1]`, which shortens every long query by a
    character for no reason, and a cut written as a REFUSAL, which decision G
    rejects: a 4000-character message is legal by construction, so refusing it
    would make two of our own caps contradict each other."""
    assert len(query_for("x" * length)) == expected


def test_a_message_at_the_query_cap_is_sent_whole(turn_env: TurnEnv) -> None:
    """The same boundary through the real turn, which the pure call cannot
    reach: catches a node that cuts twice, or that sends `state.user_message`
    unchanged and lets the SERVER's `check_query` refuse it. Criterion 5 says
    neither length refuses the turn, so the outcome is asserted too."""
    session_id = uuid.uuid4()
    gateway = StagedGateway([_payroll_answer(), EMPTY_PLAN, COMPOSED_REPLY])
    tools = StubToolCaller()
    message = "x" * MAX_QUERY_CHARS

    state = _run_turn(
        turn_env,
        turn_env.tenant_a,
        session_id=session_id,
        message=message,
        gateway=gateway,
        tools=tools,
    )

    assert len(tools.calls) == 1
    assert tools.calls[0][1]["query"] == message
    assert state.outcome is TurnOutcome.DONE


def test_a_message_one_character_over_the_query_cap_is_cut_not_refused(
    turn_env: TurnEnv,
) -> None:
    """The exclusive half, and decision G's whole point. Catches a node that
    answers an over-long QUERY with `VALIDATION`: the message is well under
    `MAX_MESSAGE_CHARS`, so the user was told it was allowed, and the turn must
    still resolve. The stored message keeps every character — the cut is on the
    query only — which is what the row-length assertion pins."""
    session_id = uuid.uuid4()
    gateway = StagedGateway([_payroll_answer(), EMPTY_PLAN, COMPOSED_REPLY])
    tools = StubToolCaller()
    message = "y" * (MAX_QUERY_CHARS + 1)

    state = _run_turn(
        turn_env,
        turn_env.tenant_a,
        session_id=session_id,
        message=message,
        gateway=gateway,
        tools=tools,
    )

    rows = _stored_messages(turn_env, turn_env.tenant_a, session_id)
    assert len(tools.calls) == 1
    assert tools.calls[0][1]["query"] == "y" * MAX_QUERY_CHARS
    assert state.outcome is TurnOutcome.DONE
    assert len(rows[0].content) == MAX_QUERY_CHARS + 1


# --- decision B: the session is opened and closed by `run_turn` ---------------


def test_run_turn_opens_and_closes_the_caller_exactly_once(turn_env: TurnEnv) -> None:
    """Decision B as a count. Catches a session opened per NODE (17c makes up
    to six calls and would pay six handshakes) and one opened per call inside
    `McpToolCaller`, which is the same cost wearing a different hat. Also
    catches a `run_turn` that never enters the caller at all — the stub answers
    regardless, so nothing else in the turn would notice."""
    session_id = uuid.uuid4()
    gateway = StagedGateway([_payroll_answer(), EMPTY_PLAN, COMPOSED_REPLY])
    tools = StubToolCaller()

    _run_turn(
        turn_env,
        turn_env.tenant_a,
        session_id=session_id,
        message="I need payroll access",
        gateway=gateway,
        tools=tools,
    )

    assert (tools.entered, tools.exited) == (1, 1)


def test_the_caller_is_closed_when_a_node_raises(turn_env: TurnEnv) -> None:
    """Plan §5's fourth risk, from the graph's side. A `RuntimeError` is used
    deliberately: `retrieve_policy` converts a `ToolError` to state, so only a
    non-`ToolError` still escapes the graph, and that is the exact path on which
    a `finally`-less `run_turn` would leak the turn's MCP session. Catches an
    implementation that closes the caller after `ainvoke` returns rather than
    with `async with deps.tools:`."""
    session_id = uuid.uuid4()
    gateway = StagedGateway([_extraction_json(intent="ask_policy", confidence=0.95)])
    tools = StubToolCaller(error=RuntimeError("the stub caller exploded"))

    with pytest.raises(RuntimeError):
        _run_turn(
            turn_env,
            turn_env.tenant_a,
            session_id=session_id,
            message="what is the travel policy",
            gateway=gateway,
            tools=tools,
        )

    assert (tools.entered, tools.exited) == (1, 1)


def test_the_pre_checks_still_run_before_the_caller_is_opened(turn_env: TurnEnv) -> None:
    """a project rule, with a new effect to guard. Catches a `run_turn` that
    opens the MCP session BEFORE `check_message_content`: an over-long message
    would then pay a network handshake, and the "no effect before the check"
    property this file already pins for the database would hold for rows and
    not for sockets. The staged gateway has no answers, so a model call fails
    the case too."""
    session_id = uuid.uuid4()
    gateway = StagedGateway([])
    tools = StubToolCaller()

    with pytest.raises(MessageTooLongError):
        _run_turn(
            turn_env,
            turn_env.tenant_a,
            session_id=session_id,
            message="x" * (MAX_MESSAGE_CHARS + 1),
            gateway=gateway,
            tools=tools,
        )

    assert (tools.entered, tools.exited) == (0, 0)
    assert tools.calls == []


# --- an earlier change: planning, validation and loop control --------------------------


def _planning_state(
    *,
    intent: Intent = Intent.REQUEST_ACCESS,
    tool_calls_made: int = 0,
    tokens_used: int = 0,
) -> TurnState:
    """A turn standing where `retrieve_policy` hands over, with no graph."""
    return TurnState(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        tenant_slug=TENANT_A,
        user_message="I need payroll access",
        extraction=IntentExtraction(
            intent=intent,
            entities=IntentEntities(
                system="payroll",
                role=None,
                target_user=None,
                department=None,
                expiry_days=None,
                justification=None,
            ),
            confidence=0.95,
            clarifying_question=None,
        ),
        started_at=NOW,
        tool_calls_made=tool_calls_made,
        tokens_used=tokens_used,
    )


def _run_plan_tools(state: TurnState, gateway: StagedGateway) -> TurnState:
    """`plan_tools` alone, with no database and no graph.

    The node is handed a real `ModelClient` over the staged transport and a
    `None` session behind a cast: it writes no row, so an implementation that
    reached for `deps.db` raises `AttributeError` here rather than quietly
    adding a database to the loop-control path.
    """

    async def scenario() -> TurnState:
        client = ModelClient(
            base_url=GATEWAY_URL,
            api_keys=API_KEYS,
            transport=httpx.MockTransport(gateway.handler),
        )
        try:
            deps = TurnDeps(
                db=cast(AsyncSession, None),
                model=client,
                tools=StubToolCaller(),
                now=lambda: NOW,
            )
            return await plan_tools(state, deps)
        finally:
            await client.aclose()

    return asyncio.run(scenario())


def test_a_request_access_turn_plans_executes_replans_and_composes(
    turn_env: TurnEnv,
) -> None:
    """An earlier change criteria 1, 4 and 7, and an earlier change §8: this case used to end at
    the validation handover with nothing executed and nothing said.

    The cycle is what it pins. Four answers are staged — the classification,
    the round that proposes one call, the empty round that follows it and the
    composition — so a graph whose `execute_tools` edges to `END` instead of
    back to `plan_tools` never reads the third or fourth, and the turn ends
    with no reply. A graph that replans without executing leaves the tool
    caller with only the retrieval on it.

    Exactly one assistant row: the composed reply, written once. A turn that
    stored a reply per round would show two, and one that answered from the
    ending nodes would store the template instead."""
    session_id = uuid.uuid4()
    gateway = StagedGateway(
        [_payroll_answer(), _plan_json(CLASSIFY_PROPOSAL), EMPTY_PLAN, COMPOSED_REPLY]
    )
    tools = StubToolCaller(payload=_page(_citation("fake-access-policy", score=0.8)))

    state = _run_turn(
        turn_env,
        turn_env.tenant_a,
        session_id=session_id,
        message="I need payroll access",
        gateway=gateway,
        tools=tools,
    )

    rows = _stored_messages(turn_env, turn_env.tenant_a, session_id)
    assistant_rows = [row for row in rows if row.role == MessageRole.ASSISTANT.value]
    assert gateway.call_count == 4
    assert state.outcome is TurnOutcome.DONE
    assert tools.names == [SEARCH_POLICY, "classify_access_request"]
    assert [result.tool for result in state.tool_results] == ["classify_access_request"]
    assert state.tool_results[0].ok is True
    assert state.tool_calls_made == 1
    assert state.reply == COMPOSED_REPLY
    assert len(assistant_rows) == 1
    assert assistant_rows[0].content == COMPOSED_REPLY


def test_the_planner_asks_the_strong_alias_and_the_classifier_the_default_one(
    turn_env: TurnEnv,
) -> None:
    """Decision G, and the only case that can see it: both calls succeed
    whichever alias is named, and no state field records which was used.
    Catches a planner copied from `extract_intent` and left on
    `agent-default` — choosing which mutation to call is the riskiest
    judgment in the turn, and the cheap alias is the one that classifies into
    a closed label set.

    An earlier change adds the third: decision D puts the composition back on
    `agent-default`, which is the whole of that decision and is invisible
    anywhere else. The list is asserted in ORDER, so a composition left on the
    strong alias — paid on every turn that gets this far — fails here."""
    gateway = StagedGateway(
        [_payroll_answer(), _plan_json(CLASSIFY_PROPOSAL), EMPTY_PLAN, COMPOSED_REPLY]
    )

    _run_turn(
        turn_env,
        turn_env.tenant_a,
        session_id=uuid.uuid4(),
        message="I need payroll access",
        gateway=gateway,
        tools=StubToolCaller(payload=_page(_citation("fake-access-policy", score=0.8))),
    )

    assert [_model_of(request) for request in gateway.requests] == [
        ModelAlias.AGENT_DEFAULT.value,
        ModelAlias.AGENT_STRONG.value,
        ModelAlias.AGENT_STRONG.value,
        ModelAlias.AGENT_DEFAULT.value,
    ]


def test_the_planning_call_quotes_the_citations_as_data_and_not_as_instructions(
    turn_env: TurnEnv,
) -> None:
    """the design, criterion 3's "AS DATA" half. Retrieved policy text is
    somebody else's writing; merged into the system prompt it becomes an
    instruction the model obeys, which is the prompt-injection path straight
    to a mutation. Catches a planner that formats the citations into its
    system message: every system message must be a prompt file this repo
    ships, and the citation text must appear in a `user` message."""
    citation = _citation("fake-access-policy", score=0.8)
    gateway = StagedGateway([_payroll_answer(), EMPTY_PLAN, COMPOSED_REPLY])

    _run_turn(
        turn_env,
        turn_env.tenant_a,
        session_id=uuid.uuid4(),
        message="I need payroll access",
        gateway=gateway,
        tools=StubToolCaller(payload=_page(citation)),
    )

    messages = _messages_of(gateway.requests[1])
    shipped = {load_prompt("system").text, load_prompt(PLANNING_PROMPT).text}
    system_content = [item["content"] for item in messages if item["role"] == "system"]
    user_content = " ".join(item["content"] for item in messages if item["role"] == "user")

    assert system_content
    assert set(system_content) <= shipped
    assert citation["text"] in user_content


def test_an_ask_policy_turn_makes_no_planning_call_at_all(turn_env: TurnEnv) -> None:
    """Criterion 4. `ask_policy`'s row is empty, so the round is decided by a
    pure predicate BEFORE the gateway is touched. Catches a planner that calls
    the model first and filters its proposals afterwards: only ONE answer is
    staged, so a second request fails the case outright — and the turn would
    otherwise pay a strong-alias call per policy question to be told there was
    nothing to call.

    An earlier change §8: the second staged answer is the COMPOSITION, so the hard stop
    still holds — a planner that called the model would take the composition's
    slot and the turn would then ask for a third answer that is not there."""
    session_id = uuid.uuid4()
    gateway = StagedGateway(
        [_extraction_json(intent="ask_policy", confidence=0.95), COMPOSED_REPLY]
    )
    tools = StubToolCaller(payload=_page(_citation("fake-expenses-policy", score=0.7)))

    state = _run_turn(
        turn_env,
        turn_env.tenant_a,
        session_id=session_id,
        message="what is the expenses policy",
        gateway=gateway,
        tools=tools,
    )

    assert gateway.call_count == 2
    assert state.outcome is TurnOutcome.DONE
    assert state.proposed == ()
    assert state.validated == ()
    assert state.reply == COMPOSED_REPLY


def test_a_rejected_round_ends_on_the_refusal_with_exactly_one_assistant_message(
    turn_env: TurnEnv,
) -> None:
    """Plan §5's fifth risk, end to end. The proposal is a catalogued tool
    outside `request_access`'s row, so only the row check refuses it.

    Catches a rejected round that ends the turn SILENTLY — the user would see
    nothing at all — and one that runs its proposals anyway, which the stub
    caller reports by name. Exactly one assistant row proves the refusal was
    stored once, not once per proposal."""
    session_id = uuid.uuid4()
    gateway = StagedGateway(
        [
            _payroll_answer(),
            _plan_json(_proposal("offboard_user", user_id=TARGET_USER_ID, end_date="2026-07-01")),
        ]
    )
    tools = StubToolCaller(payload=_page(_citation("fake-access-policy", score=0.8)))

    state = _run_turn(
        turn_env,
        turn_env.tenant_a,
        session_id=session_id,
        message="I need payroll access",
        gateway=gateway,
        tools=tools,
    )

    rows = _stored_messages(turn_env, turn_env.tenant_a, session_id)
    assistant_rows = [row for row in rows if row.role == MessageRole.ASSISTANT.value]
    assert state.outcome is TurnOutcome.REFUSE
    assert state.validated == ()
    assert state.reply == REFUSAL_REPLY
    assert len(assistant_rows) == 1
    assert assistant_rows[0].content == REFUSAL_REPLY
    assert tools.names == [SEARCH_POLICY]


def test_a_gateway_failure_in_planning_ends_the_turn_through_fail_turn(
    turn_env: TurnEnv,
) -> None:
    """Criterion 9. Two schema-invalid planning answers exhaust
    `complete_json`'s single retry, so the node meets a `ToolError`.

    Catches a `plan_tools` that lets it escape — `run_turn` would raise where
    every other failure comes back as state — and one that swallows it into
    `CONTINUE`, handing 17d a turn with no plan and no error. Nothing may
    survive on the state: a half-parsed proposal kept from the first attempt
    is exactly what 17d would then execute."""
    session_id = uuid.uuid4()
    gateway = StagedGateway([_payroll_answer(), '{"calls": "not a list"}', "not json at all"])
    tools = StubToolCaller(payload=_page(_citation("fake-access-policy", score=0.8)))

    state = _run_turn(
        turn_env,
        turn_env.tenant_a,
        session_id=session_id,
        message="I need payroll access",
        gateway=gateway,
        tools=tools,
    )

    rows = _stored_messages(turn_env, turn_env.tenant_a, session_id)
    assistant_rows = [row for row in rows if row.role == MessageRole.ASSISTANT.value]
    assert gateway.call_count == 3
    assert state.outcome is TurnOutcome.FAILED
    assert state.error_code is ErrorCode.INTERNAL
    assert state.reply == FAILURE_REPLY
    assert state.proposed == ()
    assert state.validated == ()
    assert len(assistant_rows) == 1
    assert assistant_rows[0].content == FAILURE_REPLY
    assert tools.names == [SEARCH_POLICY]


def test_run_turn_stamps_started_at_from_the_injected_clock(turn_env: TurnEnv) -> None:
    """The deadline is measured from this field, so a turn that never sets it
    has no deadline at all. Catches a `started_at` left `None` (the elapsed
    computation then has nothing to subtract) and one read from
    `datetime.now()` inside a node rather than from `deps.now()`, which no
    test could control and which would restart the clock on every round."""
    state = _run_turn(
        turn_env,
        turn_env.tenant_a,
        session_id=uuid.uuid4(),
        message="I need payroll access",
        gateway=StagedGateway([_payroll_answer(), EMPTY_PLAN, COMPOSED_REPLY]),
    )

    assert state.started_at == NOW


def test_the_planning_prompt_version_rides_on_its_own_state_field(turn_env: TurnEnv) -> None:
    """Criterion 8. One string cannot carry two versions: catches a
    `plan_tools` that overwrites `prompt_version` with its own digest, which
    would make 17b's assertion false and every trace name the wrong prompt for
    the classification. The two digests are asserted to DIFFER, because a
    planner recording the extraction prompt's digest would satisfy both
    equalities otherwise."""
    state = _run_turn(
        turn_env,
        turn_env.tenant_a,
        session_id=uuid.uuid4(),
        message="I need payroll access",
        gateway=StagedGateway([_payroll_answer(), EMPTY_PLAN, COMPOSED_REPLY]),
    )

    extraction_digest = load_prompt("intent_extraction").sha256
    planning_digest = load_prompt(PLANNING_PROMPT).sha256
    assert state.prompt_version == extraction_digest
    assert state.planning_prompt_version == planning_digest
    assert extraction_digest != planning_digest


def test_a_turn_past_the_call_budget_proposes_nothing_and_never_reaches_the_gateway() -> None:
    """Criterion 7 through the node rather than the predicate: `run_turn`
    always starts a turn at zero calls, so the only way to stand past the
    budget is to hand `plan_tools` the state 17d's loop would hand it.

    The staged gateway has NO answers, so a planner that calls the model and
    checks the budget afterwards fails outright — which is the point: the
    budget exists to stop paying for a strong-alias call, not to discard its
    answer. The other two counters are at zero, so only the call budget can
    decide this case.

    Seven, not six: criterion 7 makes every bound INCLUSIVE, so exactly six
    executed calls may still plan. The plan's §6 line reading "a turn already
    at six executed calls proposes nothing" contradicts its own §1 and its own
    gates row; §1 wins."""
    gateway = StagedGateway([])

    state = _run_plan_tools(
        _planning_state(tool_calls_made=MAX_TOOL_CALLS + 1),
        gateway,
    )

    assert gateway.call_count == 0
    assert state.outcome is TurnOutcome.REFUSE
    assert state.proposed == ()
    assert state.validated == ()


def test_a_turn_exactly_at_the_call_budget_still_plans() -> None:
    """The inclusive half of the same bound, which the case above cannot
    reach: catches a `>=` comparison in the node's own budget read, which
    would refuse the last round the plan allows and end a turn one call short
    of its work with the refusal template."""
    gateway = StagedGateway([_plan_json(CLASSIFY_PROPOSAL)])

    state = _run_plan_tools(
        _planning_state(tool_calls_made=MAX_TOOL_CALLS),
        gateway,
    )

    assert gateway.call_count == 1
    assert state.outcome is TurnOutcome.CONTINUE
    assert [call.tool for call in state.proposed] == ["classify_access_request"]


def test_a_round_over_the_per_round_cap_is_refused_whole() -> None:
    """`MAX_CALLS_PER_ROUND` as a refusal rather than a trim, and a case the
    plan's §6 does not list — added with the implementation, recorded in the
    plan's Deviations.

    Catches a planner that keeps the first three of an over-long round: those
    three are a plan the model did not make, and executing them is decision
    C's failure by another route. Every one of the four proposals here is
    legal on its own, so only the count can decide the case, and the zero is
    what separates refusing from trimming."""
    over_the_cap = [CLASSIFY_PROPOSAL] * (MAX_CALLS_PER_ROUND + 1)
    gateway = StagedGateway([_plan_json(*over_the_cap)])

    state = _run_plan_tools(_planning_state(), gateway)

    assert gateway.call_count == 1
    assert state.outcome is TurnOutcome.REFUSE
    assert state.proposed == ()


# --- an earlier change: the cycle, its three stops and the counters --------------------


def test_a_turn_driven_to_the_call_budget_composes_and_never_trips_the_limit(
    turn_env: TurnEnv,
) -> None:
    """Plan §5's second risk, and decision G's whole reason. Six rounds of one
    call each is about twenty-five node visits with the four leading nodes and
    the composition, so LangGraph's DEFAULT recursion limit of 25 aborts this
    turn with a `GraphRecursionError` — a turn every budget in this plan
    permits, failing on a library default nobody chose.

    Catches that default being left in place. It also catches an
    `execute_tools` that counts successes rather than attempts (decision F):
    the budget would never be reached, the planner would be asked for an
    eighth round, and the staged answers would run out.

    Six calls is the INCLUSIVE bound, so the seventh planning round still
    happens and is the empty one that ends the turn."""
    session_id = uuid.uuid4()
    rounds = [_plan_json(CLASSIFY_PROPOSAL)] * MAX_TOOL_CALLS
    gateway = StagedGateway([_payroll_answer(), *rounds, EMPTY_PLAN, COMPOSED_REPLY])
    tools = StubToolCaller(payload=_page(_citation("fake-access-policy", score=0.8)))

    state = _run_turn(
        turn_env,
        turn_env.tenant_a,
        session_id=session_id,
        message="I need payroll access",
        gateway=gateway,
        tools=tools,
    )

    rows = _stored_messages(turn_env, turn_env.tenant_a, session_id)
    assistant_rows = [row for row in rows if row.role == MessageRole.ASSISTANT.value]
    assert gateway.call_count == MAX_TOOL_CALLS + 3
    assert state.tool_calls_made == MAX_TOOL_CALLS
    assert len(state.tool_results) == MAX_TOOL_CALLS
    assert tools.names.count("classify_access_request") == MAX_TOOL_CALLS
    assert state.outcome is TurnOutcome.DONE
    assert state.reply == COMPOSED_REPLY
    assert len(assistant_rows) == 1


def test_a_spent_budget_with_no_tool_results_ends_on_the_refusal_template(
    turn_env: TurnEnv,
) -> None:
    """Criterion 5's first half, through the whole graph. The extraction alone
    bills one token past `MAX_TURN_TOKENS`, so the turn stands past a budget
    having called nothing — the state 17c's node test could only build by
    hand.

    Catches an `exhausted_outcome` wired to compose whatever happened: this
    turn has no tool result and no citation worth composing, so the model
    would be asked to write a reply out of nothing. It also catches a
    `tokens_used` that never counts the EXTRACTION's spend — with the counter
    at zero the planner would plan, ask for an answer that is not staged, and
    fail the case."""
    session_id = uuid.uuid4()
    gateway = StagedGateway([StagedAnswer(_payroll_answer(), total_tokens=MAX_TURN_TOKENS + 1)])
    tools = StubToolCaller(payload=_page(_citation("fake-access-policy", score=0.8)))

    state = _run_turn(
        turn_env,
        turn_env.tenant_a,
        session_id=session_id,
        message="I need payroll access",
        gateway=gateway,
        tools=tools,
    )

    rows = _stored_messages(turn_env, turn_env.tenant_a, session_id)
    assistant_rows = [row for row in rows if row.role == MessageRole.ASSISTANT.value]
    assert gateway.call_count == 1
    assert state.outcome is TurnOutcome.REFUSE
    assert state.tool_results == ()
    assert state.reply == REFUSAL_REPLY
    assert len(assistant_rows) == 1
    assert assistant_rows[0].content == REFUSAL_REPLY


def test_a_spent_budget_with_tool_results_composes_instead_of_refusing(
    turn_env: TurnEnv,
) -> None:
    """Criterion 5's second half, and decision C in the words a person reads.
    The planning round bills past the budget, so the turn's SECOND planning
    round is refused — but by then a call has been made and its result is on
    the state.

    Catches the design followed literally: `refuse_and_ticket` would answer
    the person whose access request was just filed with "I could not find
    anything in the current policy documents that covers this", which is a
    false statement about a mutation the same turn performed. Only the
    presence of a tool result separates this case from the one above, so
    deleting the `tool_results` half of the predicate flips exactly this
    one."""
    session_id = uuid.uuid4()
    gateway = StagedGateway(
        [
            _payroll_answer(),
            StagedAnswer(_plan_json(CLASSIFY_PROPOSAL), total_tokens=MAX_TURN_TOKENS + 1),
            COMPOSED_REPLY,
        ]
    )
    tools = StubToolCaller(payload=_page(_citation("fake-access-policy", score=0.8)))

    state = _run_turn(
        turn_env,
        turn_env.tenant_a,
        session_id=session_id,
        message="I need payroll access",
        gateway=gateway,
        tools=tools,
    )

    rows = _stored_messages(turn_env, turn_env.tenant_a, session_id)
    assistant_rows = [row for row in rows if row.role == MessageRole.ASSISTANT.value]
    assert gateway.call_count == 3
    assert state.tool_calls_made == 1
    assert [result.tool for result in state.tool_results] == ["classify_access_request"]
    assert state.outcome is TurnOutcome.DONE
    assert state.reply == COMPOSED_REPLY
    assert len(assistant_rows) == 1
    assert assistant_rows[0].content == COMPOSED_REPLY
    assert REFUSAL_REPLY not in [row.content for row in rows]


def test_tokens_used_is_the_sum_of_every_staged_answers_total_tokens(
    turn_env: TurnEnv,
) -> None:
    """Criterion 8, and plan §5's fifth risk: counted twice, or not at all,
    once three call sites feed one counter.

    The three answers bill three DIFFERENT numbers, so every wrong
    implementation lands on a different total: a counter fed only by the
    extraction reads 11, one fed only by the planner 13, one that forgot the
    composition 24, and one that added the same answer twice cannot reach 41
    either. An earlier change's deviation — "`tokens_used` is present and always 0" —
    is closed by this row, which is why the assertion is an equality and not
    "greater than zero"."""
    session_id = uuid.uuid4()
    gateway = StagedGateway(
        [
            StagedAnswer(_payroll_answer(), total_tokens=11),
            StagedAnswer(EMPTY_PLAN, total_tokens=13),
            StagedAnswer(COMPOSED_REPLY, total_tokens=17),
        ]
    )

    state = _run_turn(
        turn_env,
        turn_env.tenant_a,
        session_id=session_id,
        message="I need payroll access",
        gateway=gateway,
    )

    assert gateway.call_count == 3
    assert state.tokens_used == 11 + 13 + 17
    assert state.tokens_used == gateway.staged_tokens


def test_the_recursion_limit_is_exactly_fifty_and_is_what_run_turn_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """a project rule on decision G's number, from both sides: the constant's
    value, and the fact that the invocation actually carries it.

    Catches a `GRAPH_RECURSION_LIMIT` declared and never passed — the graph
    would run under LangGraph's default 25 while a constant in the source said
    otherwise, which is the failure mode the six-round case above can only
    report as a mystery. It also catches a config REPLACED rather than
    extended: `deps` must still ride on it, and a turn whose deps went missing
    fails at the first node.

    No graph is compiled and no row is written: the compiled object is
    replaced by a recorder, which is the only way to read the config
    `ainvoke` was handed."""

    class RecordingGraph:
        def __init__(self) -> None:
            self.configs: list[dict[str, Any]] = []

        async def ainvoke(
            self, state: TurnState, config: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            self.configs.append({} if config is None else config)
            return state.model_dump()

    recorder = RecordingGraph()
    # `build_graph` takes the checkpointer `run_turn` hands it, so the stub has
    # to accept it: a zero-argument lambda raised TypeError before any config
    # was recorded.
    monkeypatch.setattr(build_module, "build_graph", lambda checkpointer=None: recorder)
    tools = StubToolCaller()

    async def scenario() -> TurnState:
        client = ModelClient(
            base_url=GATEWAY_URL,
            api_keys=API_KEYS,
            transport=httpx.MockTransport(StagedGateway([]).handler),
        )
        try:
            deps = TurnDeps(db=cast(AsyncSession, None), model=client, tools=tools, now=lambda: NOW)
            return await run_turn(
                deps=deps,
                tenant_id=uuid.uuid4(),
                user_id=uuid.uuid4(),
                session_id=uuid.uuid4(),
                tenant_slug=TENANT_A,
                message="I need payroll access",
            )
        finally:
            await client.aclose()

    asyncio.run(scenario())

    assert GRAPH_RECURSION_LIMIT == 50
    assert len(recorder.configs) == 1
    assert recorder.configs[0]["recursion_limit"] == GRAPH_RECURSION_LIMIT
    assert "deps" in recorder.configs[0]["configurable"]


# --- an earlier change: strict structured outputs ---------------------------------------


def _schema_request(schema: dict[str, Any]) -> httpx.Request:
    """A structured-output request in the shape `complete_json` posts."""
    return httpx.Request(
        "POST",
        f"{GATEWAY_URL}/chat/completions",
        json={
            "model": ModelAlias.AGENT_STRONG.value,
            "messages": [{"role": "user", "content": "I need payroll access"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "Plan", "schema": schema, "strict": True},
            },
        },
    )


def _strict_object(*, note: dict[str, Any]) -> dict[str, Any]:
    """One object schema meeting strict mode except for whatever `note` carries."""
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {"tool": {"type": "string"}, "note": note},
        "required": ["tool", "note"],
    }


def test_the_staged_gateway_refuses_a_non_strict_schema() -> None:
    """An earlier change criterion: the fake gateway answers 400 to a schema OpenAI's
    strict mode would refuse, so a non-strict schema cannot pass this suite
    while the real gateway turns every chat away.

    Catches a `StagedGateway.handler` that serves its staged answer whatever
    schema it is sent. The two requests differ ONLY in the `default` key on
    one property, and two answers are staged, so the 200 proves the check
    does not refuse a legal schema and only that key can decide the 400."""
    gateway = StagedGateway([EMPTY_PLAN, EMPTY_PLAN])
    nullable: dict[str, Any] = {"anyOf": [{"type": "string"}, {"type": "null"}]}

    accepted = gateway.handler(_schema_request(_strict_object(note=nullable)))
    refused = gateway.handler(_schema_request(_strict_object(note={**nullable, "default": None})))

    assert accepted.status_code == 200
    assert refused.status_code == 400


def test_a_planner_answer_with_non_object_arguments_is_retried_once() -> None:
    """Decision A's unhappy path through the node: an `arguments` string that
    decodes to an array must fail inside `complete_json`, where the single
    retry runs, and the second (legal) answer becomes the round.

    Catches a `PlannedCallAnswer` validator that decodes without checking for
    an object: `[1]` would validate, no retry would run, and mapping it onto
    `ProposedCall` would fail outside the client, ending the turn instead.
    Two answers staged, one bad plus one retry, so `call_count` is 2 and a
    third request fails the test at the gateway.

    The extraction names every entity field, absent ones as `None`, so the
    state builds both before and after an earlier change drops the defaults."""
    arguments = {"target_user_id": TARGET_USER_ID, "role_id": ROLE_ID}
    bad = json.dumps({"calls": [{"tool": "classify_access_request", "arguments": "[1]"}]})
    good = json.dumps(
        {"calls": [{"tool": "classify_access_request", "arguments": json.dumps(arguments)}]}
    )
    gateway = StagedGateway([bad, good])
    state = TurnState(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        tenant_slug=TENANT_A,
        user_message="I need payroll access",
        extraction=IntentExtraction(
            intent=Intent.REQUEST_ACCESS,
            entities=IntentEntities(
                system="payroll",
                role=None,
                target_user=None,
                department=None,
                expiry_days=None,
                justification=None,
            ),
            confidence=0.95,
            clarifying_question=None,
        ),
        started_at=NOW,
    )

    result = _run_plan_tools(state, gateway)

    assert gateway.call_count == 2
    assert result.outcome is TurnOutcome.CONTINUE
    assert [call.tool for call in result.proposed] == ["classify_access_request"]
    assert result.proposed[0].arguments == arguments
