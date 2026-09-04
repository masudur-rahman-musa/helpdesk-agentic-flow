"""An earlier change: the real readiness check bodies, not the faked seam.

`test_mcp_transport.py` fakes `readiness.check_*` to drive the /readyz route
and calls the real ones once against the live compose stack, which covers the
True paths. This file covers the rest: a dependency that is not there, and a
configuration that is wrong, must both come back False rather than raise —
or /readyz answers 500 during the outage it exists to report.

the project notes 2026-09-03: when a plan's tests fake a seam, the code
behind the seam needs tests of its own.
"""

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from mcp_server import readiness

Check = Callable[[], Any]

CHECKS: tuple[tuple[str, Check], ...] = (
    ("postgres", readiness.check_postgres),
    ("redis", readiness.check_redis),
)

# Port 9 (discard) is closed, so every client fails to connect. The
# misconfigured pair is not a closed port at all: the URL cannot be parsed
# into a client, which used to raise out of the check.
ENVIRONMENTS: tuple[tuple[str, str, str], ...] = (
    (
        "unreachable",
        "postgresql+asyncpg://u:p@127.0.0.1:9/db",
        "redis://127.0.0.1:9/0",
    ),
    (
        "misconfigured",
        "not-a-database-url",
        "not-a-redis-url",
    ),
)

CASES = [
    (f"{env}-{name}", database_url, redis_url, check)
    for env, database_url, redis_url in ENVIRONMENTS
    for name, check in CHECKS
]


@pytest.mark.parametrize(
    ("label", "database_url", "redis_url", "check"), CASES, ids=[case[0] for case in CASES]
)
def test_check_returns_false_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
    label: str,
    database_url: str,
    redis_url: str,
    check: Check,
) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("REDIS_URL", redis_url)

    assert asyncio.run(check()) is False, label


def test_both_clients_are_built_with_both_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Asserted on the constructed clients, not on the constant.

    A test that only reads `TIMEOUT_SECONDS` passes with every timeout
    argument deleted from the module, which is the behaviour it claims to
    pin. Both halves are checked on both clients: a connect timeout alone
    leaves a completed handshake free to hang on the read.
    """
    captured: dict[str, Any] = {}

    def fake_engine(url: str, **kwargs: Any) -> Any:
        captured["engine"] = kwargs
        raise RuntimeError("construction is all this test needs")

    class FakeRedis:
        @classmethod
        def from_url(cls, url: str, **kwargs: Any) -> Any:
            captured["redis"] = kwargs
            raise RuntimeError("construction is all this test needs")

    monkeypatch.setattr(readiness, "create_async_engine", fake_engine)
    monkeypatch.setattr(readiness, "Redis", FakeRedis)

    assert asyncio.run(readiness.check_postgres()) is False
    assert asyncio.run(readiness.check_redis()) is False

    assert readiness.TIMEOUT_SECONDS == 2
    assert captured["engine"]["connect_args"] == {"timeout": 2, "command_timeout": 2}
    assert captured["engine"]["pool_timeout"] == 2
    assert captured["redis"]["socket_connect_timeout"] == 2
    assert captured["redis"]["socket_timeout"] == 2
