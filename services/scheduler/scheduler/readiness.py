"""Dependency reachability checks for the scheduler's /readyz.

Two dependencies, and both are on the path of every tick: Postgres is where
the work is, and Redis holds the cadence gate, so a scheduler with no Redis
claims nothing and runs nothing at all. A readiness answer naming only one of
them would report ready straight through the other's outage.

`mcp_server.readiness`'s shape, minus Qdrant, and separate rather than
imported for the same reason `redis_client` is: two services do not share a
private module.

Each check answers one question and never raises — including when the
configuration itself is wrong, which is why building the client is inside the
`try`: a malformed `DATABASE_URL` must make /readyz say "not ready", not hand
the operator a 500 during the outage the endpoint exists to report.

Connections are opened per call, never pooled: readiness must probe the
dependency now, not report on a connection from before it went down. Both
carry both timeout halves, because a dependency that drops packets rather
than refusing them would otherwise hang the check and leave the container
reporting ready throughout.
"""

from helpdesk.core.settings import load_settings
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

TIMEOUT_SECONDS = 2
"""Connect AND read timeout for both checks.

asyncpg spells the read half `command_timeout` and redis spells it
`socket_timeout`; both halves are passed on both clients.
"""


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
