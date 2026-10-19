"""`GET /admin/tickets`, the open tickets board over HTTP.

Surface under test (written before it exists; every case below is red on one of
these names until the route lands):

``agent_api.schemas.tickets``
    ``ListOpenTicketsArgs`` with no field, and ``ListOpenTicketsArgsWithLimit``
    with one ``limit: int``. The limit lives on a subclass, not a nullable
    field, because ``model_dump`` would send ``limit: null`` and the tool
    refuses it (the same shape as ``schemas.me``).

``agent_api.api.routes.admin.tickets``
    ``router`` with ``GET /admin/tickets`` (optional ``limit``), calling the
    ``list_open_tickets`` MCP tool through ``agent_api.services.tool_proxy``'s
    seam, so every case here patches ``tool_proxy.build_tool_caller``, enters no
    lifespan and opens no socket. Registered in ``api.router``.

Shapes this file fixes that the plan left open:
- the refusal envelope is ``{"code": "<ErrorCode value>"}``; a missing bearer is
  a literal 401 carrying ``{"code": "FORBIDDEN"}`` (what the shared middleware
  answers), a refusal from the tool is a 403.
- the arguments are exactly empty when no limit is asked for, and exactly
  ``{"limit": N}`` when one is. Nothing naming a tenant or a user ever rides
  along.

Cost and isolation: NO backing service at all, exactly as
``test_rules_routes.py`` and ``test_me_routes.py``. Fixtures live in this module
rather than a conftest, for the same reason they do there.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from helpdesk.agent_api.api.router import app
from helpdesk.agent_api.schemas.tickets import ListOpenTicketsArgs, ListOpenTicketsArgsWithLimit
from helpdesk.agent_api.services import tool_proxy as deps
from helpdesk.agent_api.services.mcp_client import (
    RemoteForbiddenError,
    RemoteToolError,
    ToolCaller,
)
from helpdesk.mcp_server.tools.tickets import ListOpenTicketsOutput
from helpdesk.shared.auth.jwt import issue_token
from helpdesk.shared.core.config import Settings
from helpdesk.shared.core.errors import ErrorCode

TICKETS_PATH = "/admin/tickets"
LIST_OPEN_TICKETS = "list_open_tickets"

# Obviously fake, tests only: none of these is a credential, and nothing in this
# file connects to any of these hosts.
TEST_JWT_SIGNING_PHRASE = "admin-tickets-test-jwt-secret-0123456789ab"
MCP_URL = "http://mcp.invalid:8081/mcp"
DATABASE_URL = "postgresql+asyncpg://fake:fake@db.invalid:5432/fake_admin_tickets"
REDIS_URL = "redis://redis.invalid:6379/0"
QDRANT_URL = "http://qdrant.invalid:6333"

TENANT_ID = uuid.UUID("11111111-1111-5111-8111-111111111111")
ADMIN_ID = uuid.UUID("22222222-2222-5222-8222-222222222222")
DEPARTMENT_ID = uuid.UUID("33333333-3333-5333-8333-333333333333")
SESSION_ID = uuid.UUID("44444444-4444-5444-8444-444444444444")

TICKETS_PAYLOAD: Mapping[str, Any] = {
    "tickets": [
        {
            "ticket_id": "88888888-8888-5888-8888-888888888888",
            "ticket_number": "TCK-0042",
            "kind": "general",
            "title": "Obviously fake ticket title.",
            "opened_by": "Fake Person One",
            "created_at": "2026-06-02T10:15:00Z",
        },
        {
            "ticket_id": "99999999-9999-5999-8999-999999999999",
            "ticket_number": "TCK-0043",
            "kind": "password_reset",
            "title": "Another obviously fake title.",
            "opened_by": "Fake Person Two",
            "created_at": "2026-06-03T08:00:00Z",
        },
    ]
}
"""Two rows exactly as the tool answers them, oldest first. The route must hand
this back UNCHANGED, so the equality case catches a router that reshapes,
re-sorts, filters or renames on the way out."""


# --- the scripted seam --------------------------------------------------------


class ScriptedToolCaller:
    """A `ToolCaller` answering one scripted payload per call, in order.

    A call past the last scripted answer fails the test outright, which is how
    "the route opened no session at all" is asserted as a hard stop.
    """

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
                f"the route made {len(self.calls)} tool calls; only {len(self._answers)} "
                "were scripted"
            )
        return self._answers[index]


class RefusingToolCaller:
    """A caller whose session opens and whose tool call raises a typed error."""

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
    """Records the bearer `build_tool_caller` was handed, and hands back a stub."""

    def __init__(self, caller: ToolCaller) -> None:
        self._caller = caller
        self.bearer_tokens: list[str] = []

    def build(self, settings: Settings, bearer_token: str) -> ToolCaller:
        self.bearer_tokens.append(bearer_token)
        return self._caller


@pytest.fixture
def tickets_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Obviously fake settings. Nothing here is dialled: no lifespan is entered."""
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setenv("QDRANT_URL", QDRANT_URL)
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SIGNING_PHRASE)
    monkeypatch.setenv("MCP_URL", MCP_URL)


def _token() -> str:
    """One verified-looking token for one fake it_admin and one session."""
    return issue_token(
        {
            "sub": str(ADMIN_ID),
            "tenant_id": str(TENANT_ID),
            "department_id": str(DEPARTMENT_ID),
            "principals": ["it_admin"],
            "sid": str(SESSION_ID),
        },
        TEST_JWT_SIGNING_PHRASE,
        datetime.now(UTC),
    )


def _drive(scenario: Callable[[httpx.AsyncClient], Awaitable[httpx.Response]]) -> httpx.Response:
    async def runner() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await scenario(client)

    return asyncio.run(runner())


def _get(
    *,
    monkeypatch: pytest.MonkeyPatch,
    caller: ToolCaller,
    path: str = TICKETS_PATH,
    headers: Mapping[str, str] | None = None,
) -> tuple[httpx.Response, CallerSpy]:
    """One read against the app, with the tool caller replaced at the seam."""
    spy = CallerSpy(caller)
    monkeypatch.setattr(deps, "build_tool_caller", spy.build)

    async def scenario(client: httpx.AsyncClient) -> httpx.Response:
        return await client.get(path, headers=dict(headers or {}))

    return _drive(scenario), spy


# --- the staged payload is the tool's own shape --------------------------------


def test_the_staged_payload_is_exactly_what_the_tool_model_produces() -> None:
    """Catches a fixture invented here instead of read off the tool's model,
    which would let the equality case pass while the route answered a shape the
    web's parser refuses. An extra, missing or mistyped key does not round-trip."""
    assert ListOpenTicketsOutput.model_validate(TICKETS_PAYLOAD).model_dump(mode="json") == dict(
        TICKETS_PAYLOAD
    )


# --- the arguments the route sends ---------------------------------------------


def test_the_argument_models_are_fieldless_and_limit_only() -> None:
    """Catches an arguments model that declares a tenant or user field (the
    browser asking to read another company's tickets, which only the token may
    decide), and a limit placed on the base model as a nullable field, which
    `model_dump` would send as `limit: null` for the tool to refuse."""
    assert ListOpenTicketsArgs.model_fields == {}
    assert set(ListOpenTicketsArgsWithLimit.model_fields) == {"limit"}
    assert ListOpenTicketsArgsWithLimit(limit=5).model_dump() == {"limit": 5}


def test_the_route_is_registered_as_a_get_on_the_admin_tickets_path() -> None:
    """Catches a router written and never included: every other case would
    then fail as a 404 with no hint which of the two is missing. Reads
    `app.openapi()` because routers are included through a wrapper that keeps
    the concrete route off `app.routes`."""
    paths = app.openapi()["paths"]

    assert TICKETS_PATH in paths
    assert "get" in paths[TICKETS_PATH]


# --- the happy paths ------------------------------------------------------------


def test_the_route_answers_the_tools_output_unchanged(
    tickets_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a route that reshapes, re-sorts or renames the tool's output,
    calls a different tool, invents a limit when the caller sent none (the
    tool's own default of 50 must stay the only place for that number), or sends
    an argument naming a tenant or user. The token is forwarded verbatim, so the
    tool's own RBAC and tenant guard apply."""
    caller = ScriptedToolCaller(TICKETS_PAYLOAD)
    token = _token()

    response, spy = _get(
        monkeypatch=monkeypatch,
        caller=caller,
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == dict(TICKETS_PAYLOAD)
    assert caller.calls == [(LIST_OPEN_TICKETS, {})]
    assert spy.bearer_tokens == [token]


def test_the_route_passes_the_callers_limit(
    tickets_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a route that drops the query parameter (the page size the caller
    asked for is lost) or sends it as text instead of the number 5."""
    caller = ScriptedToolCaller(TICKETS_PAYLOAD)

    response, _spy = _get(
        monkeypatch=monkeypatch,
        caller=caller,
        path=f"{TICKETS_PATH}?limit=5",
        headers={"Authorization": f"Bearer {_token()}"},
    )

    assert response.status_code == 200
    assert caller.calls == [(LIST_OPEN_TICKETS, {"limit": 5})]


# --- the refusals ----------------------------------------------------------------


def test_the_route_without_a_bearer_is_a_typed_401(
    tickets_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a tickets route outside the token check: the caller is scripted
    with nothing, so a route that opened a session or called a tool fails
    outright, and the refusal must be the typed 401 body, not free text."""
    caller = ScriptedToolCaller()

    response, spy = _get(monkeypatch=monkeypatch, caller=caller, headers={})

    assert response.status_code == 401
    assert response.json() == {"code": ErrorCode.FORBIDDEN.value}
    assert caller.entered == 0
    assert caller.calls == []
    assert spy.bearer_tokens == []


def test_a_tool_refusal_answers_its_own_typed_403(
    tickets_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a route that lets the tool's refusal escape as an untyped 500,
    so an employee told "not allowed" would read an outage. The bearer
    verifies, so the token check cannot decide this row; only the tool-error
    branch can."""
    caller = RefusingToolCaller(RemoteForbiddenError(ErrorCode.FORBIDDEN.value))

    response, _spy = _get(
        monkeypatch=monkeypatch,
        caller=caller,
        headers={"Authorization": f"Bearer {_token()}"},
    )

    assert response.status_code == 403
    assert response.json() == {"code": ErrorCode.FORBIDDEN.value}
    assert caller.entered == 1
