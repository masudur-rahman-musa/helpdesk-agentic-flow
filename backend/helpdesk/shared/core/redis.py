"""The one Redis client the apps share, per event loop."""

import asyncio
from weakref import WeakKeyDictionary

from redis.asyncio import Redis

from helpdesk.shared.core.config import Settings

REDIS_TIMEOUT_SECONDS = 2
"""Connect and read timeout. Without both, a silent Redis hangs every tool call
and the scheduler's loop, instead of failing closed."""

_SHARED_REDIS: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, Redis]] = WeakKeyDictionary()


def get_redis(settings: Settings) -> Redis:
    """A shared client per (event loop, URL): a pooled connection belongs to its loop."""
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
    """Close the running loop's clients; nothing else would once the loop dies."""
    loop = asyncio.get_running_loop()
    for client in _SHARED_REDIS.pop(loop, {}).values():
        await client.aclose()


__all__ = ["REDIS_TIMEOUT_SECONDS", "close_loop_clients", "get_redis"]
