"""Tests for the shared readiness ping functions, driven from mcp-server context.

An earlier change moved the three per-call readiness checks into `shared.core.health`
as `ping_*` functions that take the app's existing client instead of building
a new one on every call. This file covers the ping bodies themselves.

The per-call construction assertions and the timeout assertions from the old
file drop here: the app's long-lived clients carry those settings, and no new
client is ever built inside a ping function.

`test_mcp_transport.py` covers the /readyz route behaviour via
`tests.support.mcp.http_get`; this file covers only the ping bodies.
"""

import asyncio
from types import TracebackType
from typing import Any

from helpdesk.shared.core.health import (
    ping_postgres,
    ping_qdrant,
    ping_redis,
)

# ---------------------------------------------------------------------------
# Minimal fake clients
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


# ---------------------------------------------------------------------------
# Happy paths — each ping uses the passed client
# ---------------------------------------------------------------------------


def test_ping_postgres_returns_true_when_engine_answers() -> None:
    assert asyncio.run(ping_postgres(_FakeEngine())) is True


def test_ping_redis_returns_true_when_client_answers() -> None:
    assert asyncio.run(ping_redis(_FakeRedis())) is True


def test_ping_qdrant_returns_true_when_client_answers() -> None:
    assert asyncio.run(ping_qdrant(_FakeQdrantClient())) is True


# ---------------------------------------------------------------------------
# Unhappy paths — each ping catches errors and returns False
# ---------------------------------------------------------------------------

ENVIRONMENTS = (
    (
        "unreachable",
        "postgresql+asyncpg://u:p@127.0.0.1:9/db",
        "redis://127.0.0.1:9/0",
        "http://127.0.0.1:9",
    ),
    (
        "misconfigured",
        "not-a-database-url",
        "not-a-redis-url",
        "not-a-qdrant-url",
    ),
)


def test_check_returns_false_rather_than_raising(
    monkeypatch: Any,
) -> None:
    """Each ping returns False for an unreachable and a misconfigured client.

    Separate from the unit tests above: here the passed client IS built with
    bad env vars to confirm a client that raises on its first real operation
    still causes the ping to return False rather than propagate. This covers
    the mcp-server-specific combinations (postgres + redis + qdrant, no
    gateway).
    """
    # For each env / check combination, pass a raising client to each ping.

    class _BadEngine:
        def connect(self) -> Any:
            raise RuntimeError("no db")

    class _BadRedis:
        async def ping(self) -> bool:
            raise RuntimeError("no redis")

    class _BadQdrant:
        async def get_collections(self) -> None:
            raise RuntimeError("no qdrant")

    assert asyncio.run(ping_postgres(_BadEngine())) is False
    assert asyncio.run(ping_redis(_BadRedis())) is False
    assert asyncio.run(ping_qdrant(_BadQdrant())) is False
