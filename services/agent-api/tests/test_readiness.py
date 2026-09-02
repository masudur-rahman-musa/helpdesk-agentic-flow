"""Unit tests for the real readiness check bodies.

The /readyz route tests fake these checks at the module seam, so this file
covers the checks themselves. False paths point at unreachable localhost
ports; True paths fake the client libraries, since unit tests cannot assume
live infrastructure (the live path is verified manually against compose,
see the plan's closing notes).
"""

import asyncio
from collections.abc import Callable
from types import TracebackType
from typing import Any

import pytest
from agent_api import readiness

Check = Callable[[], Any]

CHECKS: tuple[Check, Check, Check] = (
    readiness.check_postgres,
    readiness.check_redis,
    readiness.check_qdrant,
)


def _set_env(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    monkeypatch.setenv("DATABASE_URL", f"postgresql+asyncpg://u:p@{host}/db")
    monkeypatch.setenv("REDIS_URL", f"redis://{host}/0")
    monkeypatch.setenv("QDRANT_URL", f"http://{host}")


@pytest.mark.parametrize("check", CHECKS)
def test_check_returns_false_when_dependency_unreachable(
    monkeypatch: pytest.MonkeyPatch, check: Check
) -> None:
    # Port 9 (discard) is closed; every client fails to connect and the
    # check must swallow that into False, never raise.
    _set_env(monkeypatch, "127.0.0.1:9")

    assert asyncio.run(check()) is False


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

    async def dispose(self) -> None:
        return None


class _FakeRedis:
    @classmethod
    def from_url(cls, url: str) -> "_FakeRedis":
        return cls()

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


class _FakeQdrantClient:
    def __init__(self, url: str, timeout: int) -> None:
        return None

    async def get_collections(self) -> None:
        return None

    async def close(self) -> None:
        return None


def test_check_postgres_true_when_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, "127.0.0.1:9")
    monkeypatch.setattr(readiness, "create_async_engine", lambda url: _FakeEngine())

    assert asyncio.run(readiness.check_postgres()) is True


def test_check_redis_true_when_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, "127.0.0.1:9")
    monkeypatch.setattr(readiness, "Redis", _FakeRedis)

    assert asyncio.run(readiness.check_redis()) is True


def test_check_qdrant_true_when_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, "127.0.0.1:9")
    monkeypatch.setattr(readiness, "AsyncQdrantClient", _FakeQdrantClient)

    assert asyncio.run(readiness.check_qdrant()) is True
