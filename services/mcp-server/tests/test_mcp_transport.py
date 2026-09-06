"""An earlier change, section 1 criterion 1 and section 6: the MCP transport itself.

Surface under test (written before it exists):

``mcp_server.main``
    ``create_app() -> FastAPI`` — a FRESH lowlevel MCP server and Starlette
    app per call. Called per test on purpose: the SDK's
    ``StreamableHTTPSessionManager`` refuses a second ``run()`` on one
    instance and each test drives its own ``asyncio.run`` loop. The backing
    services it talks to stay session-scoped.
    ``app`` — a module-level application for uvicorn.
    ``run_stdio() -> None`` — the stdio entrypoint (the design, local debugging).
    Routes: ``GET /healthz`` -> ``{"status": "ok"}``;
    ``GET /readyz`` -> ``{"status": "ready"|"not_ready",
    "checks": {"postgres": bool, "redis": bool}}`` with 200/503; the MCP
    streamable HTTP transport at ``POST /mcp``.

``mcp_server.readiness``
    ``check_postgres()`` / ``check_redis()``, mirroring ``agent_api.readiness``
    with no qdrant. ``/readyz`` calls them through the module attribute so a
    test can fake one check.

``mcp_server.registry.REGISTRY``
    The wire-name -> ``ToolSpec`` mapping ``tools/list`` is built from.

No pytest-asyncio in this project: every case is a sync function driving one
``asyncio.run`` scenario.
"""

import asyncio
import os
import sys
from typing import Any

import mcp.types as mcp_types
import pytest
from mcp import Client, StdioServerParameters, stdio_client
from mcp_fixtures import (  # noqa: F401 - fixtures are resolved by name
    SeededMcp,
    bearer,
    http_get,
    list_tools,
    mcp_env,
    mcp_pg,
    mcp_redis_base_url,
    mcp_redis_url,
    seeded_mcp_db,
    token_for,
)
from mcp_server import readiness
from mcp_server.registry import REGISTRY

# An earlier change registered the two read tools; an earlier change adds the three helpdesk
# mutating tools; an earlier change adds the two access read tools. Pinned as a
# literal, not derived from REGISTRY, so adding a tool without deciding it
# belongs on the wire fails.
EXPECTED_TOOL_NAMES = {
    "get_me",
    "list_my_access",
    "reset_own_password",
    "unlock_own_account",
    "create_ticket",
    "classify_access_request",
    "list_my_requests",
    "create_access_request",
    # An earlier change: the approval queue and the two deciding tools.
    "list_pending_approvals",
    "approve_request",
    "reject_request",
    # An earlier change: the one tool that takes access away.
    "revoke_access",
}

DEPENDENCIES: tuple[str, str] = ("postgres", "redis")

# The stdio entrypoint, driven as a real subprocess.
_STDIO_CODE = "from mcp_server.main import run_stdio; run_stdio()"


def test_registry_registers_exactly_the_expected_tools() -> None:
    assert set(REGISTRY) == EXPECTED_TOOL_NAMES


def test_tools_list_over_the_transport_returns_exactly_the_registry_names(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Driven through a real MCP client session, not by asserting on a route."""
    result = list_tools(bearer(token_for(mcp_env.tenant_a)))

    assert {tool.name for tool in result.tools} == set(REGISTRY)
    assert {tool.name for tool in result.tools} == EXPECTED_TOOL_NAMES


def test_each_advertised_schema_is_its_registry_models_schema(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """A schema drifting from the model it validates is a lie to every client."""
    result = list_tools(bearer(token_for(mcp_env.tenant_a)))
    advertised = {tool.name: tool for tool in result.tools}

    for name, spec in REGISTRY.items():
        tool = advertised[name]
        assert tool.input_schema == spec.input_model.model_json_schema(), name
        assert tool.output_schema == spec.output_model.model_json_schema(), name
        assert tool.description == spec.description, name


def test_stdio_entrypoint_starts_and_lists_the_same_tools(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """the design's local-debugging entrypoint, driven as a real subprocess."""
    environment = dict(os.environ)
    environment.update(
        {
            "DATABASE_URL": mcp_env.database_url,
            "PYTHONUNBUFFERED": "1",
        }
    )
    params = StdioServerParameters(
        command=sys.executable, args=["-c", _STDIO_CODE], env=environment
    )

    async def scenario() -> mcp_types.ListToolsResult:
        async with Client(stdio_client(params)) as client:
            return await client.list_tools()

    result = asyncio.run(scenario())

    assert {tool.name for tool in result.tools} == set(REGISTRY)


# --- /healthz and /readyz ------------------------------------------------------


def _make_check(result: bool) -> Any:
    async def _check() -> bool:
        return result

    return _check


def _fake_checks(monkeypatch: pytest.MonkeyPatch, results: dict[str, bool]) -> None:
    for dependency, healthy in results.items():
        monkeypatch.setattr(readiness, f"check_{dependency}", _make_check(healthy))


def test_healthz_answers_without_touching_any_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_checks(monkeypatch, dict.fromkeys(DEPENDENCIES, False))

    response = http_get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_reports_ready_when_postgres_and_redis_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_checks(monkeypatch, dict.fromkeys(DEPENDENCIES, True))

    response = http_get("/readyz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"postgres": True, "redis": True},
    }


@pytest.mark.parametrize("failing", DEPENDENCIES)
def test_readyz_returns_503_naming_the_failing_dependency(
    monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    _fake_checks(monkeypatch, {dep: dep != failing for dep in DEPENDENCIES})

    response = http_get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"][failing] is False
    for dependency in DEPENDENCIES:
        if dependency != failing:
            assert body["checks"][dependency] is True


def test_readyz_against_the_live_compose_stack_is_ready(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The real check bodies, not the faked seam: a suite that only ever fakes
    readiness proves the route and nothing about the checks."""
    response = http_get("/readyz")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "status": "ready",
        "checks": {"postgres": True, "redis": True},
    }
