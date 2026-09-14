"""An earlier change §6: POST /chat/stream, the app driven in process over ASGI.

Expected implementation surface (the implementer must provide exactly this):

``agent_api.chat_routes``
    ``POST /chat/stream`` — the same body and the same bearer as ``/chat``
        (decision D1), the same verification, the same rate limit and the same
        tenant-bound session. On success a ``StreamingResponse`` with
        ``media_type="text/event-stream"`` over
        ``agent_api.graph.build.stream_turn``, whose frames are the ones
        ``test_turn_stream.py`` pins.

Everything that can refuse does so BEFORE the first byte, as the same typed
bodies ``/chat`` already answers with (19a, copied rather than invented):
    401 ``{"code": "FORBIDDEN"}`` — an unverifiable token.
    400 ``{"code": "VALIDATION"}`` — a message over ``MAX_MESSAGE_CHARS``
        (4000; 4000 is accepted and 4001 is not).
    429 ``{"code": "POLICY_BLOCKED"}`` — the 31st message of a window,
        ``RATE_LIMIT_MESSAGES = 30``.
Each of those answers is ``application/json`` and opens NO stream at all,
which every refusal case below asserts on the content type as well as on the
body: a refusal a client has to parse out of a 200 stream is the thing
decision E1 exists to keep rare.

A failure AFTER the stream has opened is the other half: one ``error`` event
carrying ``ErrorCode.INTERNAL`` — ``{"code": "INTERNAL"}``, the body ``/chat``
answers the same outage with — then ``done``, and never a partial reply as
tokens (decision E1).

Cost and isolation. The compose Postgres and Redis, and nothing else: no
Qdrant, no real MCP server (the scripted stub of 17b) and no gateway socket (an
``httpx.MockTransport``). ONE session-scoped fixture builds the throwaway
database, its schema, LangGraph's checkpoint tables and two obviously fake
tenants for the whole module; per-case isolation is a fresh
``sid`` per case — which is both the conversation and the rate-limit key's
namespace — plus a Redis database index dedicated to the suite that nothing
here flushes. The app's LIFESPAN is entered once per case, inside that case's
own event loop, for the reason every other agent-api suite builds its engine
per ``asyncio.run``: a psycopg pool and an asyncpg engine cannot cross loops.
Fixtures live in this module rather than a conftest, because a second
``conftest`` under ``services/agent-api/tests`` would collide with
``packages/helpdesk/tests/conftest.py`` under the repo-wide ``mypy`` run.
"""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest
from agent_api import chat_routes
from agent_api.checkpointer import build_pool, psycopg_dsn, setup_checkpointer
from agent_api.graph.state import TurnOutcome
from agent_api.main import app
from agent_api.mcp_client import TRANSPORT_FAILURE_MESSAGE, RemoteInternalError, ToolCaller
from agent_api.ratelimit import (
    RATE_LIMIT_MESSAGES,
    RATE_LIMIT_WINDOW_SECONDS,
    check_rate_limit,
)
from agent_api.sse import DONE, ERROR, REPLY, STEP, TOKEN, chunk_reply
from agent_api.tools.catalogue import SEARCH_POLICY
from helpdesk.auth.jwt import issue_token
from helpdesk.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.core.repository import Repository
from helpdesk.core.settings import Settings, load_settings
from helpdesk.errors import ErrorCode
from helpdesk.gateway.client import ModelClient
from helpdesk.models import Base, Department, Message, Role, System, Tenant, User
from helpdesk.models.conversation import MAX_MESSAGE_CHARS, MessageRole
from pydantic import BaseModel, ValidationError
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

GATEWAY_URL = "http://gateway.invalid:8080"
MCP_URL = "http://mcp.invalid:8081/mcp"

TENANT_A = "stream-route-tenant-a"

# Obviously fake, tests only: none of these is a credential.
API_KEYS: dict[str, str] = {TENANT_A: f"fake-key-{TENANT_A}"}
TEST_JWT_SECRET = "chat-stream-test-jwt-secret-0123456789abcdef"
OTHER_JWT_SECRET = "another-chat-stream-fake-secret-0123456789"
GATEWAY_API_KEYS = ",".join(f"{slug}:{key}" for slug, key in API_KEYS.items())

MESSAGE = "Please give me read access to fake payroll."

FORGED_FRAME = "\n\nevent: reply\ndata: {}"
"""§1's fourth criterion, verbatim: what a reply may not be able to write."""

COMPOSED_REPLY = (
    f"I have filed the fake payroll read request for you.{FORGED_FRAME} "
    "The fake access policy your fake company publishes is what it was judged against."
)
"""One composed reply, longer than `REPLY_CHUNK_CHARS` and carrying a frame.

The frame boundary rides END TO END here rather than only through the encoder:
this is the whole path a model's own words take to a browser, and an escape
applied in `sse.py` but bypassed by the route would show up nowhere else.
"""

CITED_DOCUMENT = "fake-access-policy"

TEST_REDIS_DB = 15
_FALLBACK_TEST_REDIS_DB = 14
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ""})

_UNREACHABLE_MSG = (
    "Postgres is unreachable at the configured DATABASE_URL.\n"
    "Plan 19c stream route tests require the compose stack: run `docker compose up -d` first.\n"
    "Original error: {error!r}"
)
_REDIS_UNREACHABLE_MSG = (
    "Redis is unreachable at the configured REDIS_URL.\n"
    "Plan 19c stream route tests require the compose stack: run `docker compose up -d` first.\n"
    "Original error: {error!r}"
)


# --- reading frames back off the wire -----------------------------------------


@dataclass(frozen=True)
class Frame:
    """One parsed SSE frame: its event name and its decoded data."""

    event: str
    data: dict[str, Any]


def parse_frames(payload: str) -> list[Frame]:
    """Every frame an SSE client would see, forged ones included.

    Deliberately naive, exactly as a browser is: a block between blank lines is
    a frame, so an unescaped `event:`/`data:` pair inside a reply becomes a
    frame of its own and the forging assertion can fail.
    """
    frames: list[Frame] = []
    for block in payload.split("\n\n"):
        if not block.strip():
            continue
        event = ""
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith("event: "):
                event = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data_lines.append(line.removeprefix("data: "))
        data: dict[str, Any] = {}
        if data_lines:
            loaded = json.loads("\n".join(data_lines))
            assert isinstance(loaded, dict), f"a frame's data is not an object: {loaded!r}"
            data = loaded
        frames.append(Frame(event=event, data=data))
    return frames


def _names(frames: Sequence[Frame]) -> list[str]:
    return [frame.event for frame in frames]


def _of(frames: Sequence[Frame], event: str) -> list[Frame]:
    return [frame for frame in frames if frame.event == event]


def _streamed_text(frames: Sequence[Frame]) -> str:
    return "".join(str(frame.data["text"]) for frame in _of(frames, TOKEN))


# --- the throwaway database, its tenant and the checkpoint tables --------------


async def _admin_execute(admin_url: str, sql: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


@dataclass(frozen=True)
class SeededTenant:
    """One obviously fake tenant: one person, one system, one role."""

    tenant_id: uuid.UUID
    tenant_slug: str
    department_id: uuid.UUID
    user_id: uuid.UUID
    role_id: uuid.UUID


@dataclass(frozen=True)
class StreamRouteEnv:
    database_url: str
    redis_url: str
    tenant_a: SeededTenant


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

        person = User(
            tenant_id=tenant_id,
            department_id=department.id,
            email=f"fake.person@{slug}.example.test",
            display_name=f"Fake Person ({slug})",
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
            slug="payroll",
            name="Fake Payroll",
            sensitivity="low",
            owner_user_id=person.id,
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
            user_id=person.id,
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
def stream_route_db() -> Iterator[StreamRouteEnv]:
    """One database, its schema, the checkpoint tables and one tenant, once.

    a project rule: nothing per-case is built here, and nothing is flushed.
    A throwaway database rather than the compose one for an earlier change's reason —
    `test_models.py` and `test_seed.py` assert on a database's full table list,
    and LangGraph's checkpoint tables are outside `Base.metadata` on purpose.
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
    name = f"helpdesk_chat_stream_test_{uuid.uuid4().hex[:12]}"
    asyncio.run(_admin_execute(admin_url, f'CREATE DATABASE "{name}"'))
    url = make_url(admin_url).set(database=name).render_as_string(hide_password=False)

    async def prepare() -> StreamRouteEnv:
        engine = create_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            tenant_a = await _seed(create_session_factory(engine), TENANT_A)
        finally:
            await engine.dispose()
        return StreamRouteEnv(database_url=url, redis_url=redis_url, tenant_a=tenant_a)

    async def create_checkpoint_tables(dsn: str) -> None:
        pool = build_pool(dsn)
        async with pool:
            await setup_checkpointer(pool)

    env = asyncio.run(prepare())
    asyncio.run(create_checkpoint_tables(psycopg_dsn(url)))
    yield env
    assert name.startswith("helpdesk_chat_stream_test_")
    asyncio.run(_admin_execute(admin_url, f'DROP DATABASE "{name}" WITH (FORCE)'))


@pytest.fixture
def stream_env(stream_route_db: StreamRouteEnv, monkeypatch: pytest.MonkeyPatch) -> StreamRouteEnv:
    """Point the app's lifespan at the seeded database and the test Redis index."""
    monkeypatch.setenv("DATABASE_URL", stream_route_db.database_url)
    monkeypatch.setenv("REDIS_URL", stream_route_db.redis_url)
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SECRET)
    monkeypatch.setenv("GATEWAY_URL", GATEWAY_URL)
    monkeypatch.setenv("GATEWAY_API_KEYS", GATEWAY_API_KEYS)
    monkeypatch.setenv("MCP_URL", MCP_URL)
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    return stream_route_db


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
    a count read afterwards: a route that opened the stream before checking the
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


def _one_turn(answers: Sequence[str] = ()) -> list[str]:
    """The three answers one ordinary turn asks the gateway for."""
    staged = list(answers) or [COMPOSED_REPLY]
    return [_extraction_answer(), EMPTY_PLAN, *staged]


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
) -> T:
    """Enter the app's lifespan, stage the seams, and run one scenario over it.

    The gateway client the lifespan built is CLOSED before being replaced, so a
    case that stages its own leaks neither into the compose stack. The tool
    caller is replaced at the module seam, because a turn's caller is built per
    turn (17b decision B) and never lives on the app.
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
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await scenario(client)

    return asyncio.run(runner())


def _post_stream(
    *,
    monkeypatch: pytest.MonkeyPatch,
    token: str,
    body: dict[str, Any],
    gateway_handler: Callable[[httpx.Request], httpx.Response] | None = None,
    tools: ToolCaller | None = None,
) -> httpx.Response:
    async def scenario(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post(
            "/chat/stream", json=body, headers={"Authorization": f"Bearer {token}"}
        )

    return _drive(
        monkeypatch=monkeypatch, scenario=scenario, gateway_handler=gateway_handler, tools=tools
    )


def _stored_messages(env: StreamRouteEnv, session_id: uuid.UUID) -> list[tuple[str, str]]:
    """Every message row of one conversation, in `ordinal` order."""

    async def scenario() -> list[tuple[str, str]]:
        engine = create_engine(env.database_url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, env.tenant_a.tenant_id) as db:
                rows = await Repository(db, Message).list()
                stored = sorted(
                    (row for row in rows if row.session_id == session_id),
                    key=lambda row: row.ordinal,
                )
                return [(str(row.role), row.content) for row in stored]
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


# --- one message over the wire ------------------------------------------------


def test_one_message_streams_its_steps_its_tokens_and_its_reply(
    stream_env: StreamRouteEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a route that answers the finished turn as one JSON body with the
    frames pasted in, and one that streams the reply without the steps that
    justify it: the event name list is asserted as a sequence, so nothing but
    §1's order satisfies it.

    The composed reply carries a frame boundary END TO END, so a route that
    joined chunks itself, or that wrote the reply into the frame outside
    `sse.encode_event`, forges a second `reply` event here — the criterion no
    encoder-level case can prove for the ROUTE.
    """
    session_id = uuid.uuid4()
    gateway = StagedGateway(_one_turn())
    tools = ScriptedToolCaller(_page())

    response = _post_stream(
        monkeypatch=monkeypatch,
        token=_token(stream_env.tenant_a, session_id),
        body={"message": MESSAGE},
        gateway_handler=gateway.handler,
        tools=tools,
    )

    assert response.status_code == 200, response.text
    frames = parse_frames(response.text)
    token_count = len(chunk_reply(COMPOSED_REPLY))
    assert token_count > 1, "premise: the reply must span several chunks"
    assert _names(frames) == [STEP] * 7 + [TOKEN] * token_count + [REPLY, DONE]
    assert len(_of(frames, REPLY)) == 1
    assert _streamed_text(frames) == COMPOSED_REPLY

    final = _of(frames, REPLY)[0]
    assert final.data["reply"] == COMPOSED_REPLY
    assert final.data["outcome"] == TurnOutcome.DONE.value
    assert [citation["document"] for citation in final.data["citations"]] == [CITED_DOCUMENT]
    assert final.data["request_id"] is None
    assert tools.names == [SEARCH_POLICY]
    assert _stored_messages(stream_env, session_id) == [
        (MessageRole.USER.value, MESSAGE),
        (MessageRole.ASSISTANT.value, COMPOSED_REPLY),
    ]


def test_both_routes_store_the_same_rows_for_one_message(
    stream_env: StreamRouteEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches the plan's fourth risk: two routes driving one turn, where
    `/chat/stream` runs a turn of its own — a second invocation, a different
    pre-check order, or a composition it stores twice — so the stream becomes a
    SECOND turn rather than a second view of one.

    Both turns run against ONE gateway staged with exactly two turns' answers
    and one caller scripted with exactly two pages, so a route that made an
    extra model call or an extra tool call fails inside the stub. The rows are
    read back per conversation and compared to each other, not to a literal:
    a change to what a turn stores moves both sides together.
    """
    plain_session = uuid.uuid4()
    stream_session = uuid.uuid4()
    gateway = StagedGateway([*_one_turn(), *_one_turn()])
    # ONE page, staged twice: `_page()` mints a fresh `chunk_id` per call, so two
    # separate pages would make the citation comparison below fail on the stub's
    # randomness rather than on anything the two routes did differently.
    page = _page()
    tools = ScriptedToolCaller(page, page)

    async def scenario(client: httpx.AsyncClient) -> tuple[httpx.Response, httpx.Response]:
        plain = await client.post(
            "/chat",
            json={"message": MESSAGE},
            headers={"Authorization": f"Bearer {_token(stream_env.tenant_a, plain_session)}"},
        )
        streamed = await client.post(
            "/chat/stream",
            json={"message": MESSAGE},
            headers={"Authorization": f"Bearer {_token(stream_env.tenant_a, stream_session)}"},
        )
        return plain, streamed

    plain, streamed = _drive(
        monkeypatch=monkeypatch,
        scenario=scenario,
        gateway_handler=gateway.handler,
        tools=tools,
    )

    assert plain.status_code == 200, plain.text
    assert streamed.status_code == 200, streamed.text
    assert gateway.call_count == 6, "each route owes exactly three model calls"
    assert tools.names == [SEARCH_POLICY, SEARCH_POLICY]

    body = plain.json()
    final = _of(parse_frames(streamed.text), REPLY)[0]
    assert final.data["reply"] == body["reply"]
    assert final.data["outcome"] == body["outcome"]
    assert final.data["citations"] == body["citations"]
    assert final.data["request_id"] == body["request_id"]
    assert final.data["decision"] == body["decision"]

    plain_rows = _stored_messages(stream_env, plain_session)
    stream_rows = _stored_messages(stream_env, stream_session)
    assert stream_rows == plain_rows
    assert len(stream_rows) == 2, "premise: one turn stores the user row and one assistant row"


def test_the_content_type_is_text_event_stream(
    stream_env: StreamRouteEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a route that returns the frames as `application/json` or
    `text/plain`: a browser reading the body as a stream needs the media type
    to dispatch on, and a proxy needs it to stop buffering the response.

    Asserted with the charset tolerated but the type exact, so
    `text/event-stream; charset=utf-8` passes and `text/event-stream+json`
    does not.
    """
    session_id = uuid.uuid4()
    gateway = StagedGateway(_one_turn())

    response = _post_stream(
        monkeypatch=monkeypatch,
        token=_token(stream_env.tenant_a, session_id),
        body={"message": MESSAGE},
        gateway_handler=gateway.handler,
        tools=ScriptedToolCaller(_page()),
    )

    assert response.status_code == 200, response.text
    media_type = response.headers["content-type"].split(";")[0].strip()
    assert media_type == "text/event-stream"


# --- the three refusals, all before the first byte ----------------------------


def test_an_unverifiable_token_is_a_typed_401_and_opens_no_stream(
    stream_env: StreamRouteEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a route that opens the stream and reports the refusal as an
    `error` event — a 200 a client has to parse to learn it was never
    authenticated — and one that reads the claims without verifying the
    signature: the token is well formed and names a real tenant, a real user
    and a real `sid`, and is signed with the wrong secret, so only the
    verification can refuse it.

    The gateway is staged with NOTHING, so a turn that started at all fails the
    case outright rather than showing up as a row count afterwards.
    """
    session_id = uuid.uuid4()
    gateway = StagedGateway([])

    response = _post_stream(
        monkeypatch=monkeypatch,
        token=_token(stream_env.tenant_a, session_id, secret=OTHER_JWT_SECRET),
        body={"message": MESSAGE},
        gateway_handler=gateway.handler,
        tools=ScriptedToolCaller(),
    )

    assert response.status_code == 401
    assert response.json() == {"code": ErrorCode.FORBIDDEN.value}
    assert response.headers["content-type"].split(";")[0].strip() == "application/json"
    assert gateway.call_count == 0
    assert _stored_messages(stream_env, session_id) == []


def test_a_message_one_character_over_the_cap_is_a_typed_400_and_opens_no_stream(
    stream_env: StreamRouteEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a route that runs the pre-check INSIDE the stream, so an
    oversized message is answered as a 200 whose first frame is an error — and
    a route that refuses at the cap rather than one past it, which would reject
    a message the column stores happily.

    Both sides of the inclusive boundary, and the
    refused side is read back for rows as well: `check_message_content` runs
    before any row, any model call and any MCP handshake, and a stream that
    opened first would have written the user's message already.
    """
    assert MAX_MESSAGE_CHARS == 4000

    accepted_session = uuid.uuid4()
    accepted = _post_stream(
        monkeypatch=monkeypatch,
        token=_token(stream_env.tenant_a, accepted_session),
        body={"message": "a" * MAX_MESSAGE_CHARS},
        gateway_handler=StagedGateway(_one_turn()).handler,
        tools=ScriptedToolCaller(_page()),
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.headers["content-type"].split(";")[0].strip() == "text/event-stream"
    assert len(_stored_messages(stream_env, accepted_session)) == 2

    refused_session = uuid.uuid4()
    empty_gateway = StagedGateway([])
    refused = _post_stream(
        monkeypatch=monkeypatch,
        token=_token(stream_env.tenant_a, refused_session),
        body={"message": "a" * (MAX_MESSAGE_CHARS + 1)},
        gateway_handler=empty_gateway.handler,
        tools=ScriptedToolCaller(),
    )

    assert refused.status_code == 400
    assert refused.json() == {"code": ErrorCode.VALIDATION.value}
    assert refused.headers["content-type"].split(";")[0].strip() == "application/json"
    assert empty_gateway.call_count == 0
    assert _stored_messages(stream_env, refused_session) == []


def test_a_spent_rate_limit_is_a_typed_429_and_opens_no_stream(
    stream_env: StreamRouteEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a streaming route that skipped the design's limit entirely, or
    counted it after the turn — the one control bounding a runaway client, and
    the one a new route is likeliest to forget.

    The window is spent through the limiter itself rather than by sending
    thirty turns, so the case costs one refused request instead of thirty full
    turns; the 31st message is the request under test, and the gateway staged
    with nothing proves no turn was started. Both the current window and the
    next one are filled, because the route stamps its own `datetime.now(UTC)`
    and a window boundary crossed between the two calls would otherwise let the
    31st message through and make this case flaky.
    """
    assert RATE_LIMIT_MESSAGES == 30

    session_id = uuid.uuid4()
    gateway = StagedGateway([])

    async def scenario(client: httpx.AsyncClient) -> httpx.Response:
        now = datetime.now(UTC)
        for moment in (now, now + timedelta(seconds=RATE_LIMIT_WINDOW_SECONDS)):
            for _ in range(RATE_LIMIT_MESSAGES):
                await check_rate_limit(
                    app.state.redis,
                    tenant_id=stream_env.tenant_a.tenant_id,
                    session_id=str(session_id),
                    now=moment,
                )
        return await client.post(
            "/chat/stream",
            json={"message": MESSAGE},
            headers={"Authorization": f"Bearer {_token(stream_env.tenant_a, session_id)}"},
        )

    response = _drive(
        monkeypatch=monkeypatch,
        scenario=scenario,
        gateway_handler=gateway.handler,
        tools=ScriptedToolCaller(),
    )

    assert response.status_code == 429
    assert response.json() == {"code": ErrorCode.POLICY_BLOCKED.value}
    assert response.headers["content-type"].split(";")[0].strip() == "application/json"
    assert gateway.call_count == 0
    assert _stored_messages(stream_env, session_id) == []


# --- a failure after the first byte -------------------------------------------


def test_an_unreachable_mcp_server_mid_stream_is_a_typed_error_event(
    stream_env: StreamRouteEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a `ToolError` escaping the streaming route unmapped: the MCP
    handshake fails inside the generator, where a raise cannot become a status
    line any more — the response is already 200 — so without decision E1 the
    client gets a truncated body, or Starlette's own error, with nothing typed
    in it at all.

    The DATABASE session is the route's half of the plan's first risk: it is
    opened around a response BODY now, so an engine still holding a checked-out
    connection after the response completed is the leak nothing else would see.
    Read off the engine the LIFESPAN built, which nothing in this case replaced.
    """
    session_id = uuid.uuid4()
    gateway = StagedGateway([])

    async def scenario(client: httpx.AsyncClient) -> tuple[httpx.Response, int]:
        response = await client.post(
            "/chat/stream",
            json={"message": MESSAGE},
            headers={"Authorization": f"Bearer {_token(stream_env.tenant_a, session_id)}"},
        )
        checked_out: int = app.state.engine.pool.checkedout()
        return response, checked_out

    response, checked_out = _drive(
        monkeypatch=monkeypatch,
        scenario=scenario,
        gateway_handler=gateway.handler,
        tools=UnreachableToolCaller(),
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].split(";")[0].strip() == "text/event-stream"

    frames = parse_frames(response.text)
    errors = _of(frames, ERROR)
    assert len(errors) == 1
    assert errors[0].data == {"code": ErrorCode.INTERNAL.value}
    assert _names(frames)[-2:] == [ERROR, DONE]
    assert _of(frames, TOKEN) == []
    assert _of(frames, REPLY) == []
    assert gateway.call_count == 0
    assert checked_out == 0, "the turn's database session outlived the stream"
