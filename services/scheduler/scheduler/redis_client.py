"""The one Redis accessor the scheduler uses, and the timeouts it is pinned to.

`mcp_server.redis_client`'s shape, with a public accessor: the runner takes
the cadence-gate lock through it on every tick, and the lifespan closes the
loop's clients on shutdown. Not imported from that service — two services do
not share a private module — so the two are siblings, and a change to the
timeout has to be made in both places on purpose.
"""

import asyncio
from weakref import WeakKeyDictionary

from helpdesk.core.settings import Settings
from redis.asyncio import Redis

REDIS_TIMEOUT_SECONDS = 2
"""Connect AND read timeout for the job-lock client.

Both halves, because a connect timeout alone leaves a completed handshake
free to hang on the read: a blackholed Redis (packets dropped rather than
refused) would then hold the scheduler's single loop for ever, so no job of
any tenant ever runs again — and nothing reports it, because the loop is not
what `/readyz` reads. Asserted on the CONSTRUCTED client, not on this
constant, by
`test_scheduler_runner.py::test_the_redis_client_carries_both_timeout_halves`.
"""

_SHARED_REDIS: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, Redis]] = WeakKeyDictionary()


def get_redis(settings: Settings) -> Redis:
    """A process-shared Redis client for the running event loop.

    Keyed by (event loop, URL) for the same reason `helpdesk.core.db`'s
    `shared_engine` is: a pooled connection belongs to the loop that opened
    it. Under the service this is one client for the life of the process;
    under a test that drives each call through its own `asyncio.run`, one per
    loop. Entries drop with their loop.
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
