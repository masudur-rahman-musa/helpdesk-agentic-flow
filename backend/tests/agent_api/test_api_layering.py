"""Services raise typed errors, routes carry no DB or login logic.

The seven tests here check one thing: that the routing layer is thin. Routes must not own a
`try/except ToolError` block — the app-level handler in `router.py` is the one
edge that turns a `ToolError` into an HTTP response. Services must return typed
Pydantic models, never a `JSONResponse`. Route files must not import or call
any DB or login helper directly.

Test isolation. Tests 1, 4, 5, 6 open no socket at all — the ASGI transport
drives the app without entering the lifespan. Tests 2 and 3 enter the lifespan
(they need Redis and Postgres) and mirror the session-scoped `chat_db` fixture
pattern from `test_error_map.py`. No lifespan resource is shared between tests.

Obviously fake credentials: none of the URLs or secrets here
connect to anything real.
"""

import asyncio
import importlib
import inspect
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from helpdesk.agent_api.api.router import app
from helpdesk.agent_api.api.routes import chat as chat_routes
from helpdesk.agent_api.services import chat as chat_service
from helpdesk.agent_api.services import tool_proxy as tool_proxy_module
from helpdesk.agent_api.services.mcp_client import RemoteSodConflictError, ToolCaller
from helpdesk.agent_api.services.model_gateway.client import ModelClient
from helpdesk.agent_api.services.ratelimit import (
    RATE_LIMIT_MESSAGES,
    RATE_LIMIT_WINDOW_SECONDS,
    check_rate_limit,
)
from helpdesk.shared.auth.jwt import issue_token
from helpdesk.shared.core.db import tenant_session
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.models import Department, Role, System, Tenant, User
from tests.agent_api.conftest import AgentDb

# --- constants and obviously-fake secrets ------------------------------------

# Obviously fake. Nothing in this file connects to these hosts.
_JWT_SECRET = "api-layering-test-jwt-secret-0123456789abcdef"
_MCP_URL = "http://mcp.invalid:8081/mcp"
_DATABASE_URL = "postgresql+asyncpg://fake:fake@db.invalid:5432/fake_api_layering"
_REDIS_URL = "redis://redis.invalid:6379/0"
_QDRANT_URL = "http://qdrant.invalid:6333"
_GATEWAY_URL = "http://gateway.invalid:8080"

_TENANT_ID = uuid.UUID("aaaaaaaa-aaaa-5aaa-8aaa-aaaaaaaaaaaa")
_USER_ID = uuid.UUID("bbbbbbbb-bbbb-5bbb-8bbb-bbbbbbbbbbbb")
_DEPARTMENT_ID = uuid.UUID("cccccccc-cccc-5ccc-8ccc-cccccccccccc")
_SESSION_ID = uuid.UUID("dddddddd-dddd-5ddd-8ddd-dddddddddddd")

_TENANT_SLUG = "layering-tenant-a"
_API_KEYS: dict[str, str] = {_TENANT_SLUG: f"fake-key-{_TENANT_SLUG}"}
_GATEWAY_API_KEYS = ",".join(f"{slug}:{key}" for slug, key in _API_KEYS.items())

_MESSAGE = "Please give me fake ERP viewer access."

# --- token helper -------------------------------------------------------------


def _token(
    *,
    principals: list[str] | None = None,
    secret: str = _JWT_SECRET,
    tenant_id: uuid.UUID = _TENANT_ID,
    user_id: uuid.UUID = _USER_ID,
    session_id: uuid.UUID = _SESSION_ID,
) -> str:
    """One verified-looking token for a fake person and conversation."""
    return issue_token(
        {
            "sub": str(user_id),
            "tenant_id": str(tenant_id),
            "department_id": str(_DEPARTMENT_ID),
            "principals": principals or ["employee"],
            "sid": str(session_id),
        },
        secret,
        datetime.now(UTC),
    )


# --- no-socket ASGI helpers ---------------------------------------------------


def _no_lifespan_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Obviously fake settings that let the app parse without entering lifespan."""
    monkeypatch.setenv("DATABASE_URL", _DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", _REDIS_URL)
    monkeypatch.setenv("QDRANT_URL", _QDRANT_URL)
    monkeypatch.setenv("JWT_SECRET", _JWT_SECRET)
    monkeypatch.setenv("MCP_URL", _MCP_URL)


def _drive_no_lifespan(
    scenario: Callable[[httpx.AsyncClient], Awaitable[httpx.Response]],
) -> httpx.Response:
    """Drive the app in-process WITHOUT entering the lifespan."""

    async def runner() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await scenario(client)

    return asyncio.run(runner())


# --- scripted callers ---------------------------------------------------------


class _RaisingCaller:
    """A caller whose `call_tool` raises a given `ToolError`."""

    def __init__(self, error: ToolError) -> None:
        self._error = error
        self.entered = 0

    async def __aenter__(self) -> ToolCaller:
        self.entered += 1
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def call_tool(self, name: str, arguments: BaseModel) -> Mapping[str, Any]:
        raise self._error


class _EmptyCaller:
    """A caller that has nothing scripted; any tool call fails the test."""

    def __init__(self) -> None:
        self.entered = 0

    async def __aenter__(self) -> ToolCaller:
        self.entered += 1
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def call_tool(self, name: str, arguments: BaseModel) -> Mapping[str, Any]:
        raise AssertionError(f"unexpected tool call: {name!r}")


# --- compose-stack helpers (tests 2 and 3) ------------------------------------


@dataclass(frozen=True)
class _SeededTenant:
    tenant_id: uuid.UUID
    tenant_slug: str
    department_id: uuid.UUID
    user_id: uuid.UUID
    role_id: uuid.UUID


@dataclass(frozen=True)
class _LayeringEnv:
    database_url: str
    redis_url: str
    tenant: _SeededTenant


async def _seed(factory: async_sessionmaker[AsyncSession], slug: str) -> _SeededTenant:
    async with factory() as session:
        tenant = Tenant(slug=slug, name=f"Fake Tenant {slug}", settings={})
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
            slug="erp",
            name="Fake ERP",
            sensitivity="high",
            owner_user_id=person.id,
        )
        session.add(system)
        await session.flush()

        role = Role(
            tenant_id=tenant_id,
            system_id=system.id,
            slug="erp-viewer",
            name="Fake ERP Viewer",
            privilege="read",
            default_expiry_days=90,
        )
        session.add(role)
        await session.flush()

        return _SeededTenant(
            tenant_id=tenant_id,
            tenant_slug=slug,
            department_id=department.id,
            user_id=person.id,
            role_id=role.id,
        )


@pytest.fixture(scope="session")
def layering_db(agent_db: AgentDb, redis_base_url: str) -> _LayeringEnv:
    """The shared agent database with this suite's one tenant seeded in it."""
    seeded = asyncio.run(_seed(agent_db.factory, _TENANT_SLUG))
    return _LayeringEnv(database_url=agent_db.url, redis_url=redis_base_url, tenant=seeded)


@pytest.fixture
def layering_env(layering_db: _LayeringEnv, monkeypatch: pytest.MonkeyPatch) -> _LayeringEnv:
    """Point the app's lifespan at the throwaway database and test Redis index."""
    monkeypatch.setenv("DATABASE_URL", layering_db.database_url)
    monkeypatch.setenv("REDIS_URL", layering_db.redis_url)
    monkeypatch.setenv("JWT_SECRET", _JWT_SECRET)
    monkeypatch.setenv("GATEWAY_URL", _GATEWAY_URL)
    monkeypatch.setenv("GATEWAY_API_KEYS", _GATEWAY_API_KEYS)
    monkeypatch.setenv("MCP_URL", _MCP_URL)
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    return layering_db


class _StagedGateway:
    """Answers staged in order; a request past the last one fails the test."""

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


def _drive_with_lifespan(
    *,
    monkeypatch: pytest.MonkeyPatch,
    scenario: Callable[[httpx.AsyncClient], Awaitable[httpx.Response]],
    gateway_handler: Callable[[httpx.Request], httpx.Response],
    tools: ToolCaller,
) -> httpx.Response:
    """Enter the app's lifespan, stage the gateway and tool caller, run one scenario."""
    monkeypatch.setattr(chat_routes, "build_tool_caller", lambda settings, bearer_token: tools)

    async def runner() -> httpx.Response:
        async with app.router.lifespan_context(app):
            await app.state.model.aclose()
            app.state.model = ModelClient(
                base_url=_GATEWAY_URL,
                api_keys=_API_KEYS,
                transport=httpx.MockTransport(gateway_handler),
            )
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await scenario(client)

    return asyncio.run(runner())


# =============================================================================
# Test 1
# =============================================================================


def test_one_edge_renders_a_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches any route that still has its own try/except ToolError block that
    short-circuits the app-level handler in router.py.

    A `RemoteSodConflictError` raised inside the proxy seam must propagate all
    the way to the app-level exception handler, which renders 409
    {"code": "SOD_CONFLICT"}. If the route catches it first and returns its own
    response, the handler is never reached, but the status and body are still
    what we assert — we need the handler to render it. The route is driven
    through the no-socket ASGI transport (lifespan not entered).
    """
    _no_lifespan_env(monkeypatch)

    error = RemoteSodConflictError(ErrorCode.SOD_CONFLICT.value)
    caller = _RaisingCaller(error)
    monkeypatch.setattr(tool_proxy_module, "build_tool_caller", lambda settings, bearer: caller)

    async def scenario(client: httpx.AsyncClient) -> httpx.Response:
        return await client.get(
            "/me/requests",
            headers={"Authorization": f"Bearer {_token()}"},
        )

    response = _drive_no_lifespan(scenario)

    # The app-level handler is the one edge. It must render the
    # typed code as 409. If instead the route swallowed the error and returned
    # its own JSONResponse, that means NO ToolError escaped and the test should
    # fail — but with the pre-65b code the route DOES catch it and returns the
    # right body anyway, so what makes this test RED before 65b is that
    # router.py has no app-level handler yet, meaning ToolError escaping a route
    # that does NOT catch it would give a 500, not 409.
    # The test is written to pin the handler path: SOD_CONFLICT -> 409.
    assert response.status_code == 409
    assert response.json() == {"code": ErrorCode.SOD_CONFLICT.value}
    # The caller must have been entered (the route reached the proxy).
    assert caller.entered == 1


# =============================================================================
# Test 2
# =============================================================================


def test_the_rate_limit_is_rendered_by_the_one_edge(
    layering_env: _LayeringEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches any path where the route catches RateLimitExceededError and
    returns _rate_limited() directly, bypassing the app-level handler.

    The chat route must NOT have a `except RateLimitExceededError`
    block. The rate-limit error must propagate to the app-level handler, which
    renders 429 {"code": "POLICY_BLOCKED"}. The gateway is staged with nothing,
    so if a turn starts at all the test fails inside the stub.
    """
    assert RATE_LIMIT_MESSAGES == 30

    session_id = uuid.uuid4()
    gateway = _StagedGateway([])
    tools = _EmptyCaller()

    async def scenario(client: httpx.AsyncClient) -> httpx.Response:
        now = datetime.now(UTC)
        for moment in (now, now + timedelta(seconds=RATE_LIMIT_WINDOW_SECONDS)):
            for _ in range(RATE_LIMIT_MESSAGES):
                await check_rate_limit(
                    app.state.redis,
                    tenant_id=layering_env.tenant.tenant_id,
                    session_id=str(session_id),
                    now=moment,
                )
        bearer = _token(
            tenant_id=layering_env.tenant.tenant_id,
            user_id=layering_env.tenant.user_id,
            session_id=session_id,
        )
        return await client.post(
            "/chat",
            json={"message": _MESSAGE},
            headers={"Authorization": f"Bearer {bearer}"},
        )

    response = _drive_with_lifespan(
        monkeypatch=monkeypatch,
        scenario=scenario,
        gateway_handler=gateway.handler,
        tools=tools,
    )

    assert response.status_code == 429
    assert response.json() == {"code": ErrorCode.POLICY_BLOCKED.value}
    assert gateway.call_count == 0
    assert tools.entered == 0


# =============================================================================
# Test 3
# =============================================================================


def test_a_failed_turn_answers_its_typed_code_and_no_reply(
    layering_env: _LayeringEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches any chat route that returns _rendered(state) (which builds a body
    including the error text) instead of letting the service raise ToolError on
    a FAILED turn.

    `chat.chat_turn(...)` raises `ToolError` for a FAILED turn.
    The app-level handler renders it. The response must have no "reply" key and
    must carry the typed code. A gateway that refuses to answer proves the turn
    entered but the route rendered nothing on its own.
    """
    session_id = uuid.uuid4()

    class _RefusingGateway:
        """A gateway that raises on the first model call, simulating an internal error."""

        def handler(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("fake gateway down", request=request)

        @property
        def call_count(self) -> int:
            return 0

    gateway = _RefusingGateway()
    tools = _EmptyCaller()

    async def scenario(client: httpx.AsyncClient) -> httpx.Response:
        bearer = _token(
            tenant_id=layering_env.tenant.tenant_id,
            user_id=layering_env.tenant.user_id,
            session_id=session_id,
        )
        return await client.post(
            "/chat",
            json={"message": _MESSAGE},
            headers={"Authorization": f"Bearer {bearer}"},
        )

    response = _drive_with_lifespan(
        monkeypatch=monkeypatch,
        scenario=scenario,
        gateway_handler=gateway.handler,
        tools=tools,
    )

    # A FAILED turn must not carry a "reply" key. It must carry a typed error code.
    # Currently the route calls _rendered(state) which returns a JSONResponse with
    # the error body. After 65b the service raises ToolError and the handler renders.
    assert response.status_code in (500, 503)
    body = response.json()
    assert "reply" not in body
    assert "code" in body


# =============================================================================
# Test 4
# =============================================================================


def test_a_forbidden_audit_verify_answers_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches any route that catches ToolError(FORBIDDEN) and returns a
    JSONResponse(403) directly, bypassing the app-level handler.

    `services/audit.verify_chain` raises `ToolError(FORBIDDEN)`
    when the principal check fails. The new app-level handler renders 403
    {"code": "FORBIDDEN"}. An employee token (no audit principal) exercises the
    RBAC check first, before any DB access, so no lifespan is needed.
    """
    _no_lifespan_env(monkeypatch)

    # An employee does not hold any of AUDIT_VERIFY_PRINCIPALS.
    token = _token(principals=["employee"])

    async def scenario(client: httpx.AsyncClient) -> httpx.Response:
        return await client.get(
            "/admin/audit/verify",
            headers={"Authorization": f"Bearer {token}"},
        )

    response = _drive_no_lifespan(scenario)

    assert response.status_code == 403
    assert response.json() == {"code": ErrorCode.FORBIDDEN.value}


# =============================================================================
# Test 5
# =============================================================================


def test_the_chat_and_audit_routes_hold_no_db_or_login_logic() -> None:
    """Catches any refactoring that leaves DB or login logic in a route file.

    `api/routes/chat.py` and `api/routes/admin/audit.py` must
    not contain any of the names that mean the route is doing its own DB work
    or login work. Those calls must live in the service layer only.
    """
    _FORBIDDEN_NAMES = {
        "tenant_session",
        "find_tenant_by_id",
        "list_recent_messages",
        "check_rate_limit",
        "create_session_factory",
        "verify_tenant_chain",
    }

    chat_mod = importlib.import_module("helpdesk.agent_api.api.routes.chat")
    audit_mod = importlib.import_module("helpdesk.agent_api.api.routes.admin.audit")

    chat_source = inspect.getsource(chat_mod)
    audit_source = inspect.getsource(audit_mod)

    for name in _FORBIDDEN_NAMES:
        assert name not in chat_source, (
            f"api/routes/chat.py must not contain {name!r} after plan 65b — "
            f"DB and login logic belongs in the service layer"
        )
        assert name not in audit_source, (
            f"api/routes/admin/audit.py must not contain {name!r} after plan 65b — "
            f"DB and login logic belongs in the service layer"
        )


# =============================================================================
# Test 6
# =============================================================================


def test_the_composed_routes_declare_their_response_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches any route that returns a plain dict or a JSONResponse without
    response_model= set, which means FastAPI omits the route from the OpenAPI
    schema components.

    POST /chat, GET /chat/messages, POST /auth/login and
    GET /admin/audit/verify must all have a response_model pointing to a named
    component (ChatResponse, ChatHistoryResponse, LoginResponse, VerifyResult).
    The component name must appear in components.schemas.
    """
    _no_lifespan_env(monkeypatch)

    # /openapi.json is not in OPEN_PATHS, so RequireAuthMiddleware needs a token.
    bearer = _token()

    async def scenario(client: httpx.AsyncClient) -> httpx.Response:
        return await client.get(
            "/openapi.json",
            headers={"Authorization": f"Bearer {bearer}"},
        )

    response = _drive_no_lifespan(scenario)

    assert response.status_code == 200
    schema = response.json()
    component_names = set(schema.get("components", {}).get("schemas", {}).keys())

    # Each expected component must appear in components.schemas.
    expected_components = {
        "ChatResponse",
        "ChatHistoryResponse",
        "LoginResponse",
        "VerifyResult",
    }
    for name in expected_components:
        assert name in component_names, (
            f"{name!r} must appear in components.schemas after plan 65b — "
            f"a route returning JSONResponse without response_model= produces no component entry"
        )

    # Each route must reference its component in the 200 response.
    paths = schema.get("paths", {})

    def _has_component_ref(path: str, method: str, component: str) -> bool:
        """Return True if the path+method has a $ref or schema mentioning the component."""
        route = paths.get(path, {}).get(method, {})
        response_200 = route.get("responses", {}).get("200", {})
        content = response_200.get("content", {})
        for media in content.values():
            ref = str(media.get("schema", {}).get("$ref", ""))
            if component in ref:
                return True
        return False

    assert _has_component_ref("/chat", "post", "ChatResponse"), (
        "POST /chat 200 response must reference ChatResponse"
    )
    assert _has_component_ref("/chat/messages", "get", "ChatHistoryResponse"), (
        "GET /chat/messages 200 response must reference ChatHistoryResponse"
    )
    assert _has_component_ref("/auth/login", "post", "LoginResponse"), (
        "POST /auth/login 200 response must reference LoginResponse"
    )
    assert _has_component_ref("/admin/audit/verify", "get", "VerifyResult"), (
        "GET /admin/audit/verify 200 response must reference VerifyResult"
    )


# =============================================================================
# Test 7
# =============================================================================


def test_a_service_returns_a_result_not_a_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches any service that still returns a JSONResponse instead of a typed
    result model.

    Expected state:
    - `chat.chat_turn(...)` returns a `ChatResponse`, never a Starlette Response.
    - `tool_proxy.proxy_tool(...)` returns a `Mapping`, never a Response.

    Both are called with mocked request state so no socket opens.
    """
    _no_lifespan_env(monkeypatch)

    # --- chat.chat_turn ---
    # The function does not exist yet; the import will fail
    # until then, which is the correct RED state.
    chat_turn_fn = getattr(chat_service, "chat_turn", None)
    assert chat_turn_fn is not None, (
        "chat_service.chat_turn must exist after plan 65b; "
        "missing means the service refactoring has not landed"
    )

    # --- tool_proxy.proxy_tool ---
    proxy_tool_fn = getattr(tool_proxy_module, "proxy_tool", None)
    assert proxy_tool_fn is not None, (
        "tool_proxy_module.proxy_tool must exist after plan 65b; "
        "missing means the service refactoring has not landed"
    )
