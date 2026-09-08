"""Tests for agent-api health endpoints.

API under test:
    agent_api.main.app  -- FastAPI application
    agent_api.readiness -- module with the readiness seam:
        async def check_postgres() -> bool
        async def check_redis() -> bool
        async def check_qdrant() -> bool
        async def check_gateway() -> bool
    The /readyz route must call these via the module attribute
    (``await readiness.check_postgres()``) so monkeypatching the
    module attribute fakes the check.

Response shapes:
    GET /healthz -> 200, {"status": "ok"}          (no dependency checks)
    GET /readyz  -> 200, {"status": "ready",
                          "checks": {"postgres": true, "redis": true,
                                     "qdrant": true, "gateway": true}}
    GET /readyz  -> 503 when any check is False, body:
                         {"status": "not_ready",
                          "checks": {...}}  with the failing dependency mapped to false
"""

import asyncio
from typing import Any

import httpx
import pytest
from agent_api import readiness
from agent_api.main import app

# An earlier change section 8: FOUR dependencies. the design names the gateway as a
# dependency of the agent's readiness, so a container reporting ready while
# every model call fails is the failure this endpoint exists to prevent. The
# 503 case below is parametrised over this tuple, so `gateway: False` alone
# must fail readiness.
DEPENDENCIES: tuple[str, ...] = ("postgres", "redis", "qdrant", "gateway")


def _get(path: str) -> httpx.Response:
    async def _run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)

    return asyncio.run(_run())


def _make_check(result: bool) -> Any:
    async def _check() -> bool:
        return result

    return _check


def _fake_checks(monkeypatch: pytest.MonkeyPatch, results: dict[str, bool]) -> None:
    for dep, healthy in results.items():
        monkeypatch.setattr(readiness, f"check_{dep}", _make_check(healthy))


def test_healthz_returns_200_without_dependency_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # All dependencies down: /healthz must still be 200 (process up only).
    _fake_checks(monkeypatch, dict.fromkeys(DEPENDENCIES, False))

    response = _get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_returns_200_when_all_checks_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_checks(monkeypatch, dict.fromkeys(DEPENDENCIES, True))

    response = _get("/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"] == dict.fromkeys(DEPENDENCIES, True)


@pytest.mark.parametrize("failing", DEPENDENCIES)
def test_readyz_returns_503_naming_the_failing_dependency(
    monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    results = {dep: dep != failing for dep in DEPENDENCIES}
    _fake_checks(monkeypatch, results)

    response = _get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"][failing] is False
    # The healthy dependencies must not be blamed.
    for dep in DEPENDENCIES:
        if dep != failing:
            assert body["checks"][dep] is True
    # The failing dependency is named in the body.
    assert failing in response.text
