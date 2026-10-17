"""Health checks are written once.

Seven tests, all plain objects and fakes. No running service, no `db` marker.

Surface under test: ``helpdesk.shared.core.health``
    ``HealthResponse`` — Pydantic model, status field
    ``ReadinessResponse`` — Pydantic model, named dependency flags
    ``ping_postgres(engine)`` — returns bool, catches all exceptions
    ``ping_redis(client)`` — returns bool, catches all exceptions
    ``ping_qdrant(client)`` — returns bool, catches all exceptions
    ``ping_gateway(client, base_url)`` — returns bool for 2xx, catches all exceptions
    ``register_health(app, build_checks)`` — mounts /healthz, /readyz, /metrics

The tests are RED until ``backend/helpdesk/shared/core/health.py`` exists.
"""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI

from helpdesk.shared.core.health import (
    HealthResponse,
    ReadinessResponse,
    ping_gateway,
    ping_postgres,
    ping_qdrant,
    ping_redis,
    register_health,
)

# ---------------------------------------------------------------------------
# Fake clients
# ---------------------------------------------------------------------------


class _RaisingEngine:
    """A fake SQLAlchemy async engine whose connect() raises unconditionally.

    Used to prove /healthz ignores raised exceptions from all checks.
    """

    @asynccontextmanager
    async def connect(self) -> Any:
        raise RuntimeError("fake engine: always raises")
        yield  # pragma: no cover — unreachable, satisfies the generator protocol


class _RaisingRedis:
    """A fake Redis client whose ping() raises unconditionally."""

    async def ping(self) -> bool:
        raise RuntimeError("fake redis: always raises")


class _RaisingQdrant:
    """A fake Qdrant client whose get_collections() raises unconditionally."""

    async def get_collections(self) -> Any:
        raise RuntimeError("fake qdrant: always raises")


class _RaisingHttpx:
    """A fake httpx async client whose get() raises unconditionally."""

    async def get(self, url: str) -> Any:
        raise RuntimeError("fake httpx: always raises")


class _OkEngine:
    """A fake SQLAlchemy async engine whose connect() succeeds."""

    def __init__(self) -> None:
        self.called = False

    @asynccontextmanager
    async def connect(self) -> Any:
        self.called = True
        yield _OkConn()


class _OkConn:
    async def execute(self, stmt: Any) -> Any:
        return None


class _OkRedis:
    """A fake Redis client whose ping() returns True."""

    def __init__(self) -> None:
        self.called = False

    async def ping(self) -> bool:
        self.called = True
        return True


class _OkQdrant:
    """A fake Qdrant client whose get_collections() returns a dummy object."""

    def __init__(self) -> None:
        self.called = False

    async def get_collections(self) -> Any:
        self.called = True
        return object()


class _OkHttpx:
    """A fake httpx async client whose get() returns a 200 response."""

    def __init__(self) -> None:
        self.called = False

    async def get(self, url: str) -> Any:
        self.called = True
        return _FakeResponse(200)


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


# ---------------------------------------------------------------------------
# App builder helpers
# ---------------------------------------------------------------------------

CheckDict = dict[str, Callable[[], Awaitable[bool]]]


def _make_app(build_checks: Callable[[Any], CheckDict]) -> FastAPI:
    """Return a fresh FastAPI app with register_health applied."""
    app = FastAPI()
    register_health(app, build_checks)
    return app


def _get(app: FastAPI, path: str) -> httpx.Response:
    """Drive the ASGI app synchronously, without entering lifespan."""

    async def _run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# Test 1
# ---------------------------------------------------------------------------


def test_healthz_is_ok_and_touches_no_dependency() -> None:
    """Catches an implementation that calls dependency checks inside /healthz.

    If /healthz runs any ping function the raising fakes will propagate an
    exception that becomes a 500. The only way to get 200 {"status":"ok"}
    is to never call them.

    Also catches an implementation that does not use HealthResponse as its model:
    HealthResponse.model_validate must parse the body without error.
    """

    async def _raising() -> bool:
        raise RuntimeError("dependency probe reached from /healthz")

    def build_checks(state: Any) -> CheckDict:
        return {"dep": _raising}

    app = _make_app(build_checks)

    response = _get(app, "/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body == {"status": "ok"}
    # Confirm the body is valid for HealthResponse (pins the model's existence).
    HealthResponse.model_validate(body)


# ---------------------------------------------------------------------------
# Test 2
# ---------------------------------------------------------------------------


def test_readyz_is_200_and_names_every_check_when_all_pass() -> None:
    """Catches an implementation that omits a dependency name from the response
    body, or returns a different status code when every check passes.

    The body must include each key the build_checks dict names, all True.
    Also confirms ReadinessResponse can parse the body (pins the model's shape).
    """

    async def _pass() -> bool:
        return True

    def build_checks(state: Any) -> CheckDict:
        return {"postgres": _pass, "redis": _pass, "qdrant": _pass}

    app = _make_app(build_checks)

    response = _get(app, "/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["checks"]["postgres"] is True
    assert body["checks"]["redis"] is True
    assert body["checks"]["qdrant"] is True
    # Confirm the body parses as ReadinessResponse (pins the model's existence).
    ReadinessResponse.model_validate(body)


# ---------------------------------------------------------------------------
# Test 3
# ---------------------------------------------------------------------------


def test_readyz_is_503_naming_the_one_failing_check() -> None:
    """Catches an implementation that returns 200 even when a check fails, or
    that hides which dependency failed.

    The response must be 503 and the failing name must be False while all
    other names remain True.
    """

    async def _pass() -> bool:
        return True

    async def _fail() -> bool:
        return False

    def build_checks(state: Any) -> CheckDict:
        return {"postgres": _pass, "redis": _fail, "qdrant": _pass}

    app = _make_app(build_checks)

    response = _get(app, "/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["checks"]["redis"] is False
    assert body["checks"]["postgres"] is True
    assert body["checks"]["qdrant"] is True


# ---------------------------------------------------------------------------
# Test 4
# ---------------------------------------------------------------------------


def test_each_check_reuses_the_passed_client_and_constructs_none() -> None:
    """Catches an implementation of ping_postgres/redis/qdrant/ping_gateway that
    builds its own internal client instead of using the one passed in.

    The fake clients record whether they were called. If any ping function builds
    its own client it will get a different object, leave the passed-in one
    uncalled, and this test fails.
    """
    engine = _OkEngine()
    redis_client = _OkRedis()
    qdrant_client = _OkQdrant()
    http_client = _OkHttpx()

    # Use an obviously-unreachable base_url so that any internal client
    # construction that tries to connect would also fail immediately.
    base_url = "http://gateway.test:8080/v1"

    async def _run() -> None:
        await ping_postgres(engine)
        await ping_redis(redis_client)
        await ping_qdrant(qdrant_client)
        await ping_gateway(http_client, base_url)

    asyncio.run(_run())

    assert engine.called, "ping_postgres must call engine.connect()"
    assert redis_client.called, "ping_redis must call client.ping()"
    assert qdrant_client.called, "ping_qdrant must call client.get_collections()"
    assert http_client.called, "ping_gateway must call client.get(...)"


# ---------------------------------------------------------------------------
# Test 5
# ---------------------------------------------------------------------------


def test_readyz_runs_its_checks_at_the_same_time() -> None:
    """Catches an implementation that awaits each check one at a time instead of
    running them via asyncio.gather (or equivalent concurrent execution).

    Two checks each block until the other has started. Sequential execution
    means the first blocks for ever; concurrent execution means both enter and
    then both can release.
    """
    entered = asyncio.Event()
    released = asyncio.Event()

    async def _blocker_a() -> bool:
        entered.set()
        await released.wait()
        return True

    async def _blocker_b() -> bool:
        # Wait until blocker_a has entered, then release it.
        await entered.wait()
        released.set()
        return True

    def build_checks(state: Any) -> CheckDict:
        return {"a": _blocker_a, "b": _blocker_b}

    # If checks run sequentially, _blocker_a waits for `released` which is only
    # set by _blocker_b, which never runs. asyncio.run would time out.
    app = _make_app(build_checks)

    async def _run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await asyncio.wait_for(client.get("/readyz"), timeout=5.0)

    response = asyncio.run(_run())

    assert response.status_code == 200
    body = response.json()
    assert body["checks"]["a"] is True
    assert body["checks"]["b"] is True


# ---------------------------------------------------------------------------
# Test 6
# ---------------------------------------------------------------------------


def test_metrics_renders_through_the_shared_registrar() -> None:
    """Catches an implementation where /metrics is not mounted by register_health,
    or where the response body is not in prometheus text format.

    Status must be 200 and content-type must start with text/plain.
    """

    async def _pass() -> bool:
        return True

    def build_checks(state: Any) -> CheckDict:
        return {"postgres": _pass}

    app = _make_app(build_checks)

    response = _get(app, "/metrics")

    assert response.status_code == 200
    content_type = response.headers.get("content-type", "")
    assert content_type.startswith("text/plain"), (
        f"/metrics content-type must start with text/plain, got {content_type!r}"
    )


# ---------------------------------------------------------------------------
# Test 7
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "ping_fn", "bad_client"),
    [
        ("ping_postgres", ping_postgres, _RaisingEngine()),
        ("ping_redis", ping_redis, _RaisingRedis()),
        ("ping_qdrant", ping_qdrant, _RaisingQdrant()),
    ],
    ids=["postgres", "redis", "qdrant"],
)
def test_a_check_returns_false_rather_than_raising_when_its_client_errors(
    name: str,
    ping_fn: Any,
    bad_client: Any,
) -> None:
    """Catches an implementation that lets a client exception propagate out of a
    ping function instead of catching it and returning False.

    A raised exception in a check would turn a /readyz call into a 500 instead
    of a 503 with the dependency marked False.
    """

    async def _run() -> bool:
        return cast(bool, await ping_fn(bad_client))

    result = asyncio.run(_run())

    assert result is False, f"{name} must return False when its client raises, not propagate"


def test_ping_gateway_returns_false_rather_than_raising_when_its_client_errors() -> None:
    """Catches an implementation of ping_gateway that lets a client exception
    propagate instead of catching it and returning False.

    Separate from the parametrized case above because ping_gateway takes two
    arguments (client and base_url).
    """
    base_url = "http://gateway.test:8080/v1"

    async def _run() -> bool:
        return cast(bool, await ping_gateway(_RaisingHttpx(), base_url))

    result = asyncio.run(_run())

    assert result is False, "ping_gateway must return False when its client raises, not propagate"
