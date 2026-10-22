"""The one error map, and the three wire statuses it changes.

The agent-api turned a typed error code into an HTTP status in two places that
disagreed: the proxied routes (`core.deps.STATUS_FOR_CODE`) and the chat
service (`services.chat.STATUS_FOR_CODE`). The two are folded into one map in
`core.deps` with a row for every one of the eight `ErrorCode` members, so one
code crosses the wire as one status and nothing falls through to 500 by
accident. Two answers stay explicit and are NOT a code lookup: an
unauthenticated caller is the middleware's literal 401, and the rate limit is a
literal 429 where it is caught.

Every case below is red until that fold lands:
- the one map has only five rows today (no SOD_CONFLICT, no
  IDEMPOTENCY_CONFLICT, no INTERNAL), so the exact-dict case fails on the
  mismatch;
- `services.chat` still defines its own `STATUS_FOR_CODE` and `_typed`, so the
  two `not hasattr` checks fail;
- a chat FORBIDDEN refusal is 401 today (chat's own map), so the chat route
  case gets 401 not 403;
- a proxied SOD_CONFLICT has no row, so it falls through to 500 not 409.

Case 5 (the rate limit stays a literal 429) may already pass: it guards a
behaviour this change keeps rather than one it introduces. It fails only if the
fold makes the rate limit a POLICY_BLOCKED lookup, which would answer 409.

Cost and isolation. Cases 1, 2 and 4 open no socket: they read the map, call
`_rendered` on a plain FAILED `TurnState`, and drive the proxied route with a
scripted caller the way `test_me_routes.py` does. Cases 3 and 5 drive `/chat`
over the ASGI client against the compose stack the way `test_chat_route.py`
and `test_chat_stream.py` do: one throwaway database with its schema, the
LangGraph checkpoint tables and two obviously fake tenants built ONCE for the
module, a Redis index dedicated to the suite that nothing
flushes, a staged gateway and a scripted tool caller, so no socket opens to
the gateway or the MCP server.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from helpdesk.agent_api.agent.state import TurnOutcome, TurnState
from helpdesk.agent_api.api.router import app
from helpdesk.agent_api.api.routes import chat as chat_routes
from helpdesk.agent_api.core import deps
from helpdesk.agent_api.services import chat as chat_service
from helpdesk.agent_api.services import tool_proxy as tool_proxy_deps
from helpdesk.agent_api.services.mcp_client import (
    RemoteForbiddenError,
    RemoteSodConflictError,
    RemoteToolError,
    ToolCaller,
)
from helpdesk.agent_api.services.model_gateway.client import ModelClient
from helpdesk.agent_api.services.ratelimit import (
    RATE_LIMIT_MESSAGES,
    RATE_LIMIT_WINDOW_SECONDS,
    check_rate_limit,
)
from helpdesk.shared.auth.jwt import issue_token
from helpdesk.shared.core.config import Settings
from helpdesk.shared.core.db import tenant_session
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.models import Department, Role, System, Tenant, User
from tests.agent_api.conftest import AgentDb

# --- the one complete map -----------------------------------------------------


def test_the_one_map_has_a_row_for_every_code() -> None:
    """Catches the five-row map this change replaces, where SOD_CONFLICT,
        IDEMPOTENCY_CONFLICT and INTERNAL have no row and fall through to 500: a
        SOD_CONFLICT or an idempotency clash a user could act on would read as an
        outage they cannot.

        Asserted as an EXACT mapping, not key by key: a map read
        one code at a time stays green when a row is missing, so only the whole-dict
        equality and the length catch a short map. Every value is a literal:
    400, 403, 404, three 409s, 503, 500.
    """
    assert dict(deps.STATUS_FOR_CODE) == {
        ErrorCode.VALIDATION: 400,
        ErrorCode.FORBIDDEN: 403,
        ErrorCode.NOT_FOUND: 404,
        ErrorCode.SOD_CONFLICT: 409,
        ErrorCode.POLICY_BLOCKED: 409,
        ErrorCode.IDEMPOTENCY_CONFLICT: 409,
        ErrorCode.REQUEST_NOT_PENDING: 409,
        ErrorCode.TOOL_DISABLED: 503,
        ErrorCode.INTERNAL: 500,
    }
    assert len(deps.STATUS_FOR_CODE) == 9


# --- chat no longer carries its own map ---------------------------------------


def test_chat_is_no_longer_its_own_map() -> None:
    """Catches the chat service keeping its own second map and its own `_typed`:
    today `services.chat.STATUS_FOR_CODE` reads FORBIDDEN as 401 and
    POLICY_BLOCKED as 429, so a chat refusal and a proxied refusal answer one
    code two ways. The two `not hasattr` checks fail until both are dropped.

    `_rendered` on a FAILED turn raises `ToolError` instead of
    returning a `JSONResponse`. The raised error carries the turn's `error_code`
    so the app-level handler in `router.py` is the one edge that renders it.
    A plain frozen `TurnState` with no node run and no socket, so the only thing
    that can flip the raise vs. return is whether the service refactoring landed.
    """
    assert not hasattr(chat_service, "STATUS_FOR_CODE")
    assert not hasattr(chat_service, "_typed")

    failed = TurnState(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        tenant_slug="fake-tenant",
        user_message="Obviously fake message.",
        outcome=TurnOutcome.FAILED,
        error_code=ErrorCode.FORBIDDEN,
    )
    with pytest.raises(ToolError) as exc_info:
        chat_service._rendered(failed)
    assert exc_info.value.code == ErrorCode.FORBIDDEN


# --- the proxied-route harness (no socket, like test_me_routes.py) ------------

REQUESTS_PATH = "/me/requests"

# Obviously fake, tests only: none of these is a credential, and
# nothing in this file connects to any of these hosts.
PROXY_JWT_SECRET = "error-map-proxy-test-jwt-secret-0123456789abcdef"
PROXY_MCP_URL = "http://mcp.invalid:8081/mcp"
PROXY_DATABASE_URL = "postgresql+asyncpg://fake:fake@db.invalid:5432/fake_error_map"
PROXY_REDIS_URL = "redis://redis.invalid:6379/0"
PROXY_QDRANT_URL = "http://qdrant.invalid:6333"

PROXY_TENANT_ID = uuid.UUID("11111111-1111-5111-8111-111111111111")
PROXY_USER_ID = uuid.UUID("22222222-2222-5222-8222-222222222222")
PROXY_DEPARTMENT_ID = uuid.UUID("33333333-3333-5333-8333-333333333333")
PROXY_SESSION_ID = uuid.UUID("44444444-4444-5444-8444-444444444444")


class RefusingToolCaller:
    """A caller whose session opens and whose tool call raises a typed error.

    The same shape `test_me_routes.py` uses: the handshake succeeds, so the
    token gate cannot decide the case and only the `except ToolError` branch
    and the one map can set the status.
    """

    def __init__(self, error: RemoteToolError) -> None:
        self._error = error
        self.entered = 0

    async def __aenter__(self) -> ToolCaller:
        self.entered += 1
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def call_tool(self, name: str, arguments: BaseModel) -> Mapping[str, Any]:
        raise self._error


class CallerSpy:
    """Records nothing more than the built caller; hands back the stub."""

    def __init__(self, caller: ToolCaller) -> None:
        self._caller = caller

    def build(self, settings: Settings, bearer_token: str) -> ToolCaller:
        return self._caller


@pytest.fixture
def proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Obviously fake settings. Nothing here is dialled: no lifespan is entered."""
    monkeypatch.setenv("DATABASE_URL", PROXY_DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", PROXY_REDIS_URL)
    monkeypatch.setenv("QDRANT_URL", PROXY_QDRANT_URL)
    monkeypatch.setenv("JWT_SECRET", PROXY_JWT_SECRET)
    monkeypatch.setenv("MCP_URL", PROXY_MCP_URL)


def _proxy_token() -> str:
    """One verified-looking token for one fake person and one conversation."""
    return issue_token(
        {
            "sub": str(PROXY_USER_ID),
            "tenant_id": str(PROXY_TENANT_ID),
            "department_id": str(PROXY_DEPARTMENT_ID),
            "principals": ["employee"],
            "sid": str(PROXY_SESSION_ID),
        },
        PROXY_JWT_SECRET,
        datetime.now(UTC),
    )


def _get_proxy(
    *, monkeypatch: pytest.MonkeyPatch, path: str, caller: ToolCaller, token: str
) -> httpx.Response:
    """One GET against the app, with the tool caller replaced at the proxy seam.

    The lifespan is NOT entered: `call_tool_route` reads nothing off
    `app.state`, so entering it would buy sockets no case here uses.
    """
    monkeypatch.setattr(tool_proxy_deps, "build_tool_caller", CallerSpy(caller).build)

    async def runner() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path, headers={"Authorization": f"Bearer {token}"})

    return asyncio.run(runner())


def test_a_conflict_through_a_proxied_route_answers_409(
    proxy_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a proxied route where SOD_CONFLICT has no row and falls through
    to 500: a segregation-of-duties clash the user could resolve reads as an
    outage they cannot. The bearer VERIFIES, so the token gate cannot decide
    this case and only the one map's SOD_CONFLICT row can
    turn the tool's refusal into 409.

    The caller raises `RemoteSodConflictError`, exactly the class
    `test_agent_mcp_client.py` maps `"SOD_CONFLICT"` to, so the code reaching
    `call_tool_route`'s `except ToolError` is the real one and not a stand-in.
    """
    caller = RefusingToolCaller(RemoteSodConflictError(ErrorCode.SOD_CONFLICT.value))

    response = _get_proxy(
        monkeypatch=monkeypatch, path=REQUESTS_PATH, caller=caller, token=_proxy_token()
    )

    assert response.status_code == 409
    assert response.json() == {"code": ErrorCode.SOD_CONFLICT.value}
    assert caller.entered == 1


# --- the chat route harness (compose stack, like test_chat_route.py) ----------

NOW = datetime(2026, 6, 2, 10, 15, tzinfo=UTC)

GATEWAY_URL = "http://gateway.invalid:8080"
MCP_URL = "http://mcp.invalid:8081/mcp"

TENANT_A = "error-map-tenant-a"

# Obviously fake, tests only: none of these is a credential.
CHAT_API_KEYS: dict[str, str] = {TENANT_A: f"fake-key-{TENANT_A}"}
CHAT_JWT_SECRET = "error-map-chat-test-jwt-secret-0123456789abcdef"
GATEWAY_API_KEYS = ",".join(f"{slug}:{key}" for slug, key in CHAT_API_KEYS.items())

MESSAGE = "Please give me read access to fake payroll."


@dataclass(frozen=True)
class SeededTenant:
    tenant_id: uuid.UUID
    tenant_slug: str
    department_id: uuid.UUID
    user_id: uuid.UUID
    role_id: uuid.UUID


@dataclass(frozen=True)
class ChatEnv:
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
            email=f"fake.person1@{slug}.example.test",
            display_name=f"Fake Person 1 ({slug})",
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


@pytest.fixture(scope="session")
def chat_db(agent_db: AgentDb, redis_base_url: str) -> ChatEnv:
    """The shared agent database with this suite's one tenant seeded in it.

    A case's namespace is its own session id.
    """
    tenant_a = asyncio.run(_seed(agent_db.factory, TENANT_A))
    return ChatEnv(database_url=agent_db.url, redis_url=redis_base_url, tenant_a=tenant_a)


@pytest.fixture
def chat_env(chat_db: ChatEnv, monkeypatch: pytest.MonkeyPatch) -> ChatEnv:
    """Point the app's lifespan at the seeded database and the test Redis index."""
    monkeypatch.setenv("DATABASE_URL", chat_db.database_url)
    monkeypatch.setenv("REDIS_URL", chat_db.redis_url)
    monkeypatch.setenv("JWT_SECRET", CHAT_JWT_SECRET)
    monkeypatch.setenv("GATEWAY_URL", GATEWAY_URL)
    monkeypatch.setenv("GATEWAY_API_KEYS", GATEWAY_API_KEYS)
    monkeypatch.setenv("MCP_URL", MCP_URL)
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    return chat_db


class StagedGateway:
    """Answers staged in order; a request past the last one fails the test.

    Staged with NOTHING in both chat cases: a route that started a turn before
    the refusal it is under test for asks for an answer nobody staged.
    """

    def __init__(self, answers: list[str]) -> None:
        self._answers = list(answers)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        raise AssertionError(
            f"the turn made {len(self.requests)} model requests; "
            f"only {len(self._answers)} were staged"
        )

    @property
    def call_count(self) -> int:
        return len(self.requests)


class ScriptedToolCaller:
    """A `ToolCaller` answering one scripted payload per call, in order."""

    def __init__(self, *answers: Mapping[str, Any]) -> None:
        self._answers = list(answers)
        self.calls: list[str] = []
        self.entered = 0

    async def __aenter__(self) -> ToolCaller:
        self.entered += 1
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def call_tool(self, name: str, arguments: BaseModel) -> Mapping[str, Any]:
        self.calls.append(name)
        index = len(self.calls) - 1
        if index >= len(self._answers):
            raise AssertionError(
                f"the turn made {len(self.calls)} tool calls; "
                f"only {len(self._answers)} were scripted"
            )
        return self._answers[index]


class RefusingHandshakeToolCaller:
    """A caller whose handshake fails with a typed error, the way a refused
    MCP initialize does. The error reaches `/chat`'s `except ToolError`, where
    the one map decides the status.
    """

    def __init__(self, error: RemoteToolError) -> None:
        self._error = error
        self.entered = 0

    async def __aenter__(self) -> ToolCaller:
        self.entered += 1
        raise self._error

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def call_tool(self, name: str, arguments: BaseModel) -> Mapping[str, Any]:
        raise AssertionError("a caller whose handshake failed must never be called")


def _chat_token(tenant: SeededTenant, session_id: uuid.UUID) -> str:
    return issue_token(
        {
            "sub": str(tenant.user_id),
            "tenant_id": str(tenant.tenant_id),
            "department_id": str(tenant.department_id),
            "principals": ["employee"],
            "sid": str(session_id),
        },
        CHAT_JWT_SECRET,
        datetime.now(UTC),
    )


def _drive(
    *,
    monkeypatch: pytest.MonkeyPatch,
    scenario: Callable[[httpx.AsyncClient], Awaitable[httpx.Response]],
    gateway_handler: Callable[[httpx.Request], httpx.Response],
    tools: ToolCaller,
) -> httpx.Response:
    """Enter the app's lifespan, stage the gateway and the tool caller, run one scenario."""
    monkeypatch.setattr(chat_routes, "build_tool_caller", lambda settings, bearer_token: tools)

    async def runner() -> httpx.Response:
        async with app.router.lifespan_context(app):
            await app.state.model.aclose()
            app.state.model = ModelClient(
                base_url=GATEWAY_URL,
                api_keys=CHAT_API_KEYS,
                transport=httpx.MockTransport(gateway_handler),
            )
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await scenario(client)

    return asyncio.run(runner())


def test_a_forbidden_refusal_on_chat_answers_403(
    chat_env: ChatEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches the chat route answering a FORBIDDEN as 401 through chat's old
    map: a permission refusal the caller could read reads instead as "you are
    not logged in", the one answer an unauthenticated caller already gets. The
    token VERIFIES (the middleware let the request through) so the 401 path
    cannot decide this case; only the one map's FORBIDDEN row can, and it is
    403.

    The caller raises `RemoteForbiddenError` at the handshake, which reaches
    `/chat`'s `except ToolError` outside every node. The gateway is staged with
    nothing, so a turn that started at all fails inside the stub rather than
    showing up as a count afterwards.
    """
    session_id = uuid.uuid4()
    gateway = StagedGateway([])
    tools = RefusingHandshakeToolCaller(RemoteForbiddenError(ErrorCode.FORBIDDEN.value))

    async def scenario(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post(
            "/chat",
            json={"message": MESSAGE},
            headers={"Authorization": f"Bearer {_chat_token(chat_env.tenant_a, session_id)}"},
        )

    response = _drive(
        monkeypatch=monkeypatch, scenario=scenario, gateway_handler=gateway.handler, tools=tools
    )

    assert response.status_code == 403
    assert response.json() == {"code": ErrorCode.FORBIDDEN.value}
    assert gateway.call_count == 0
    assert tools.entered == 1


def test_the_rate_limit_still_answers_429(
    chat_env: ChatEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a fold that routed the rate limit through the code map: the one
    map reads POLICY_BLOCKED as 409, but the rate limit is a literal 429 where
    it is caught (an explicit answer). A rate-limited `/chat`
    that answered 409 would tell a client its action conflicts with state when
    the truth is "slow down".

    The window is spent through the limiter itself rather than by sending
    thirty turns, and both the current window and the next are filled so a
    boundary crossed between the two calls cannot let the request under test
    through. The gateway staged with nothing proves no turn was started.
    """
    assert RATE_LIMIT_MESSAGES == 30

    session_id = uuid.uuid4()
    gateway = StagedGateway([])
    tools = ScriptedToolCaller()

    async def scenario(client: httpx.AsyncClient) -> httpx.Response:
        now = datetime.now(UTC)
        for moment in (now, now + timedelta(seconds=RATE_LIMIT_WINDOW_SECONDS)):
            for _ in range(RATE_LIMIT_MESSAGES):
                await check_rate_limit(
                    app.state.redis,
                    tenant_id=chat_env.tenant_a.tenant_id,
                    session_id=str(session_id),
                    now=moment,
                )
        return await client.post(
            "/chat",
            json={"message": MESSAGE},
            headers={"Authorization": f"Bearer {_chat_token(chat_env.tenant_a, session_id)}"},
        )

    response = _drive(
        monkeypatch=monkeypatch, scenario=scenario, gateway_handler=gateway.handler, tools=tools
    )

    assert response.status_code == 429
    assert response.json() == {"code": ErrorCode.POLICY_BLOCKED.value}
    assert gateway.call_count == 0
    assert tools.entered == 0
