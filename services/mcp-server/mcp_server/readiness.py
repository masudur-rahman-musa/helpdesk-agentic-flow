"""Dependency reachability checks for the mcp-server's /readyz.

Two dependencies, not three: this service reaches Postgres and Redis, and
Redis is on the path of every tool call, not just the mutating ones (plan
09b decision C, fail closed). Qdrant arrives with the RAG tools at row 15.

Each check answers one question and never raises — including when the
configuration itself is wrong. Building the client is inside the `try` for
that reason: a malformed `DATABASE_URL` must make /readyz say "not ready",
not hand the operator a 500 and a stack trace during the outage the endpoint
exists to report. Pinned by the `misconfigured` rows of
`test_mcp_readiness.py::test_check_returns_false_rather_than_raising`.

Connections are opened per call: readiness must probe the dependency now,
not report on a pooled connection from before it went down. Both checks
carry an explicit timeout, because a dependency that drops packets rather
than refusing them would otherwise hang the check, hold the worker, and
leave the container ready throughout.
"""

from helpdesk.core.settings import load_settings
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

TIMEOUT_SECONDS = 2
"""Connect AND read timeout for both checks, both halves on both clients.

A connect timeout alone is not enough: a completed handshake followed by a
fenced or paused server hangs the read, which is the same outage with a
later hang. asyncpg spells the read half `command_timeout`, redis spells it
`socket_timeout`. Pinned by
`test_mcp_readiness.py::test_both_clients_are_built_with_both_timeouts`,
which asserts on the kwargs the clients are actually constructed with, not
on this constant."""


async def check_postgres() -> bool:
    engine = None
    try:
        engine = create_async_engine(
            load_settings().database_url,
            connect_args={
                "timeout": TIMEOUT_SECONDS,
                "command_timeout": TIMEOUT_SECONDS,
            },
            pool_timeout=TIMEOUT_SECONDS,
        )
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
    finally:
        if engine is not None:
            await engine.dispose()


async def check_redis() -> bool:
    client: Redis | None = None
    try:
        client = Redis.from_url(
            load_settings().redis_url,
            socket_connect_timeout=TIMEOUT_SECONDS,
            socket_timeout=TIMEOUT_SECONDS,
        )
        await client.ping()
        return True
    except Exception:
        return False
    finally:
        if client is not None:
            await client.aclose()
