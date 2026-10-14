"""The one Redis accessor this service uses, and the timeouts it is pinned to.

Moved out of `pipeline.py` by an earlier change, unchanged: the pipeline reads the kill
switch before every call, and `set_kill_switch`'s handler WRITES one, so both
need the same per-loop pooled client. A handler cannot import `dispatch` for
it — `dispatch` imports `registry`, which imports `tools.killswitch`, which is
the handler — so the accessor lives here and both import it (decision F).

`dispatch` re-exports `_redis` and `REDIS_TIMEOUT_SECONDS` under their old
names, so `test_mcp_pipeline.py::test_the_redis_client_carries_the_pinned_timeouts`
keeps covering this module from its old call site.
"""

import asyncio
from weakref import WeakKeyDictionary

from redis.asyncio import Redis

from helpdesk.shared.core.settings import Settings

REDIS_TIMEOUT_SECONDS = 2
"""Connect and read timeout for the kill-switch client.

Without it a blackholed Redis (packets dropped rather than refused) hangs
every tool call forever inside `KillSwitch.check`, instead of the `INTERNAL`
the fail-closed design promises. Pinned exactly by
`test_mcp_pipeline.py::test_the_redis_client_carries_the_pinned_timeouts`."""

_SHARED_REDIS: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, Redis]] = WeakKeyDictionary()


def _redis(settings: Settings) -> Redis:
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

    Called by `pipeline.close_shared_connections`, which keeps its home and
    its behaviour: a pool belonging to a dead loop is a connection nothing
    will ever close.
    """
    loop = asyncio.get_running_loop()
    for client in _SHARED_REDIS.pop(loop, {}).values():
        await client.aclose()


__all__ = ["REDIS_TIMEOUT_SECONDS", "close_loop_clients"]
