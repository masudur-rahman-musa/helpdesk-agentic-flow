"""An earlier change §6: POST /chat and GET /chat/messages, the app driven in process.

Expected implementation surface (the implementer must provide exactly this):

``agent_api.main``
    a FastAPI ``lifespan`` building the process-lifetime resources ONCE and
    closing them at shutdown (decision B), reachable off ``app.state``:
        ``app.state.engine`` — the SQLAlchemy async engine.
        ``app.state.session_factory`` — ``async_sessionmaker[AsyncSession]``.
        ``app.state.model`` — the gateway ``ModelClient``.
        ``app.state.redis`` — the ``redis.asyncio.Redis`` the limiter uses.
        ``app.state.checkpointer_pool`` — the psycopg ``AsyncConnectionPool``.
        ``app.state.checkpointer`` — the ``AsyncPostgresSaver`` over that pool.
    the chat router included beside the existing two.

``agent_api.chat_routes``
    ``def build_tool_caller(settings: Settings, bearer_token: str) -> ToolCaller``
        — the per-turn MCP caller, called through the MODULE attribute (the
        seam ``agent_api.readiness`` already uses) so these cases drive the
        scripted stub of 17b and open no socket.
    ``POST /chat`` — body ``{"message": str}``; identity is the verified
        token's ``sub``, ``tenant_id`` and ``sid``, and unknown body keys are
        IGNORED (Pydantic's default, deliberately not ``extra="forbid"``:
        §1 requires a body naming another user to be accepted and to change
        nothing). 200 ->
        ``{"reply": str, "outcome": str, "citations": [...],
        "request_id": str | None, "decision": {...} | None}`` — the fifth key
        is the turn's decision card, ``null`` when there is none.
    ``GET /chat/messages`` — this session's messages oldest first, as
        ``{"messages": [{"role": str, "content": str}, ...]}``.

Every refusal is a typed body from ``helpdesk.errors``, never free text:
    401 ``{"code": "FORBIDDEN"}`` — an unverifiable token (the shape
        ``auth_routes`` and ``admin_routes`` already answer with).
    400 ``{"code": "VALIDATION"}`` — a message over ``MAX_MESSAGE_CHARS``.
    500 ``{"code": "INTERNAL"}`` — an unreachable gateway, MCP server or Redis.
The plan's §6 leaves the status/body pairs open; they are fixed here and owed
to the plan's Deviations. A turn that ended ``FAILED`` answers the typed 500
and carries NO reply key at all, because §1 says a dependency failure is a
typed code and "never ... a partial reply".

Cost and isolation. Postgres and Redis are the compose stack's: one throwaway
database, its schema, LangGraph's checkpoint tables and two obviously fake
tenants are built ONCE for the whole module, and per-case
isolation is a fresh session id per case plus a Redis index dedicated to the
suite that nothing here flushes. The gateway is an ``httpx.MockTransport`` and
the tool caller is the scripted stub, so no socket is opened to either. The
app's LIFESPAN is entered once per case, inside that case's own event loop,
for the same reason every other agent-api suite builds its engine per
``asyncio.run``: a psycopg pool and an asyncpg engine cannot cross loops.
Fixtures live in this module rather than a conftest, because a second
``conftest`` under ``services/agent-api/tests`` would collide with
``packages/helpdesk/tests/conftest.py`` under the repo-wide ``mypy`` run.
"""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest
from agent_api import chat_routes
from agent_api.checkpointer import build_pool, psycopg_dsn, setup_checkpointer
from agent_api.graph.nodes.await_approval import PENDING_APPROVAL_REPLY
from agent_api.graph.state import TurnOutcome
from agent_api.main import app
from agent_api.mcp_client import TRANSPORT_FAILURE_MESSAGE, RemoteInternalError, ToolCaller
from agent_api.tools.catalogue import SEARCH_POLICY
from helpdesk.auth.jwt import issue_token
from helpdesk.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.core.repository import Repository, append_message, get_or_create_session
from helpdesk.core.settings import Settings, load_settings
from helpdesk.errors import ErrorCode
from helpdesk.gateway.client import ModelClient
from helpdesk.models import Base, Department, Message, Role, Session, System, Tenant, User
from helpdesk.models.access import RequestStatus
from helpdesk.models.conversation import MAX_MESSAGE_CHARS, MessageRole
from helpdesk.policy.types import ApproverSlot, Decision, ReasonCode, RiskClass
from pydantic import BaseModel, ValidationError
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

NOW = datetime(2026, 6, 2, 10, 15, tzinfo=UTC)

GATEWAY_URL = "http://gateway.invalid:8080"
MCP_URL = "http://mcp.invalid:8081/mcp"

TENANT_A = "chat-tenant-a"
TENANT_B = "chat-tenant-b"

# Obviously fake, tests only: none of these is a credential.
API_KEYS: dict[str, str] = {slug: f"fake-key-{slug}" for slug in (TENANT_A, TENANT_B)}
TEST_JWT_SECRET = "chat-route-test-jwt-secret-0123456789abcdef"
OTHER_JWT_SECRET = "another-chat-route-fake-secret-0123456789"
GATEWAY_API_KEYS = ",".join(f"{slug}:{key}" for slug, key in API_KEYS.items())

CREATE_REQUEST = "create_access_request"
REQUEST_ID = uuid.UUID("66666666-6666-5666-8666-666666666666")

# The decision card the filed request projects. Obviously fake.
CARD_APPROVER_ID = uuid.UUID("99999999-9999-5999-8999-999999999999")
CARD_RISK_CLASS = RiskClass.PRIVILEGED.value
CARD_FIRST_SLOT = ApproverSlot.DEPARTMENT_MANAGER.value
CARD_EXPIRY_DAYS = 30
CARD_EXPIRES_AT = "2026-07-02T10:15:00Z"

MESSAGE = "Please give me read access to fake payroll."
COMPOSED_REPLY = "I have asked for fake payroll read access on your behalf."
CITED_DOCUMENT = "fake-access-policy"

TEST_REDIS_DB = 15
_FALLBACK_TEST_REDIS_DB = 14
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ""})

_UNREACHABLE_MSG = (
    "Postgres is unreachable at the configured DATABASE_URL.\n"
    "Plan 19a chat route tests require the compose stack: run `docker compose up -d` first.\n"
    "Original error: {error!r}"
)
_REDIS_UNREACHABLE_MSG = (
    "Redis is unreachable at the configured REDIS_URL.\n"
    "Plan 19a chat route tests require the compose stack: run `docker compose up -d` first.\n"
    "Original error: {error!r}"
)


# --- the throwaway database, its two tenants and the checkpoint tables ---------


async def _admin_execute(admin_url: str, sql: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


@dataclass(frozen=True)
class SeededTenant:
    """One obviously fake tenant: two people, one system, one role."""

    tenant_id: uuid.UUID
    tenant_slug: str
    department_id: uuid.UUID
    user_id: uuid.UUID
    other_user_id: uuid.UUID
    role_id: uuid.UUID


@dataclass(frozen=True)
class ChatEnv:
    database_url: str
    redis_url: str
    tenant_a: SeededTenant
    tenant_b: SeededTenant


async def _seed(factory: async_sessionmaker[AsyncSession], slug: str) -> SeededTenant:
    async with factory() as session:
        tenant = Tenant(slug=slug, name=f"Fake Tenant {slug}", residency="eu", settings={})
        session.add(tenant)
        await session.commit()
        tenant_id = tenant.id

    async with tenant_session(factory, tenant_id) as session:
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

        system = System(
            tenant_id=tenant_id,
            slug="payroll",
            name="Fake Payroll",
            sensitivity="low",
            owner_user_id=people[0].id,
        )
        session.add(system)
        await session.flush()

        role = Role(
            tenant_id=tenant_id,
            system_id=system.id,
            slug="payroll-read",
            name="Fake Payroll Read",
            privilege="read",
            default_expiry_days=90,
        )
        session.add(role)
        await session.flush()

        return SeededTenant(
            tenant_id=tenant_id,
            tenant_slug=slug,
            department_id=department.id,
            user_id=people[0].id,
            other_user_id=people[1].id,
            role_id=role.id,
        )


def _redis_url_for_db(url: str, index: int) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{index}", parts.query, parts.fragment))


def _redis_db_index(url: str) -> int:
    path = urlsplit(url).path.lstrip("/")
    return int(path) if path.isdigit() else 0


async def _ping_redis(url: str) -> None:
    client: Redis = Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
    try:
        await client.ping()
    finally:
        await client.aclose()


def _test_redis_url() -> str:
    """A Redis database index dedicated to the suite. Nothing here flushes it."""
    settings = load_settings()
    host = urlsplit(settings.redis_url).hostname or ""
    if host not in _LOCAL_HOSTS:
        pytest.fail(
            f"REDIS_URL points at {host!r}, not this machine. Point it at the compose stack.",
            pytrace=False,
        )
    configured = _redis_db_index(settings.redis_url)
    index = TEST_REDIS_DB if configured != TEST_REDIS_DB else _FALLBACK_TEST_REDIS_DB
    url = _redis_url_for_db(settings.redis_url, index)
    try:
        asyncio.run(_ping_redis(url))
    except Exception as exc:
        pytest.fail(_REDIS_UNREACHABLE_MSG.format(error=exc), pytrace=False)
    return url


@pytest.fixture(scope="session")
def chat_db() -> Iterator[ChatEnv]:
    """One database, its schema, the checkpoint tables and two tenants, once.

    A throwaway database rather than the compose one for an earlier change's reason:
    `test_models.py` and `test_seed.py` assert on a database's full table list,
    and LangGraph's checkpoint tables are outside `Base.metadata` on purpose.
    Nothing per-case is built here; a case's namespace is its
    own session id.
    """
    try:
        settings = load_settings()
    except ValidationError as exc:
        pytest.fail(
            f"DATABASE_URL is not configured (env or .env). Copy .env.example to .env.\n{exc}",
            pytrace=False,
        )
    admin_url = settings.database_url
    try:
        asyncio.run(_admin_execute(admin_url, "SELECT 1"))
    except Exception as exc:
        pytest.fail(_UNREACHABLE_MSG.format(error=exc), pytrace=False)

    redis_url = _test_redis_url()
    name = f"helpdesk_chat_test_{uuid.uuid4().hex[:12]}"
    asyncio.run(_admin_execute(admin_url, f'CREATE DATABASE "{name}"'))
    url = make_url(admin_url).set(database=name).render_as_string(hide_password=False)

    async def prepare() -> ChatEnv:
        engine = create_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            factory = create_session_factory(engine)
            tenant_a = await _seed(factory, TENANT_A)
            tenant_b = await _seed(factory, TENANT_B)
        finally:
            await engine.dispose()
        return ChatEnv(database_url=url, redis_url=redis_url, tenant_a=tenant_a, tenant_b=tenant_b)

    async def create_checkpoint_tables(dsn: str) -> None:
        pool = build_pool(dsn)
        async with pool:
            await setup_checkpointer(pool)

    env = asyncio.run(prepare())
    asyncio.run(create_checkpoint_tables(psycopg_dsn(url)))
    yield env
    assert name.startswith("helpdesk_chat_test_")
    asyncio.run(_admin_execute(admin_url, f'DROP DATABASE "{name}" WITH (FORCE)'))


@pytest.fixture
def chat_env(chat_db: ChatEnv, monkeypatch: pytest.MonkeyPatch) -> ChatEnv:
    """Point the app's lifespan at the seeded database and the test Redis index."""
    monkeypatch.setenv("DATABASE_URL", chat_db.database_url)
    monkeypatch.setenv("REDIS_URL", chat_db.redis_url)
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SECRET)
    monkeypatch.setenv("GATEWAY_URL", GATEWAY_URL)
    monkeypatch.setenv("GATEWAY_API_KEYS", GATEWAY_API_KEYS)
    monkeypatch.setenv("MCP_URL", MCP_URL)
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    return chat_db


# --- the staged gateway and the scripted caller -------------------------------


def _completion(answer: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": answer}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )


class StagedGateway:
    """Answers staged in order; a request past the last one fails the test.

    That is how "no turn was started" is asserted as a hard stop rather than as
    a count read afterwards: a route that ran the turn before checking the
    token, the cap or the limit asks for an answer nobody staged.
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


class UnreachableGateway:
    """A transport that refuses every dial, the way a dead gateway does."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        raise httpx.ConnectError("connection refused", request=request)

    @property
    def call_count(self) -> int:
        return len(self.requests)


class ScriptedToolCaller:
    """A `ToolCaller` answering one scripted payload per call, in order."""

    def __init__(self, *answers: Mapping[str, Any]) -> None:
        self._answers = list(answers)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.entered = 0

    async def __aenter__(self) -> ToolCaller:
        self.entered += 1
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def call_tool(self, name: str, arguments: BaseModel) -> Mapping[str, Any]:
        self.calls.append((name, arguments.model_dump(mode="json")))
        index = len(self.calls) - 1
        if index >= len(self._answers):
            raise AssertionError(
                f"the turn made {len(self.calls)} tool calls ({self.names}); "
                f"only {len(self._answers)} were scripted"
            )
        return self._answers[index]

    @property
    def names(self) -> list[str]:
        return [name for name, _arguments in self.calls]


class UnreachableToolCaller:
    """A caller whose handshake fails, exactly as `McpToolCaller.__aenter__` does."""

    async def __aenter__(self) -> ToolCaller:
        raise RemoteInternalError(TRANSPORT_FAILURE_MESSAGE)

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def call_tool(self, name: str, arguments: BaseModel) -> Mapping[str, Any]:
        raise AssertionError("a caller whose handshake failed must never be called")


def _page() -> dict[str, Any]:
    """One `search_policy` page with a single obviously fake citation."""
    return {
        "citations": [
            {
                "document": CITED_DOCUMENT,
                "title": "Fake Access Policy",
                "version": "v1",
                "heading_path": ["Fake Section"],
                "chunk_id": str(uuid.uuid4()),
                "text": "Obviously fake policy text about fake payroll access.",
                "score": 0.42,
            }
        ],
        "refused": False,
    }


def _approval_payload() -> dict[str, Any]:
    """A `create_access_request` answer that went to an approval chain."""
    return {
        "request_id": str(REQUEST_ID),
        "decision": "approval",
        "status": "pending",
        "required_approvers": ["manager", "system_owner"],
    }


def _card_payload() -> dict[str, Any]:
    """The same approval answer, with every field a decision card projects.

    `_approval_payload` carries only what the PAUSE gate reads, so a card
    projected off it would be `None` for a missing key rather than for the
    route dropping the key. This one is the tool's whole output:
    one seat the engine resolved, one group seat it never can.
    """
    return {
        "request_id": str(REQUEST_ID),
        "status": RequestStatus.PENDING.value,
        "risk_class": CARD_RISK_CLASS,
        "decision": Decision.APPROVAL.value,
        "reasons": [ReasonCode.PRIVILEGE_ADMIN.value],
        "required_approvers": [
            {"slot": CARD_FIRST_SLOT, "user_id": str(CARD_APPROVER_ID)},
            {"slot": ApproverSlot.IT_ADMIN.value, "user_id": None},
        ],
        "approver_user_ids": [str(CARD_APPROVER_ID)],
        "effective_expiry_days": CARD_EXPIRY_DAYS,
        "sod_conflicts": [],
        "policy_refs": ["FAKE-POL-001 §4"],
        "grant_id": None,
        "expires_at": CARD_EXPIRES_AT,
    }


def _extraction_answer() -> str:
    return json.dumps(
        {
            "intent": "request_access",
            "entities": {"system": "payroll", "role": "payroll-read"},
            "confidence": 0.95,
            "clarifying_question": None,
        }
    )


EMPTY_PLAN = json.dumps({"calls": []})
"""A planning round proposing nothing: the ordinary end of a turn."""


def _request_plan(env: ChatEnv) -> str:
    """One planning round proposing exactly the request a pause waits on."""
    return json.dumps(
        {
            "calls": [
                {
                    "tool": CREATE_REQUEST,
                    "arguments": {
                        "target_user_id": str(env.tenant_a.user_id),
                        "role_id": str(env.tenant_a.role_id),
                        "justification": "Obviously fake justification.",
                        "requested_expiry_days": None,
                        "citations": [],
                    },
                }
            ]
        }
    )


# --- driving the app ----------------------------------------------------------


def _token(tenant: SeededTenant, session_id: uuid.UUID, *, secret: str = TEST_JWT_SECRET) -> str:
    """One verified-looking token for a seeded person and one conversation."""
    return issue_token(
        {
            "sub": str(tenant.user_id),
            "tenant_id": str(tenant.tenant_id),
            "department_id": str(tenant.department_id),
            "principals": ["employee"],
            "sid": str(session_id),
        },
        secret,
        datetime.now(UTC),
    )


def _drive[T](
    *,
    monkeypatch: pytest.MonkeyPatch,
    scenario: Callable[[httpx.AsyncClient], Awaitable[T]],
    gateway_handler: Callable[[httpx.Request], httpx.Response] | None = None,
    tools: ToolCaller | None = None,
    redis_url: str | None = None,
) -> T:
    """Enter the app's lifespan, stage the seams, and run one scenario over it.

    The gateway client and the Redis client the lifespan built are CLOSED
    before being replaced, so a case that stages its own leaks neither into the
    compose stack. The tool caller is replaced at the module seam, because a
    turn's caller is built per turn (17b decision B) and never lives on the app.
    """
    if tools is not None:
        caller = tools

        def _build_tool_caller(settings: Settings, bearer_token: str) -> ToolCaller:
            return caller

        monkeypatch.setattr(chat_routes, "build_tool_caller", _build_tool_caller)

    async def runner() -> T:
        async with app.router.lifespan_context(app):
            if gateway_handler is not None:
                await app.state.model.aclose()
                app.state.model = ModelClient(
                    base_url=GATEWAY_URL,
                    api_keys=API_KEYS,
                    transport=httpx.MockTransport(gateway_handler),
                )
            if redis_url is not None:
                await app.state.redis.aclose()
                app.state.redis = Redis.from_url(
                    redis_url, socket_connect_timeout=1, socket_timeout=1
                )
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await scenario(client)

    return asyncio.run(runner())


def _post_chat(
    *,
    monkeypatch: pytest.MonkeyPatch,
    token: str,
    body: dict[str, Any],
    gateway_handler: Callable[[httpx.Request], httpx.Response] | None = None,
    tools: ToolCaller | None = None,
    redis_url: str | None = None,
) -> httpx.Response:
    async def scenario(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post("/chat", json=body, headers={"Authorization": f"Bearer {token}"})

    return _drive(
        monkeypatch=monkeypatch,
        scenario=scenario,
        gateway_handler=gateway_handler,
        tools=tools,
        redis_url=redis_url,
    )


def _get_messages(*, monkeypatch: pytest.MonkeyPatch, token: str) -> httpx.Response:
    async def scenario(client: httpx.AsyncClient) -> httpx.Response:
        return await client.get("/chat/messages", headers={"Authorization": f"Bearer {token}"})

    return _drive(monkeypatch=monkeypatch, scenario=scenario)


# --- reading the rows back ----------------------------------------------------


@dataclass(frozen=True)
class StoredMessage:
    role: str
    content: str
    ordinal: int
    created_at: datetime


def _stored_messages(
    env: ChatEnv, tenant: SeededTenant, session_id: uuid.UUID
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
                        created_at=row.created_at,
                    )
                    for row in rows
                    if row.session_id == session_id
                ]
            return sorted(stored, key=lambda row: row.ordinal)
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _stored_session(
    env: ChatEnv, tenant: SeededTenant, session_id: uuid.UUID
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


def _plant(
    env: ChatEnv, tenant: SeededTenant, session_id: uuid.UUID, lines: Sequence[tuple[str, str]]
) -> None:
    """One conversation planted in ONE transaction, so every row shares a clock."""

    async def scenario() -> None:
        engine = create_engine(env.database_url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant.tenant_id) as db:
                await get_or_create_session(
                    db, session_id=session_id, user_id=tenant.user_id, now=NOW
                )
                for role, content in lines:
                    await append_message(
                        db, session_id=session_id, role=MessageRole(role), content=content
                    )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


# --- one message, one turn ----------------------------------------------------


def test_one_message_stores_one_turn_and_answers_with_the_reply(
    chat_env: ChatEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a route that runs the turn and answers without storing it, or
    stores it and answers something other than the state's own reply: the two
    message rows and the reply string are read from opposite ends of the same
    turn, so a hand-written 200 body cannot satisfy both.

    The session row's `(tenant_id, user_id)` is the criterion's other half —
    the row is created from the token's `sid` — and the citation list catches a
    response model that drops what the reply is grounded in.
    """
    session_id = uuid.uuid4()
    gateway = StagedGateway([_extraction_answer(), EMPTY_PLAN, COMPOSED_REPLY])
    tools = ScriptedToolCaller(_page())
    token = _token(chat_env.tenant_a, session_id)

    response = _post_chat(
        monkeypatch=monkeypatch,
        token=token,
        body={"message": MESSAGE},
        gateway_handler=gateway.handler,
        tools=tools,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reply"] == COMPOSED_REPLY
    assert body["outcome"] == TurnOutcome.DONE.value
    assert [citation["document"] for citation in body["citations"]] == [CITED_DOCUMENT]
    assert body["request_id"] is None
    # Three staged answers (extraction, planning, composition) and one scripted
    # tool call (the retrieval): a turn that made a fourth request or a second
    # call fails inside the stub rather than here.
    assert gateway.call_count == 3
    assert tools.names == [SEARCH_POLICY]

    assert _stored_session(chat_env, chat_env.tenant_a, session_id) == (
        chat_env.tenant_a.tenant_id,
        chat_env.tenant_a.user_id,
    )
    stored = _stored_messages(chat_env, chat_env.tenant_a, session_id)
    assert [(row.role, row.content) for row in stored] == [
        (MessageRole.USER.value, MESSAGE),
        (MessageRole.ASSISTANT.value, COMPOSED_REPLY),
    ]


def test_identity_comes_from_the_token_and_not_from_the_body(
    chat_env: ChatEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a route that honours a `user_id` or a `session_id` from the body
    instead of the token's `sub` and `sid` (a project rule, decision A1).

    The named user is the OTHER person in the CALLER'S OWN tenant and the named
    tenant is the caller's own: a cross-tenant version would
    be refused by the tenant guard whether this control exists or not, so it
    would pin nothing. The named session id is the second identity a client
    must not be able to choose — it IS the checkpoint thread, so a
    route reading it addresses another turn's checkpoints.
    """
    session_id = uuid.uuid4()
    named_session_id = uuid.uuid4()
    gateway = StagedGateway([_extraction_answer(), EMPTY_PLAN, COMPOSED_REPLY])
    tools = ScriptedToolCaller(_page())
    token = _token(chat_env.tenant_a, session_id)

    response = _post_chat(
        monkeypatch=monkeypatch,
        token=token,
        body={
            "message": MESSAGE,
            "user_id": str(chat_env.tenant_a.other_user_id),
            "tenant_id": str(chat_env.tenant_a.tenant_id),
            "session_id": str(named_session_id),
        },
        gateway_handler=gateway.handler,
        tools=tools,
    )

    assert response.status_code == 200, response.text
    assert _stored_session(chat_env, chat_env.tenant_a, session_id) == (
        chat_env.tenant_a.tenant_id,
        chat_env.tenant_a.user_id,
    )
    assert _stored_session(chat_env, chat_env.tenant_a, named_session_id) is None
    assert len(_stored_messages(chat_env, chat_env.tenant_a, session_id)) == 2
    assert _stored_messages(chat_env, chat_env.tenant_a, named_session_id) == []


# --- the refusals -------------------------------------------------------------


def test_an_unverifiable_token_is_a_typed_401_and_starts_no_turn(
    chat_env: ChatEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a route that reads the claims without verifying the signature:
    the token here is well-formed and carries a real tenant, a real user and a
    real `sid`, and is signed with the wrong secret, so only the verification
    can refuse it.

    The gateway is staged with NOTHING, so a turn that started at all fails the
    case outright rather than showing up as a row count afterwards.
    """
    session_id = uuid.uuid4()
    gateway = StagedGateway([])
    tools = ScriptedToolCaller()
    token = _token(chat_env.tenant_a, session_id, secret=OTHER_JWT_SECRET)

    response = _post_chat(
        monkeypatch=monkeypatch,
        token=token,
        body={"message": MESSAGE},
        gateway_handler=gateway.handler,
        tools=tools,
    )

    assert response.status_code == 401
    assert response.json() == {"code": ErrorCode.FORBIDDEN.value}
    assert gateway.call_count == 0
    assert _stored_session(chat_env, chat_env.tenant_a, session_id) is None
    assert _stored_messages(chat_env, chat_env.tenant_a, session_id) == []


def test_a_message_one_character_over_the_cap_writes_no_row_at_all(
    chat_env: ChatEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a route that lets `MessageTooLongError` escape unmapped: the
    pre-check itself is 17a's, but nothing yet turns it into an ANSWER, so
    without this mapping FastAPI replies with an untyped 500 and a stack trace
    for an input the user can fix.

    Both sides of the boundary, exactly at the cap and one over it (standing
    rules 1 and 2): a route that refused at 4000 would reject a message the
    column stores happily.
    """
    assert MAX_MESSAGE_CHARS == 4000

    accepted_session = uuid.uuid4()
    gateway = StagedGateway([_extraction_answer(), EMPTY_PLAN, COMPOSED_REPLY])
    accepted = _post_chat(
        monkeypatch=monkeypatch,
        token=_token(chat_env.tenant_a, accepted_session),
        body={"message": "a" * MAX_MESSAGE_CHARS},
        gateway_handler=gateway.handler,
        tools=ScriptedToolCaller(_page()),
    )
    assert accepted.status_code == 200, accepted.text
    assert len(_stored_messages(chat_env, chat_env.tenant_a, accepted_session)) == 2

    refused_session = uuid.uuid4()
    empty_gateway = StagedGateway([])
    refused = _post_chat(
        monkeypatch=monkeypatch,
        token=_token(chat_env.tenant_a, refused_session),
        body={"message": "a" * (MAX_MESSAGE_CHARS + 1)},
        gateway_handler=empty_gateway.handler,
        tools=ScriptedToolCaller(),
    )

    assert refused.status_code == 400
    assert refused.json() == {"code": ErrorCode.VALIDATION.value}
    assert empty_gateway.call_count == 0
    assert _stored_session(chat_env, chat_env.tenant_a, refused_session) is None
    assert _stored_messages(chat_env, chat_env.tenant_a, refused_session) == []


# --- the paused turn ----------------------------------------------------------


def test_a_paused_turn_answers_with_the_pending_reply_and_the_request_id(
    chat_env: ChatEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a route that treats a paused turn as an unfinished one (decision
    E2's 202 with no body, or a 500 on the outcome it does not recognise): the
    turn produced a reply, stored it and filed a request, and a client that
    cannot read the request id cannot show the user what they are waiting for.

    No composition answer is staged, so a route that re-ran or resumed the turn
    would ask for an answer nobody staged and fail outright.
    """
    session_id = uuid.uuid4()
    gateway = StagedGateway([_extraction_answer(), _request_plan(chat_env)])
    tools = ScriptedToolCaller(_page(), _approval_payload())
    token = _token(chat_env.tenant_a, session_id)

    response = _post_chat(
        monkeypatch=monkeypatch,
        token=token,
        body={"message": MESSAGE},
        gateway_handler=gateway.handler,
        tools=tools,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == TurnOutcome.AWAIT_APPROVAL.value
    assert body["request_id"] == str(REQUEST_ID)
    assert body["reply"].startswith(PENDING_APPROVAL_REPLY.split("{")[0])
    assert gateway.call_count == 2
    assert tools.names == [SEARCH_POLICY, CREATE_REQUEST]


# --- the decision card on the wire --------------------------------------------


def test_a_filed_request_answers_its_decision_card(
    chat_env: ChatEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a route that files a request and answers nothing about WHY: the
    fifth key is read off the JSON body as a nested object — `body["decision"]`
    with `risk_class` and `approvers[0]["slot"]` inside it — so a route that
    flattened the card into the envelope, or that carries the tool's payload
    through unprojected, fails here rather than at 22d's rendering.

    The same turn shape as the paused case above and the same staging: two
    gateway answers (extraction, planning; the pause composes from a template)
    and two tool calls (the retrieval this turn always makes, then the create),
    so a route that ran an extra call fails inside the stub. The payload is the
    tool's whole output, so a `None` card here is the route's doing and not a
    missing key's.
    """
    session_id = uuid.uuid4()
    gateway = StagedGateway([_extraction_answer(), _request_plan(chat_env)])
    tools = ScriptedToolCaller(_page(), _card_payload())

    response = _post_chat(
        monkeypatch=monkeypatch,
        token=_token(chat_env.tenant_a, session_id),
        body={"message": MESSAGE},
        gateway_handler=gateway.handler,
        tools=tools,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == TurnOutcome.AWAIT_APPROVAL.value

    card = body["decision"]
    assert card is not None
    assert card["risk_class"] == CARD_RISK_CLASS
    assert card["decision"] == Decision.APPROVAL.value
    assert card["request_id"] == str(REQUEST_ID)
    assert card["status"] == RequestStatus.PENDING.value
    assert card["approvers"][0]["slot"] == CARD_FIRST_SLOT
    assert card["approvers"][0]["resolved"] is True
    assert card["effective_expiry_days"] == CARD_EXPIRY_DAYS
    assert card["expires_at"] == CARD_EXPIRES_AT
    assert str(CARD_APPROVER_ID) not in json.dumps(card)

    assert gateway.call_count == 2
    assert tools.names == [SEARCH_POLICY, CREATE_REQUEST]


def test_a_turn_with_no_access_tool_answers_a_null_decision(
    chat_env: ChatEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a route that always builds a card — one projected off whatever
    the last tool answered, or an empty object standing in for "no decision":
    this turn only retrieved policy, so a client shown a card here would read a
    verdict the engine never made.

    The key must be PRESENT and null rather than absent, which is what the
    subscript asserts: a client branching on the key needs one shape for both
    turns (19c decision G1).
    """
    session_id = uuid.uuid4()
    gateway = StagedGateway([_extraction_answer(), EMPTY_PLAN, COMPOSED_REPLY])
    tools = ScriptedToolCaller(_page())

    response = _post_chat(
        monkeypatch=monkeypatch,
        token=_token(chat_env.tenant_a, session_id),
        body={"message": MESSAGE},
        gateway_handler=gateway.handler,
        tools=tools,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == TurnOutcome.DONE.value
    assert body["decision"] is None
    # Three staged answers (extraction, planning, composition), one tool call.
    assert gateway.call_count == 3
    assert tools.names == [SEARCH_POLICY]


# --- the history route --------------------------------------------------------


def test_the_history_route_answers_this_sessions_messages_oldest_first(
    chat_env: ChatEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a history route ordered by `created_at`, or by nothing at all:
    the three rows are planted in ONE transaction, so Postgres stamps them with
    one identical timestamp and only `ordinal` can tell them apart.

    The premise assertion reads that identical timestamp off the rows before
    the order is asserted, so a fixture that stopped sharing
    a transaction shows up as a red premise rather than as a passing case that
    proves nothing.
    """
    session_id = uuid.uuid4()
    planted = [
        (MessageRole.USER.value, "First fake question."),
        (MessageRole.ASSISTANT.value, "First fake answer."),
        (MessageRole.USER.value, "Second fake question."),
    ]
    _plant(chat_env, chat_env.tenant_a, session_id, planted)

    stored = _stored_messages(chat_env, chat_env.tenant_a, session_id)
    assert len({row.created_at for row in stored}) == 1, (
        "premise: the planted rows must share one transaction timestamp, or "
        "ordering by created_at would pass this case by accident"
    )

    response = _get_messages(monkeypatch=monkeypatch, token=_token(chat_env.tenant_a, session_id))

    assert response.status_code == 200, response.text
    messages = response.json()["messages"]
    assert [(row["role"], row["content"]) for row in messages] == planted


def test_the_history_route_cannot_read_another_tenants_session(
    chat_env: ChatEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a history route that reads by session id on an UNBOUND session:
    the token is tenant B's own and its `sid` names a conversation that exists,
    in full, inside tenant A, so the only thing keeping those messages out of
    the answer is the read being bound to the token's tenant.

    Tenant B's real token rather than a forged tenant claim: the route is asked
    a question it is entitled to ask, about a session it may not see.
    """
    session_id = uuid.uuid4()
    _plant(
        chat_env,
        chat_env.tenant_a,
        session_id,
        [
            (MessageRole.USER.value, "Fake question only tenant A may read."),
            (MessageRole.ASSISTANT.value, "Fake answer only tenant A may read."),
        ],
    )
    assert len(_stored_messages(chat_env, chat_env.tenant_a, session_id)) == 2

    response = _get_messages(monkeypatch=monkeypatch, token=_token(chat_env.tenant_b, session_id))

    assert response.status_code == 200, response.text
    assert response.json()["messages"] == []


# --- the three dependency failures --------------------------------------------


def test_an_unreachable_gateway_answers_a_typed_code_and_no_partial_reply(
    chat_env: ChatEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a route that renders a FAILED turn as an ordinary 200 answer:
    the turn comes back with `TurnOutcome.FAILED`, a typed `error_code` and the
    templated failure sentence, so a route that simply copied the state's reply
    into the response would show a user prose about a turn nothing decided.

    The gateway refuses the dial rather than erroring, which is the failure no
    node can turn into a real reply.
    """
    session_id = uuid.uuid4()
    gateway = UnreachableGateway()
    tools = ScriptedToolCaller()

    response = _post_chat(
        monkeypatch=monkeypatch,
        token=_token(chat_env.tenant_a, session_id),
        body={"message": MESSAGE},
        gateway_handler=gateway.handler,
        tools=tools,
    )

    assert response.status_code == 500
    assert response.json() == {"code": ErrorCode.INTERNAL.value}
    assert gateway.call_count >= 1, "the turn never reached the gateway at all"


def test_an_unreachable_mcp_server_answers_a_typed_code(
    chat_env: ChatEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a `ToolError` escaping the route unmapped: the MCP handshake
    fails inside `run_turn`'s `async with deps.tools`, outside every node, so
    nothing on the turn path converts it into state and an unhandled
    `RemoteInternalError` reaches the client as FastAPI's untyped 500 with a
    stack trace.

    The gateway is staged with nothing: the caller is entered before the graph
    is invoked, so a model request here would mean the failure was swallowed
    and the turn ran anyway.
    """
    session_id = uuid.uuid4()
    gateway = StagedGateway([])

    response = _post_chat(
        monkeypatch=monkeypatch,
        token=_token(chat_env.tenant_a, session_id),
        body={"message": MESSAGE},
        gateway_handler=gateway.handler,
        tools=UnreachableToolCaller(),
    )

    assert response.status_code == 500
    assert response.json() == {"code": ErrorCode.INTERNAL.value}
    assert gateway.call_count == 0


def test_an_unreachable_redis_refuses_the_turn_and_starts_none(
    chat_env: ChatEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches decision C2 shipped by accident — a limiter that fails OPEN, so
    the one control bounding a runaway client disappears exactly when Redis is
    down — and a limiter checked AFTER the turn, which would bill a model call
    and store two rows for a message it then refuses.

    The gateway is staged with nothing and the rows are read back, so both
    halves of "starts none" are asserted rather than inferred from the status.
    """
    session_id = uuid.uuid4()
    gateway = StagedGateway([])
    tools = ScriptedToolCaller()

    response = _post_chat(
        monkeypatch=monkeypatch,
        token=_token(chat_env.tenant_a, session_id),
        body={"message": MESSAGE},
        gateway_handler=gateway.handler,
        tools=tools,
        redis_url="redis://127.0.0.1:1/0",
    )

    assert response.status_code == 500
    assert response.json() == {"code": ErrorCode.INTERNAL.value}
    assert gateway.call_count == 0
    assert tools.entered == 0
    assert _stored_session(chat_env, chat_env.tenant_a, session_id) is None
    assert _stored_messages(chat_env, chat_env.tenant_a, session_id) == []


# --- the lifespan -------------------------------------------------------------


def test_the_lifespan_closes_the_pool_and_the_clients(
    chat_env: ChatEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a lifespan that builds the checkpointer pool and the gateway
    client at startup and never closes them at shutdown — the plan's first
    risk: a process-lifetime pool left open leaks connections into the shared
    compose Postgres, and every module after this one pays for it.

    Both halves are read off the objects the LIFESPAN itself built: nothing is
    staged in this case, so the assertions cannot be satisfied by a client the
    test swapped in. The open assertions before the exit are what stop the case
    passing on a lifespan that built neither.
    """

    async def scenario() -> None:
        async with app.router.lifespan_context(app):
            assert app.state.checkpointer_pool.closed is False
            assert app.state.model.client.is_closed is False
            assert app.state.checkpointer is not None
            assert app.state.session_factory is not None

    asyncio.run(scenario())

    assert app.state.checkpointer_pool.closed is True
    assert app.state.model.client.is_closed is True
