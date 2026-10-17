"""The per-session chat rate limit, as a fixed window counted in Redis.

It stops runaway clients; it is not billing. A Redis error refuses the turn.
"""

import uuid
from datetime import datetime

from redis.asyncio import Redis
from redis.exceptions import RedisError

from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.core.naming import check_slug

__all__ = [
    "RATE_LIMIT_MESSAGES",
    "RATE_LIMIT_WINDOW_SECONDS",
    "RateLimitExceededError",
    "RateLimitUnavailableError",
    "check_rate_limit",
    "key_for",
]

KEY_PREFIX = "ratelimit"

RATE_LIMIT_MESSAGES = 30
"""Messages one session may send per window. The thirtieth passes, the next fails."""

RATE_LIMIT_WINDOW_SECONDS = 600
"""Window length, and the key's expiry, so idle sessions leave nothing behind."""


class RateLimitExceededError(ToolError):
    """This session used up its window. `POLICY_BLOCKED`: it may work later."""

    code = ErrorCode.POLICY_BLOCKED


class RateLimitUnavailableError(ToolError):
    """The counter could not be reached. A check that cannot run has not passed."""

    code = ErrorCode.INTERNAL


def key_for(*, tenant_id: uuid.UUID, session_id: str, now: datetime) -> str:
    """The counter key for one session and window.

    The session id is checked first, and the tenant is in the key too.
    """
    window = int(now.timestamp()) // RATE_LIMIT_WINDOW_SECONDS
    return f"{KEY_PREFIX}:{tenant_id}:{check_slug(session_id)}:{window}"


async def check_rate_limit(
    client: Redis, *, tenant_id: uuid.UUID, session_id: str, now: datetime
) -> None:
    """Count this message, setting expiry only on the first hit, or refuse it."""
    key = key_for(tenant_id=tenant_id, session_id=session_id, now=now)
    try:
        count = int(await client.incr(key))
        if count == 1:
            await client.expire(key, RATE_LIMIT_WINDOW_SECONDS)
    except (RedisError, OSError) as exc:
        raise RateLimitUnavailableError("the rate limit counter could not be reached") from exc
    if count > RATE_LIMIT_MESSAGES:
        raise RateLimitExceededError(
            f"this session has sent {RATE_LIMIT_MESSAGES} messages in the last "
            f"{RATE_LIMIT_WINDOW_SECONDS} seconds"
        )
