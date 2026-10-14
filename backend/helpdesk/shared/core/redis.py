"""The one Redis accessor the apps share, and the timeouts it is pinned to.

The mcp-server reads the kill switch before every tool call and
`set_kill_switch`'s handler writes one; the scheduler takes the cadence-gate
lock on every tick. All of them want the same per-loop pooled client, so the
accessor lives here in `shared.core` and each app imports it rather than
keeping its own copy. The public name is `get_redis`; `mcp_server.tools.dispatch`
re-exports it as `_redis` so `test_mcp_pipeline.py` keeps reading it (and
`REDIS_TIMEOUT_SECONDS`) from its old call site.
"""

import asyncio
from weakref import WeakKeyDictionary

from redis.asyncio import Redis

from helpdesk.shared.core.config import Settings

REDIS_TIMEOUT_SECONDS = 2
"""Connect and read timeout for the pooled client, both halves on both apps.

Without it a blackholed Redis (packets dropped rather than refused) hangs
every tool call forever inside `KillSwitch.check` instead of the `INTERNAL`
the fail-closed design promises, and holds the scheduler's single loop so no
job of any tenant ever runs again. A connect timeout alone is not enough: a
completed handshake followed by a fenced server hangs the read. Pinned exactly
by `test_mcp_pipeline.py::test_the_redis_client_carries_the_pinned_timeouts`
and, on the constructed client, by
`test_scheduler_runner.py::test_the_redis_client_carries_both_timeout_halves`."""

_SHARED_REDIS: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, Redis]] = WeakKeyDictionary()


def get_redis(settings: Settings) -> Redis:
    """A process-shared Redis client for the running event loop.

    Keyed by (event loop, URL) for the same reason `helpdesk.core.db`'s
    `shared_engine` is: a pooled connection belongs to the loop that opened
    it. Under a server this is one client per process; under a test that
    drives each call through its own `asyncio.run`, it is one per loop.
    Entries drop with their loop.
    """
    loop = asyncio.get_running_loop()
    clients = _SHARED_REDIS.setdefault(loop, {})
    client = clients.get(settings.redis_url)
    if client is None:
        client = Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=REDIS_TIMEOUT_SECONDS,
            socket_timeout=REDIS_TIMEOUT_SECONDS,
        )
        clients[settings.redis_url] = client
    return client


async def close_loop_clients() -> None:
    """Close and forget the running loop's pooled Redis clients.

    A pool belonging to a dead loop is a connection nothing will ever close.
    """
    loop = asyncio.get_running_loop()
    for client in _SHARED_REDIS.pop(loop, {}).values():
        await client.aclose()


__all__ = ["REDIS_TIMEOUT_SECONDS", "close_loop_clients", "get_redis"]
