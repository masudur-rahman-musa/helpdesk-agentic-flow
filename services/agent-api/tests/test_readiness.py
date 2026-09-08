"""Unit tests for the real readiness check bodies.

The /readyz route tests fake these checks at the module seam, so this file
covers the checks themselves. False paths point at unreachable localhost
ports; True paths fake the client libraries, since unit tests cannot assume
live infrastructure (the live path is verified manually against compose,
see the plan's closing notes).
"""

import asyncio
from collections.abc import Callable
from types import SimpleNamespace, TracebackType
from typing import Any

import pytest
from agent_api import readiness
from qdrant_client import AsyncQdrantClient

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
    def from_url(cls, url: str, **kwargs: Any) -> "_FakeRedis":
        return cls()

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None


class _FakeQdrantClient:
    def __init__(self, url: str, timeout: int, **kwargs: Any) -> None:
        return None

    async def get_collections(self) -> None:
        return None

    async def close(self) -> None:
        return None


def test_check_postgres_true_when_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, "127.0.0.1:9")
    monkeypatch.setattr(readiness, "create_async_engine", lambda url, **kwargs: _FakeEngine())

    assert asyncio.run(readiness.check_postgres()) is True


def test_check_redis_true_when_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, "127.0.0.1:9")
    monkeypatch.setattr(readiness, "Redis", _FakeRedis)

    assert asyncio.run(readiness.check_redis()) is True


def test_check_qdrant_true_when_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, "127.0.0.1:9")
    monkeypatch.setattr(readiness, "AsyncQdrantClient", _FakeQdrantClient)

    assert asyncio.run(readiness.check_qdrant()) is True


# --- an earlier change: the gateway check, and both timeout halves on all four ---------
#
# The three checks above predate the timeouts. These additions pin the surface
# an earlier change adds:
#
#     readiness.TIMEOUT_SECONDS: int  (2, the value mcp_server/readiness.py uses)
#     async def check_gateway() -> bool
#         probes {scheme}://{netloc}/healthz derived from the versioned
#         GATEWAY_URL (decision E); any 2xx is True; never raises.
#         Builds its client through the module-level name `httpx`, so a test
#         fakes it the way the checks above fake `Redis` and
#         `AsyncQdrantClient`.

GATEWAY_ENVIRONMENTS: tuple[tuple[str, str], ...] = (
    # Port 9 (discard) is closed, so the connection is refused.
    ("unreachable", "http://127.0.0.1:9/v1"),
    # Not a closed port at all: the value cannot be turned into a request,
    # which is the case that used to raise out of a check.
    ("misconfigured", "not-a-gateway-url"),
)


def _stub_httpx(captured: dict[str, Any], status_code: int = 204) -> Any:
    """A stand-in for the `httpx` module that records what the check does.

    Records the `Timeout(...)` kwargs and the URL that is GETed, and answers
    `status_code`. Supports both the `async with` and the `aclose()` shapes,
    so it pins the URL and the timeouts without pinning the lifecycle style.
    """

    class _Response:
        def __init__(self) -> None:
            self.status_code = status_code

    class _AsyncClient:
        def __init__(self, **kwargs: Any) -> None:
            captured["client_kwargs"] = kwargs

        async def __aenter__(self) -> "_AsyncClient":
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> None:
            return None

        async def get(self, url: str, **kwargs: Any) -> _Response:
            captured["url"] = url
            return _Response()

        async def aclose(self) -> None:
            return None

    def _timeout(**kwargs: Any) -> object:
        captured["timeout_kwargs"] = kwargs
        return object()

    return SimpleNamespace(AsyncClient=_AsyncClient, Timeout=_timeout)


@pytest.mark.parametrize(
    ("label", "gateway_url"),
    GATEWAY_ENVIRONMENTS,
    ids=[case[0] for case in GATEWAY_ENVIRONMENTS],
)
def test_check_gateway_is_false_when_unreachable_and_when_misconfigured(
    monkeypatch: pytest.MonkeyPatch, label: str, gateway_url: str
) -> None:
    """Catches a check that raises during the outage it exists to report: a
    refused connection or an unparseable GATEWAY_URL would then answer /readyz
    with a 500 and a stack trace instead of "not ready", so the operator
    learns nothing about which dependency is down."""
    monkeypatch.setenv("GATEWAY_URL", gateway_url)

    assert asyncio.run(readiness.check_gateway()) is False, label


def test_the_gateway_check_probes_the_origin_of_the_configured_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches decision E implemented as a GET of the versioned base: the design
    fixes GATEWAY_URL as `.../v1`, and the console serves health at its
    origin, so probing the base asserts a route no spec promises and reports a
    healthy gateway as down.

    The stub answers 204, so this also pins that a 2xx other than 200 counts
    as reachable.
    """
    captured: dict[str, Any] = {}
    monkeypatch.setenv("GATEWAY_URL", "http://gateway.invalid:8080/v1")
    monkeypatch.setattr(readiness, "httpx", _stub_httpx(captured, status_code=204))

    assert asyncio.run(readiness.check_gateway()) is True

    assert captured["url"] == "http://gateway.invalid:8080/healthz"


def test_every_check_is_built_with_both_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Catches a connect timeout with no read half, which hangs the probe
    against a paused or fenced server: the handshake completes, the read never
    returns, and the container reports ready throughout the outage.

    Asserted on the CONSTRUCTED clients rather than on `TIMEOUT_SECONDS`: a
    test that only reads the constant passes with every timeout argument
    deleted from the module. Qdrant is the odd one out and is read off the
    constructed client, because qdrant-client takes one `timeout` and hands it
    to httpx as both halves at once (the same path
    `test_mcp_readiness.py::test_the_qdrant_check_client_carries_the_timeout`
    reads).
    """
    captured: dict[str, Any] = {}
    # The real class from its own package, not off the readiness module: mypy
    # refuses a re-exported attribute there (`attr-defined`), and the spy below
    # replaces the module attribute anyway.
    real_qdrant = AsyncQdrantClient

    def fake_engine(url: str, **kwargs: Any) -> Any:
        captured["engine"] = kwargs
        raise RuntimeError("construction is all this test needs")

    class FakeRedis:
        @classmethod
        def from_url(cls, url: str, **kwargs: Any) -> Any:
            captured["redis"] = kwargs
            raise RuntimeError("construction is all this test needs")

    def qdrant_spy(*args: Any, **kwargs: Any) -> Any:
        client = real_qdrant(*args, **kwargs)
        captured["qdrant"] = client
        return client

    gateway_captured: dict[str, Any] = {}

    _set_env(monkeypatch, "127.0.0.1:9")
    monkeypatch.setenv("GATEWAY_URL", "http://gateway.invalid:8080/v1")
    monkeypatch.setattr(readiness, "create_async_engine", fake_engine)
    monkeypatch.setattr(readiness, "Redis", FakeRedis)
    monkeypatch.setattr(readiness, "AsyncQdrantClient", qdrant_spy)
    monkeypatch.setattr(readiness, "httpx", _stub_httpx(gateway_captured))

    assert asyncio.run(readiness.check_postgres()) is False
    assert asyncio.run(readiness.check_redis()) is False
    assert asyncio.run(readiness.check_qdrant()) is False
    assert asyncio.run(readiness.check_gateway()) is True

    assert readiness.TIMEOUT_SECONDS == 2
    assert captured["engine"]["connect_args"] == {"timeout": 2, "command_timeout": 2}
    assert captured["engine"]["pool_timeout"] == 2
    assert captured["redis"]["socket_connect_timeout"] == 2
    assert captured["redis"]["socket_timeout"] == 2
    # `captured` is dict[str, Any], so the private attribute walk below is Any
    # to mypy rather than an attribute error on AsyncQdrantClient (standing
    # rule 45): the value is read off the constructed client, not the class.
    qdrant_timeout: float = float(captured["qdrant"]._client._timeout)
    assert qdrant_timeout == 2.0
    assert gateway_captured["timeout_kwargs"] == {
        "connect": 2,
        "read": 2,
        "write": 2,
        "pool": 2,
    }
