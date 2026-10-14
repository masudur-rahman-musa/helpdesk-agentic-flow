"""Dependency reachability checks for the mcp-server's /readyz.

Three dependencies. Redis is on the path of EVERY tool call, not just the
mutating ones, and Qdrant arrived with
`search_policy` at an earlier change: after that row a tool call fails when the store
is down, so a container reporting ready through a Qdrant outage would be
reporting on an outage it can see (15b decision D).

Each check answers one question and never raises — including when the
configuration itself is wrong. Building the client is inside the `try` for
that reason: a malformed `DATABASE_URL` must make /readyz say "not ready",
not hand the operator a 500 and a stack trace during the outage the endpoint
exists to report. Pinned by the `misconfigured` rows of
`test_mcp_readiness.py::test_check_returns_false_rather_than_raising`.

Connections are opened per call: readiness must probe the dependency now,
not report on a pooled connection from before it went down. Every check
carries an explicit timeout, because a dependency that drops packets rather
than refusing them would otherwise hang the check, hold the worker, and
leave the container ready throughout. The Qdrant check does NOT reuse
`rag_clients.qdrant`'s pooled client, for that same reason.
"""

from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from helpdesk.shared.core.config import load_settings

TIMEOUT_SECONDS = 2
"""Connect AND read timeout for both checks, both halves on both clients.

A connect timeout alone is not enough: a completed handshake followed by a
fenced or paused server hangs the read, which is the same outage with a
later hang. asyncpg spells the read half `command_timeout`, redis spells it
`socket_timeout`, and qdrant-client takes ONE `timeout` which it hands to
httpx as both halves at once. Pinned by
`test_mcp_readiness.py::test_every_client_is_built_with_both_timeouts` for
the first two, which asserts on the kwargs the clients are actually
constructed with rather than on this constant, and by
`::test_the_qdrant_check_client_carries_the_timeout` for the third, which
reads it off the constructed client because there the two halves are one
argument."""


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


async def check_qdrant() -> bool:
    """Can the policy corpus be reached at all?

    `get_collections`, not a bare TCP connect: a Qdrant answering its port
    while its storage is unavailable is still an outage for `search_policy`.

    Its own client rather than `rag_clients.qdrant`'s pooled one, for the
    reason at the top of this module: readiness must probe the store now, and
    a pooled connection opened before the outage would report on nothing.
    Refuses rather than raising for an unreachable AND a malformed URL, both
    pinned by `test_mcp_readiness.py::test_check_returns_false_rather_than_raising`.
    """
    client: AsyncQdrantClient | None = None
    try:
        client = AsyncQdrantClient(url=load_settings().qdrant_url, timeout=TIMEOUT_SECONDS)
        await client.get_collections()
        return True
    except Exception:
        return False
    finally:
        if client is not None:
            await client.close()
