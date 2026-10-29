"""`GET /policies`, the company's access rules for every signed-in person, over HTTP.

The route calls the `read_policies` tool with the caller's own token, so the
tool's access check, the kill switch and audit apply. The tool caller is
replaced at `tool_proxy.build_tool_caller`, the lifespan is never entered, and
nothing opens a socket.
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
from helpdesk.agent_api.services.mcp_client import RemoteNotFoundError, ToolCaller
from helpdesk.mcp_server.tools.policy_docs import ReadPoliciesOutput
from helpdesk.shared.auth.jwt import issue_token
from helpdesk.shared.core.config import Settings

READ_POLICIES = "read_policies"

# Obviously fake, tests only: nothing here is a credential or is dialled.
TEST_JWT_SIGNING_PHRASE = "policies-read-route-test-jwt-secret-0123456789"

PAYLOAD: Mapping[str, Any] = {
    "documents": [{"slug": "fake-access", "title": "Fake Access", "version": "v1"}],
    "document": {
        "slug": "fake-access",
        "title": "Fake Access",
        "version": "v1",
        "sections": [{"heading_path": ["Fake Access", "1. Purpose"], "text": "Fake text."}],
    },
}


class ScriptedCaller:
    """Answers one payload, or raises one error, and records each call."""

    def __init__(self, answer: Mapping[str, Any] | Exception) -> None:
        self._answer = answer
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> ToolCaller:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def call_tool(self, name: str, arguments: BaseModel) -> Mapping[str, Any]:
        self.calls.append((name, arguments.model_dump(mode="json")))
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer


@pytest.fixture
def route_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://fake:fake@db.invalid:5432/fake")
    monkeypatch.setenv("REDIS_URL", "redis://redis.invalid:6379/0")
    monkeypatch.setenv("QDRANT_URL", "http://qdrant.invalid:6333")
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SIGNING_PHRASE)
    monkeypatch.setenv("MCP_URL", "http://mcp.invalid:8081/mcp")


def _token() -> str:
    return issue_token(
        {
            "sub": str(uuid.uuid4()),
            "tenant_id": str(uuid.uuid4()),
            "department_id": str(uuid.uuid4()),
            "principals": ["employee"],
            "sid": str(uuid.uuid4()),
        },
        TEST_JWT_SIGNING_PHRASE,
        datetime.now(UTC),
    )


def _get(
    monkeypatch: pytest.MonkeyPatch, caller: ScriptedCaller, path: str, token: str
) -> tuple[httpx.Response, list[str]]:
    bearers: list[str] = []

    def build(settings: Settings, bearer_token: str) -> ToolCaller:
        bearers.append(bearer_token)
        return caller

    monkeypatch.setattr(tool_proxy, "build_tool_caller", build)

    async def run(scenario: Callable[[httpx.AsyncClient], Awaitable[httpx.Response]]) -> Any:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await scenario(client)

    response = asyncio.run(
        run(lambda client: client.get(path, headers={"Authorization": f"Bearer {token}"}))
    )
    return response, bearers


def test_the_staged_payload_is_the_tools_own_shape() -> None:
    """Catches a payload invented here that the tool could never answer."""
    assert ReadPoliciesOutput.model_validate(PAYLOAD).model_dump(mode="json") == dict(PAYLOAD)


def test_the_route_proxies_the_tool_with_the_callers_token_and_passes_the_slug(
    route_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a route that calls another tool, mints its own token, drops the
    slug or reshapes the answer on the way out."""
    caller = ScriptedCaller(PAYLOAD)
    token = _token()

    response, bearers = _get(monkeypatch, caller, "/policies?slug=fake-access", token)

    assert response.status_code == 200
    assert response.json() == dict(PAYLOAD)
    assert caller.calls == [(READ_POLICIES, {"slug": "fake-access"})]
    assert bearers == [token]


def test_no_slug_asks_for_the_list_alone(route_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Catches a route that sends an empty string, which the tool refuses, when
    the page asks for the list only."""
    caller = ScriptedCaller({"documents": [], "document": None})

    response, _bearers = _get(monkeypatch, caller, "/policies", _token())

    assert response.status_code == 200
    assert caller.calls == [(READ_POLICIES, {"slug": None})]


def test_a_refusal_crosses_as_its_typed_code(
    route_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a route that turns the tool's `NOT_FOUND` into a 500 or an empty page."""
    caller = ScriptedCaller(RemoteNotFoundError("no such document"))

    response, _bearers = _get(monkeypatch, caller, "/policies?slug=fake-missing", _token())

    assert response.status_code == 404
    assert response.json() == {"code": "NOT_FOUND"}


def test_no_token_is_refused_before_any_tool_call(
    route_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a route left off the sign-in check."""
    caller = ScriptedCaller(PAYLOAD)

    response, bearers = _get(monkeypatch, caller, "/policies", "")

    assert response.status_code == 401
    assert caller.calls == []
    assert bearers == []
