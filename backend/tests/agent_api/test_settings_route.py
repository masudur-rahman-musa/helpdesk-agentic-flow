"""POST /admin/settings, over HTTP.

Surface under test (written before it exists):

``agent_api.api.routes.admin.settings``
    ``router`` with ``POST /admin/settings``, body ``{"show_tool_steps": <bool>}``,
    calling the tool ``set_tool_steps_visible`` with the caller's own token.

``agent_api.schemas.settings``
    ``SetToolStepsVisibleBody``: ``extra="forbid"`` and one ``StrictBool`` field.
    ``SetToolStepsVisibleArgs``: ``show_tool_steps`` and ``idempotency_key``, with no
    tenant or user id.

A body that does not fit is the typed ``400 VALIDATION`` and never FastAPI's
untyped 422, and it opens no tool session.

Cost: no backing service. The lifespan is never entered and
``tool_proxy.build_tool_caller`` is replaced by a stub, as in the kill switch
route tests.
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
from helpdesk.agent_api.services import tool_proxy
from helpdesk.agent_api.services.mcp_client import ToolCaller
from helpdesk.shared.auth.jwt import issue_token
from helpdesk.shared.core.config import Settings
from helpdesk.shared.core.errors import ErrorCode
from helpdesk.shared.models.idempotency import KEY_LENGTH

SET_TOOL_STEPS_VISIBLE = "set_tool_steps_visible"
SETTINGS_PATH = "/admin/settings"

# Obviously fake, tests only; nothing here is dialled.
TEST_JWT_SIGNING_PHRASE = "settings-route-test-jwt-secret-0123456789"
MCP_URL = "http://mcp.invalid:8081/mcp"
DATABASE_URL = "postgresql+asyncpg://fake:fake@db.invalid:5432/fake_settings"
REDIS_URL = "redis://redis.invalid:6379/0"
QDRANT_URL = "http://qdrant.invalid:6333"

TENANT_ID = uuid.UUID("11111111-1111-5111-8111-111111111111")
ADMIN_ID = uuid.UUID("22222222-2222-5222-8222-222222222222")
DEPARTMENT_ID = uuid.UUID("33333333-3333-5333-8333-333333333333")
SESSION_ID = uuid.UUID("44444444-4444-5444-8444-444444444444")


class ScriptedToolCaller:
    """A `ToolCaller` answering one scripted payload per call, in order.

    A call past the last scripted answer fails the test outright, so "the route
    opened no session at all" is a hard stop rather than a count read later.
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
            raise AssertionError(f"the route made {len(self.calls)} tool calls; not scripted")
        return self._answers[index]


class CallerSpy:
    """Records the bearer `build_tool_caller` was handed, and hands back a stub."""

    def __init__(self, caller: ToolCaller) -> None:
        self._caller = caller
        self.bearer_tokens: list[str] = []

    def build(self, settings: Settings, bearer_token: str) -> ToolCaller:
        self.bearer_tokens.append(bearer_token)
        return self._caller


@pytest.fixture
def settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setenv("QDRANT_URL", QDRANT_URL)
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SIGNING_PHRASE)
    monkeypatch.setenv("MCP_URL", MCP_URL)


def _token() -> str:
    return issue_token(
        {
            "sub": str(ADMIN_ID),
            "tenant_id": str(TENANT_ID),
            "department_id": str(DEPARTMENT_ID),
            "principals": ["platform_admin"],
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


def _post(
    *,
    monkeypatch: pytest.MonkeyPatch,
    caller: ToolCaller,
    token: str,
    body: Mapping[str, Any],
) -> tuple[httpx.Response, CallerSpy]:
    spy = CallerSpy(caller)
    monkeypatch.setattr(tool_proxy, "build_tool_caller", spy.build)

    async def scenario(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post(
            SETTINGS_PATH, headers={"Authorization": f"Bearer {token}"}, json=dict(body)
        )

    return _drive(scenario), spy


@pytest.mark.parametrize("value", [True, False], ids=["turn-on", "turn-off"])
def test_post_settings_forwards_to_the_tool_with_the_token(
    settings_env: None, monkeypatch: pytest.MonkeyPatch, value: bool
) -> None:
    """Catches a route that calls a different tool, negates the flag, drops it
    for a constant, or sends no `idempotency_key` (the tool demands one, so every
    click would answer `VALIDATION`).

    Both values are sent, so a route hardwired to one direction fails a row. The
    argument set is the exact dict: a tenant or user id riding along fails it, and
    the token is forwarded verbatim so RBAC applies to the caller's own identity.
    """
    caller = ScriptedToolCaller({"show_tool_steps": value})
    token = _token()

    response, spy = _post(
        monkeypatch=monkeypatch, caller=caller, token=token, body={"show_tool_steps": value}
    )

    assert response.status_code == 200
    assert response.json() == {"show_tool_steps": value}
    assert [name for name, _ in caller.calls] == [SET_TOOL_STEPS_VISIBLE]
    arguments = dict(caller.calls[0][1])
    key = arguments.pop("idempotency_key")
    assert isinstance(key, str)
    assert 1 <= len(key) <= KEY_LENGTH
    assert arguments == {"show_tool_steps": value}
    assert spy.bearer_tokens == [token]


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"show_tool_steps": "yes"}, id="yes-string"),
        pytest.param({"show_tool_steps": "true"}, id="true-string"),
        pytest.param({"show_tool_steps": 1}, id="one"),
        pytest.param({"show_tool_steps": None}, id="null"),
        pytest.param({}, id="missing-field"),
        pytest.param({"show_tool_steps": True, "tenant_id": str(TENANT_ID)}, id="extra-tenant"),
        pytest.param(
            {"show_tool_steps": True, "idempotency_key": "fake-browser-key"}, id="extra-key"
        ),
    ],
)
def test_a_body_that_is_not_a_bool_is_a_typed_400(
    settings_env: None, monkeypatch: pytest.MonkeyPatch, body: Mapping[str, Any]
) -> None:
    """Catches a body model that is not strict (`"yes"`, `"true"` and `1` would
    coerce to true and answer 200), one that lets FastAPI answer its untyped 422,
    and one with `extra="ignore"` (a browser-chosen key or a tenant id would be
    swallowed and answered 200).

    The bearer verifies and the caller is scripted with NOTHING, so a route that
    entered a session or called the tool fails outright.
    """
    caller = ScriptedToolCaller()

    response, spy = _post(monkeypatch=monkeypatch, caller=caller, token=_token(), body=body)

    assert response.status_code == 400
    assert response.json() == {"code": ErrorCode.VALIDATION.value}
    assert caller.entered == 0
    assert caller.calls == []
    assert spy.bearer_tokens == []
