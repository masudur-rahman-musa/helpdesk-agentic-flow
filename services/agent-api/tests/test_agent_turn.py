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
from typing import Any

import httpx
import pytest
from agent_api.graph.build import FAILURE_REPLY, TurnDeps, build_graph, run_turn
from agent_api.graph.gates import MAX_DISAMBIGUATION_OPTIONS
from agent_api.graph.nodes.endings import REFUSAL_REPLY
from agent_api.graph.nodes.retrieve_policy import MAX_QUERY_CHARS, query_for
from agent_api.graph.state import Citation, Intent, TurnOutcome, TurnState
from agent_api.mcp_client import RemoteInternalError, ToolCaller
from agent_api.prompts import load_prompt
from agent_api.tools.catalogue import SEARCH_POLICY
from helpdesk.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.core.repository import (
    HISTORY_LIMIT,
    Repository,
    append_message,
    get_or_create_session,
)
from helpdesk.core.settings import load_settings
from helpdesk.errors import ErrorCode
from helpdesk.gateway.client import ModelClient
from helpdesk.models import Base, Department, Message, Role, Session, System, Tenant, User
from helpdesk.models.conversation import MAX_MESSAGE_CHARS, MessageRole, MessageTooLongError
from helpdesk.rag.naming import check_slug
from pydantic import BaseModel, ValidationError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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


def _completion(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
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
            "entities": entities or {},
            "confidence": confidence,
            "clarifying_question": clarifying_question,
        }
    )


class StagedGateway:
    """Answers staged in order; a request past the last one fails the test.

    That is how "the transport was never called a second time" is asserted as
    a hard stop rather than as a count read afterwards.
    """

    def __init__(self, answers: Sequence[str]) -> None:
        self._answers = list(answers)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
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


# --- the three-node happy path ------------------------------------------------


def test_the_happy_path_runs_the_three_nodes_and_resolves_the_entities(turn_env: TurnEnv) -> None:
    """Catches a graph whose edges skip `resolve_entities` — the state would
    come back `CONTINUE` with nothing resolved, and the tool call would go out
    against a system named only as free text the model produced.

    An earlier change §8: the turn no longer ENDS after `resolve_entities`, it continues
    into `retrieve_policy`, so the same case now also catches an
    `_after_entities` left routing `CONTINUE` to `END` — the tool call count
    below is what sees it. Every 17a assertion is unchanged."""
    session_id = uuid.uuid4()
    gateway = StagedGateway([_payroll_answer()])
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
    assert state.outcome is TurnOutcome.CONTINUE
    assert state.extraction is not None
    assert state.extraction.intent is Intent.REQUEST_ACCESS
    assert state.resolved is not None
    assert state.resolved.system_id == turn_env.tenant_a.system_ids["payroll"]
    assert state.resolved.system_slug == "payroll"
    assert state.resolved.role_id == turn_env.tenant_a.role_ids["payroll-read"]
    assert gateway.call_count == 1


def test_the_prompt_version_is_the_extraction_prompts_own_digest(turn_env: TurnEnv) -> None:
    """Catches a `prompt_version` invented by the node — a hand-kept string or
    the model's own name — which makes every trace claim a version no file in
    `prompts/` has, so a bad turn cannot be tied to the prompt that produced
    it (the design)."""
    session_id = uuid.uuid4()
    gateway = StagedGateway([_payroll_answer()])

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
    gateway = StagedGateway([_payroll_answer()])
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
    gateway = StagedGateway([_payroll_answer()])
    message = "x" * MAX_MESSAGE_CHARS

    state = _run_turn(
        turn_env, turn_env.tenant_a, session_id=session_id, message=message, gateway=gateway
    )

    rows = _stored_messages(turn_env, turn_env.tenant_a, session_id)
    assert state.outcome is TurnOutcome.CONTINUE
    assert len(rows) == 1
    assert len(rows[0].content) == MAX_MESSAGE_CHARS


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
    gateway = StagedGateway([_payroll_answer()])
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

    assert state.outcome is TurnOutcome.CONTINUE
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
    gateway = StagedGateway([_payroll_answer()])
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


def test_the_compiled_graph_carries_no_checkpointer() -> None:
    """Catches decision F being implemented as a `MemorySaver`: a second,
    discarded persistence story beside the conversation tables, and one that
    holds every turn's state in the process for as long as it runs. An earlier change
    adds the Postgres checkpointer with the interrupt that needs it."""
    graph = build_graph()
    assert graph.checkpointer is None


# --- an earlier change: retrieval, its refusal, and the query cut ----------------------


def test_a_request_access_turn_calls_search_policy_exactly_once(turn_env: TurnEnv) -> None:
    """Catches a node that calls the tool per candidate or per history line, and
    one that carries the citations in a re-sorted order.

    The fused order is the tool's (15a's RRF); a node that re-ranked by `score`
    would put the second citation first here, because the payload is
    deliberately ASCENDING in score. Only the node's own pass-through can keep
    this order, so a sort added anywhere flips the case."""
    session_id = uuid.uuid4()
    gateway = StagedGateway([_payroll_answer()])
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
    assert state.outcome is TurnOutcome.CONTINUE
    assert list(state.citations) == [Citation.model_validate(item) for item in page]


def test_a_retrieving_turn_writes_no_assistant_message(turn_env: TurnEnv) -> None:
    """Criterion 7: the non-refused edge is 17c's handover point, so nothing is
    said to the user yet. Catches a `retrieve_policy` that stores a reply of its
    own — the user would be answered twice once 17c composes the real one — and
    an edge routing a successful retrieval into an ending node."""
    session_id = uuid.uuid4()
    gateway = StagedGateway([_payroll_answer()])
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
    assert state.reply is None
    assert [row.role for row in rows] == [MessageRole.USER.value]


def test_an_ask_policy_turn_also_retrieves(turn_env: TurnEnv) -> None:
    """The SECOND of the design step 5's two intents. Catches a
    `should_retrieve` written for `request_access` alone: `ask_policy` names no
    system and resolves no entity, so nothing else in the turn would notice
    that the one intent whose whole purpose is the corpus never reads it."""
    session_id = uuid.uuid4()
    gateway = StagedGateway([_extraction_json(intent="ask_policy", confidence=0.95)])
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
    assert state.outcome is TurnOutcome.CONTINUE
    assert len(state.citations) == 1


def test_a_reset_password_turn_makes_no_tool_call_and_carries_no_citations(
    turn_env: TurnEnv,
) -> None:
    """Criterion 4's other half, one of the eleven intents §10.1 step 5 does not
    name. Catches a node that retrieves unconditionally: the turn would pay an
    embedding and a vector search for a password reset, and would carry policy
    chunks into 17c's planning for a request nobody asked a policy question
    about. The stub would ANSWER such a call, so only the count can see it."""
    session_id = uuid.uuid4()
    gateway = StagedGateway([_extraction_json(intent="reset_password", confidence=0.95)])
    tools = StubToolCaller(payload=_page(_citation("fake-password-policy", score=0.9)))

    state = _run_turn(
        turn_env,
        turn_env.tenant_a,
        session_id=session_id,
        message="I am locked out and need my password reset",
        gateway=gateway,
        tools=tools,
    )

    assert tools.calls == []
    assert state.outcome is TurnOutcome.CONTINUE
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
    gateway = StagedGateway([_payroll_answer()])
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
    assert state.outcome is TurnOutcome.CONTINUE


def test_a_message_one_character_over_the_query_cap_is_cut_not_refused(
    turn_env: TurnEnv,
) -> None:
    """The exclusive half, and decision G's whole point. Catches a node that
    answers an over-long QUERY with `VALIDATION`: the message is well under
    `MAX_MESSAGE_CHARS`, so the user was told it was allowed, and the turn must
    still resolve. The stored message keeps every character — the cut is on the
    query only — which is what the row-length assertion pins."""
    session_id = uuid.uuid4()
    gateway = StagedGateway([_payroll_answer()])
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
    assert state.outcome is TurnOutcome.CONTINUE
    assert len(rows[0].content) == MAX_QUERY_CHARS + 1


# --- decision B: the session is opened and closed by `run_turn` ---------------


def test_run_turn_opens_and_closes_the_caller_exactly_once(turn_env: TurnEnv) -> None:
    """Decision B as a count. Catches a session opened per NODE (17c makes up
    to six calls and would pay six handshakes) and one opened per call inside
    `McpToolCaller`, which is the same cost wearing a different hat. Also
    catches a `run_turn` that never enters the caller at all — the stub answers
    regardless, so nothing else in the turn would notice."""
    session_id = uuid.uuid4()
    gateway = StagedGateway([_payroll_answer()])
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
