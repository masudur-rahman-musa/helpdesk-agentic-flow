"""Tests for the shared readiness ping functions, driven from agent-api context.

An earlier change added the gateway check and the two-timeout pin; an earlier change moved all
four checks into `shared.core.health` as `ping_*` functions that take the
app's existing client instead of building a new one per call. This file covers
the ping functions themselves: that they use the passed client (not a fresh
one), and that the gateway ping probes the origin of the versioned URL.

The per-call construction and timeout assertions from the old file drop here:
the app's long-lived clients now carry those settings, and a new engine or
Redis client is never built inside a ping function.

`test_health.py` (agent_api) and `tests/shared/core/test_health.py` cover the
/readyz route behaviour; this file covers only the ping bodies.
"""

import asyncio
from types import TracebackType
from typing import Any

import pytest

from helpdesk.shared.core.health import (
    GATEWAY_HEALTH_PATH,
    ping_gateway,
    ping_postgres,
    ping_qdrant,
    ping_redis,
)

# ---------------------------------------------------------------------------
# Minimal fake clients — each records whether it was called
# ---------------------------------------------------------------------------


class _FakeConnection:
    async def execute(self, statement: Any) -> None:
        return None


class _FakeConnectContext:
    async def __aenter__(self) -> _FakeConnection:
        return _FakeConnection()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


class _FakeEngine:
    def connect(self) -> _FakeConnectContext:
        return _FakeConnectContext()


class _FakeRedis:
    async def ping(self) -> bool:
        return True


class _FakeQdrantClient:
    async def get_collections(self) -> None:
        return None


class _FakeHttpxClient:
    def __init__(self, status_code: int = 204) -> None:
        self._status_code = status_code
        self.last_url: str | None = None

    async def get(self, url: str, **kwargs: Any) -> Any:
        self.last_url = url

        class _Resp:
            def __init__(self, code: int) -> None:
                self.status_code = code

        return _Resp(self._status_code)


# ---------------------------------------------------------------------------
# Happy paths — each ping uses the passed client
# ---------------------------------------------------------------------------


def test_ping_postgres_returns_true_when_engine_answers() -> None:
    assert asyncio.run(ping_postgres(_FakeEngine())) is True


def test_ping_redis_returns_true_when_client_answers() -> None:
    assert asyncio.run(ping_redis(_FakeRedis())) is True


def test_ping_qdrant_returns_true_when_client_answers() -> None:
    assert asyncio.run(ping_qdrant(_FakeQdrantClient())) is True


def test_ping_gateway_returns_true_for_a_2xx_response() -> None:
    client = _FakeHttpxClient(status_code=204)
    assert asyncio.run(ping_gateway(client, "http://gateway.invalid:8080/v1")) is True


# ---------------------------------------------------------------------------
# Unhappy paths — each ping catches errors and returns False
# ---------------------------------------------------------------------------


def test_ping_postgres_returns_false_when_engine_raises() -> None:
    class _BadEngine:
        def connect(self) -> Any:
            raise RuntimeError("engine gone")

    assert asyncio.run(ping_postgres(_BadEngine())) is False


def test_ping_redis_returns_false_when_client_raises() -> None:
    class _BadRedis:
        async def ping(self) -> bool:
            raise RuntimeError("redis gone")

    assert asyncio.run(ping_redis(_BadRedis())) is False


def test_ping_qdrant_returns_false_when_client_raises() -> None:
    class _BadQdrant:
        async def get_collections(self) -> None:
            raise RuntimeError("qdrant gone")

    assert asyncio.run(ping_qdrant(_BadQdrant())) is False


GATEWAY_ENVIRONMENTS = (
    # Port 9 (discard) is closed, so the client raises a connection error.
    ("unreachable", "http://127.0.0.1:9/v1"),
    # Not a real URL: _gateway_health_url raises ValueError, caught as False.
    ("misconfigured", "not-a-gateway-url"),
)


@pytest.mark.parametrize(
    ("label", "gateway_url"),
    GATEWAY_ENVIRONMENTS,
    ids=[case[0] for case in GATEWAY_ENVIRONMENTS],
)
def test_ping_gateway_returns_false_when_unreachable_and_when_misconfigured(
    label: str, gateway_url: str
) -> None:
    """Catches a ping that raises during the outage it exists to report.

    An unreachable host would raise a transport error and a misconfigured URL
    would raise a ValueError; both must become False rather than propagating,
    or /readyz answers 500 instead of 503.
    """
    # For the misconfigured case we use a fake that would never be called
    # (the ValueError from _gateway_health_url is raised before the GET).
    # For the unreachable case we use the real httpx client so the test hits
    # a closed port — no monkeypatching needed because False is the outcome.
    import httpx

    client = httpx.AsyncClient()
    result = asyncio.run(ping_gateway(client, gateway_url))
    asyncio.run(client.aclose())
    assert result is False, label


def test_ping_gateway_probes_the_origin_of_the_configured_url() -> None:
    """Catches a probe that GETs the versioned base instead of the origin.

    the design fixes GATEWAY_URL as `.../v1`; the console's health endpoint is
    at the service level, so stripping the version path and appending
    GATEWAY_HEALTH_PATH is the only correct probe. A GET of the versioned base
    asserts a route no spec promises and would report a healthy gateway as down.

    The fake answers 204 to prove any 2xx counts as reachable.
    """
    client = _FakeHttpxClient(status_code=204)
    result = asyncio.run(ping_gateway(client, "http://gateway.invalid:8080/v1"))

    assert result is True
    expected_url = "http://gateway.invalid:8080" + GATEWAY_HEALTH_PATH
    assert client.last_url == expected_url, (
        f"expected probe of {expected_url!r}, got {client.last_url!r}"
    )
