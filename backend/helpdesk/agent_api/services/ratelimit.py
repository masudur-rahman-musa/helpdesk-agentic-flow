"""The per-session chat rate limit (the design), as a fixed window over Redis.

In the service rather than in `packages/helpdesk`: only
the chat surface rate-limits, and the package list the design fixes stays as it
is. A second service that needs one moves this module then, with a caller to
shape it.

A FIXED window rather than a sliding log (decision C1). The count is one
integer per session per window, which is what makes it cheap enough to run in
front of every turn, and the worst case a fixed window admits — twice the cap
across a boundary — is not the threat the limit exists for. It bounds a
runaway client; it does not meter billing.

Failing CLOSED on a Redis error is also decision C1: Redis is already a hard
dependency of every tool call, so a chat turn cannot
complete without it anyway. Failing open would buy nothing and would remove
the one control bounding a runaway client exactly when nobody can see it.
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
"""The namespace every counter key opens with (decision C1)."""

RATE_LIMIT_MESSAGES = 30
"""How many messages one session may send inside one window (the design).

Inclusive: the thirtieth passes and the thirty-first is refused, pinned from
both sides by
`test_rate_limit.py::test_the_thirtieth_message_passes_and_the_thirty_first_is_refused`.
"""

RATE_LIMIT_WINDOW_SECONDS = 600
"""The window's length in seconds, and the `EXPIRE` argument the first hit sets.

Ten minutes (the design). It is both the bucket width and the key's TTL, which
is what makes the counter self-cleaning: a session that stops talking leaves
nothing behind. Pinned as the literal `EXPIRE` argument by
`test_rate_limit.py::test_the_window_expires_after_exactly_six_hundred_seconds`.
"""


class RateLimitExceededError(ToolError):
    """This session has spent its window's allowance.

    `POLICY_BLOCKED` rather than `FORBIDDEN`: a platform rule blocked a request
    the caller may make again later, while `FORBIDDEN` reads as a permission
    decision that can never succeed. `TOOL_DISABLED` is the kill switch's
    answer and means something else again. Pinned by
    `test_rate_limit.py::test_the_thirtieth_message_passes_and_the_thirty_first_is_refused`.
    """

    code = ErrorCode.POLICY_BLOCKED


class RateLimitUnavailableError(ToolError):
    """The counter could not be reached, so the turn is refused (decision C1).

    The same answer `helpdesk.killswitch.switch.SwitchUnavailableError` gives
    for the same outage, for the same reason: a control that cannot be
    evaluated is not a control that passed. Pinned by
    `test_rate_limit.py::test_an_unreachable_redis_refuses`, whose client
    points at a port nothing listens on.
    """

    code = ErrorCode.INTERNAL


def key_for(*, tenant_id: uuid.UUID, session_id: str, now: datetime) -> str:
    """The counter key for one session in the window `now` falls in.

    PURE, and the session id is pre-checked here rather than after the counter
    has been touched: the id lands in a Redis key, which
    sizes and patterns nothing of its own. The gate is `check_slug` — the same
    one `checkpointer.thread_config` runs on the same value — so the bound
    before a checkpoint write and the bound before a counter are one check
    against one constant, not two that can drift. a project rule rides on it:
    `check_slug` matches in full, so a trailing newline cannot make one session
    into two counters and hand a client twice its allowance. Pinned by
    `test_rate_limit.py::test_a_session_id_that_is_not_a_slug_is_refused_before_any_redis_call`
    and `::test_the_session_id_bound_is_check_slugs_own_inclusive_bound`.

    The tenant is in the key as well as the session: session ids are unique in
    practice, but a counter shared by two tenants on a collision would be a
    cross-tenant effect, and the key is the only place that can be ruled out.
    Pinned by `::test_the_key_is_built_from_the_tenant_the_session_and_the_window`.
    """
    window = int(now.timestamp()) // RATE_LIMIT_WINDOW_SECONDS
    return f"{KEY_PREFIX}:{tenant_id}:{check_slug(session_id)}:{window}"


async def check_rate_limit(
    client: Redis, *, tenant_id: uuid.UUID, session_id: str, now: datetime
) -> None:
    """Count this message against the session's window, or refuse it.

    One `INCR`, and one `EXPIRE` on the FIRST hit of a window and on no later
    one. Setting the TTL every time would slide the window and bar a steady
    talker permanently; never setting it would leave the key — and the refusal
    — in place for ever. Both halves are pinned by
    `test_rate_limit.py::test_the_window_expires_after_exactly_six_hundred_seconds`.

    `OSError` alongside `RedisError` because a refused dial reaches here
    unwrapped on some paths, and that outage is exactly what this branch is
    for. Nothing broader: an `AssertionError` from a test's poisoned client
    must not be swallowed into a typed refusal.
    """
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
