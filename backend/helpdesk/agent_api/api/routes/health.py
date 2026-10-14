"""Dependency reachability checks for /readyz.

Four dependencies. Postgres, Redis and Qdrant are on the path of a chat turn;
the gateway joined them at an earlier change, because the design names it a dependency of
this service and a container reporting ready while every model call fails is
the failure this endpoint exists to prevent. The cost, stated so it is a
decision: an environment with no gateway configured reports not-ready.

Each check answers one question and never raises — including when the
configuration itself is wrong. Building the client is inside the `try` for
that reason: a malformed `DATABASE_URL` or an unparseable `GATEWAY_URL` must
make /readyz say "not ready", not hand the operator a 500 and a stack trace
during the outage the endpoint exists to report. Pinned for the gateway by
`test_readiness.py::test_check_gateway_is_false_when_unreachable_and_when_misconfigured`.

Connections are opened per call: readiness must probe the dependency now, not
report on a pooled connection from before it went down.
"""

from urllib.parse import urlsplit

import httpx
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from helpdesk.shared.core.config import load_settings

TIMEOUT_SECONDS = 2
"""Connect AND read timeout for every check, both halves on every client.

The same value `mcp_server/readiness.py` uses, for the same reason: a connect
timeout alone is not enough, because a completed handshake followed by a
fenced or paused server hangs the read, which is the same outage with a later
hang. asyncpg spells the read half `command_timeout`, redis spells it
`socket_timeout`, qdrant-client takes ONE `timeout` it hands to httpx as both
halves at once, and httpx takes all four halves explicitly. Pinned by
`test_readiness.py::test_every_check_is_built_with_both_timeouts`, which
asserts on the kwargs the clients are actually constructed with rather than on
this constant."""

GATEWAY_HEALTH_PATH = "/healthz"
"""The gateway's health route, at the ORIGIN of `GATEWAY_URL` (decision E).

the design fixes `GATEWAY_URL` as a versioned base (`…/v1`) and the console's
health endpoint is a service-level one, so the path is stripped and this is
appended. A GET of the versioned base would be asserting on a route no spec
promises. Cross-repo assumption, recorded in an earlier change's first risk: if the
console does not serve this, the agent reports itself not-ready against a
healthy gateway, and this is the one constant to change. Pinned by
`test_readiness.py::test_the_gateway_check_probes_the_origin_of_the_configured_url`."""


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


async def check_gateway() -> bool:
    """Is the Ops Console gateway answering at all?

    Any 2xx counts as reachable: what is being asked is whether the dependency
    is up, and a console that answers its health route with a 204 is up. A
    non-2xx, an unreachable host and a `GATEWAY_URL` that cannot be turned
    into a request are all one answer, False, for the reason at the top of
    this module.
    """
    try:
        url = _gateway_health_url(load_settings().gateway_url)
        timeout = httpx.Timeout(
            connect=TIMEOUT_SECONDS,
            read=TIMEOUT_SECONDS,
            write=TIMEOUT_SECONDS,
            pool=TIMEOUT_SECONDS,
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url)
        return 200 <= response.status_code < 300
    except Exception:
        return False


def _gateway_health_url(gateway_url: str) -> str:
    """`{scheme}://{netloc}{GATEWAY_HEALTH_PATH}` for a versioned base.

    Raises rather than guessing when the configured value carries no scheme or
    no host: a health probe of a URL assembled from half a configuration would
    report on nothing, and the caller turns the raise into False.
    """
    parts = urlsplit(gateway_url)
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"GATEWAY_URL is not an absolute URL: {gateway_url!r}")
    return f"{parts.scheme}://{parts.netloc}{GATEWAY_HEALTH_PATH}"
