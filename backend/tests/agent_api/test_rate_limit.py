"""An earlier change §6: the per-session rate limit, with no app at all.

Expected implementation surface (the implementer must provide exactly this):

``agent_api.ratelimit``
    ``RATE_LIMIT_MESSAGES = 30`` — messages one session may send per window
        (the design, plan decision C1).
    ``RATE_LIMIT_WINDOW_SECONDS = 600`` — the fixed window's length, and the
        ``EXPIRE`` argument the first hit of a window sets.
    ``def key_for(*, tenant_id: uuid.UUID, session_id: str, now: datetime) -> str``
        — ``ratelimit:{tenant_id}:{session_id}:{window}``, where ``window`` is
        the fixed-window index ``int(now.timestamp()) // RATE_LIMIT_WINDOW_SECONDS``.
        PURE, and it runs ``check_slug(session_id)`` BEFORE building the key,
        so the value that lands in a Redis key is bounded by the same gate
        ``checkpointer.thread_config`` uses (§1's input-validation criterion).
    ``class RateLimitExceededError(ToolError)`` — ``code = ErrorCode.POLICY_BLOCKED``,
        what the 31st message in one window raises.
    ``class RateLimitUnavailableError(ToolError)`` — ``code = ErrorCode.INTERNAL``,
        what an unreachable Redis raises (decision C1, fail CLOSED).
    ``async def check_rate_limit(client: Redis, *, tenant_id: uuid.UUID,
        session_id: str, now: datetime) -> None`` — one ``INCR`` on the key,
        one ``EXPIRE`` at ``RATE_LIMIT_WINDOW_SECONDS`` on the FIRST hit of a
        window and on no later one, and a typed refusal past the cap.

Two values the plan's §6 left open and this file fixes, for the implementer to
record in the plan's Deviations: the code carried by the over-limit refusal
(``POLICY_BLOCKED`` — a platform rule blocked a request the caller may retry
later; ``FORBIDDEN`` would read as a permission decision that can never
succeed, and ``TOOL_DISABLED`` is the kill switch's) and the code carried by
the unreachable-Redis refusal (``INTERNAL``, exactly as
``helpdesk.killswitch.switch.SwitchUnavailableError`` answers the same outage).

Cost and isolation. Redis is the compose stack's, on a database index
dedicated to the suite; nothing here flushes anything and nothing here opens a
Postgres connection. Per-case isolation is a fresh tenant id and a fresh
session id per case — a namespace, never a rebuilt client —
so two cases cannot share a counter even inside one window. Fixtures live in
this module rather than a conftest, because a second ``conftest`` under
``services/agent-api/tests`` would collide with
``packages/helpdesk/tests/conftest.py`` under the repo-wide ``mypy`` run.
"""

import asyncio
import os
import socket
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

import pytest
from pydantic import ValidationError
from redis.asyncio import Redis

from helpdesk.agent_api.services.ratelimit import (
    RATE_LIMIT_MESSAGES,
    RATE_LIMIT_WINDOW_SECONDS,
    RateLimitExceededError,
    RateLimitUnavailableError,
    check_rate_limit,
    key_for,
)
from helpdesk.shared.core.settings import load_settings
from helpdesk.shared.errors import ErrorCode, ToolError
from helpdesk.shared.rag.naming import SLUG_LENGTH

NOW = datetime(2026, 6, 2, 10, 15, tzinfo=UTC)
"""One fixed clock, so the window index below is a literal and not a formula."""

WINDOW_INDEX = 2967325
"""`int(NOW.timestamp()) // 600` written out.

`NOW` is 1780395300 seconds after the epoch, so the fixed window it falls in
is number 2967325, which began at 10:10:00Z. Written as a literal because a
test that recomputed the implementation's arithmetic would agree with any
window definition the implementation happened to pick.
"""

# Obviously fake, tests only: neither is a real identifier.
KEY_TENANT_ID = uuid.UUID("11111111-1111-5111-8111-111111111111")
KEY_SESSION_ID = "5f7b1c2a-0000-4000-8000-000000000001"

EXPECTED_KEY = f"ratelimit:{KEY_TENANT_ID}:{KEY_SESSION_ID}:{WINDOW_INDEX}"
"""The exact key string decision C1 fixes, spelled out rather than built."""

TEST_REDIS_DB = 15
_FALLBACK_TEST_REDIS_DB = 14


# Under pytest-xdist each worker gets its own Redis index: this file's base
# minus 4 per worker. The four suites that flush Redis use bases 15, 12 and 10
# (the rate limit shares 15, as it always has), so three workers fit in Redis's
# 16 indexes with no two suites or workers on one index. A plain run is worker 0.
_MAX_REDIS_WORKERS = 3


def _worker_redis_index(base: int, fallback: int, configured: int) -> int:
    number = os.environ.get("PYTEST_XDIST_WORKER", "gw0").removeprefix("gw")
    worker = int(number) if number.isdigit() else 0
    if worker >= _MAX_REDIS_WORKERS:
        pytest.fail(
            f"pytest-xdist worker gw{worker}: at most {_MAX_REDIS_WORKERS} workers get "
            "their own Redis index. Run with -n 3 or fewer.",
            pytrace=False,
        )
    index = base - 4 * worker
    return index if index != configured else fallback - 4 * worker


_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ""})

_REDIS_UNREACHABLE_MSG = (
    "Redis is unreachable at the configured REDIS_URL.\n"
    "Plan 19a rate limit tests require the compose stack: run `docker compose up -d` first.\n"
    "Original error: {error!r}"
)


# --- the Redis the counter runs against ---------------------------------------


def _redis_url_for_db(url: str, index: int) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{index}", parts.query, parts.fragment))


def _redis_db_index(url: str) -> int:
    path = urlsplit(url).path.lstrip("/")
    return int(path) if path.isdigit() else 0


async def _ping(url: str) -> None:
    client: Redis = Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
    try:
        await client.ping()
    finally:
        await client.aclose()


@pytest.fixture(scope="session")
def redis_url() -> str:
    """One dedicated Redis database index for the whole module.

    Nothing is flushed, here or anywhere in this file: every case owns a fresh
    tenant id and a fresh session id, so its keys cannot collide with another
    case's or with a developer's. A missing Redis FAILS loudly rather than
    skipping — a rate limit that silently went untested is the one control
    bounding a runaway client.
    """
    try:
        settings = load_settings()
    except ValidationError as exc:
        pytest.fail(
            f"REDIS_URL is not configured (env or .env). Copy .env.example to .env.\n{exc}",
            pytrace=False,
        )
    host = urlsplit(settings.redis_url).hostname or ""
    if host not in _LOCAL_HOSTS:
        pytest.fail(
            f"REDIS_URL points at {host!r}, not this machine. Point it at the compose stack.",
            pytrace=False,
        )
    configured = _redis_db_index(settings.redis_url)
    index = _worker_redis_index(TEST_REDIS_DB, _FALLBACK_TEST_REDIS_DB, configured)
    url = _redis_url_for_db(settings.redis_url, index)
    try:
        asyncio.run(_ping(url))
    except Exception as exc:
        pytest.fail(_REDIS_UNREACHABLE_MSG.format(error=exc), pytrace=False)
    return url


@asynccontextmanager
async def _client(url: str) -> AsyncIterator[Redis]:
    client: Redis = Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def session_id() -> Iterator[str]:
    """A fresh session id per case: the namespace that keeps counters apart."""
    yield str(uuid.uuid4())


@pytest.fixture
def tenant_id() -> Iterator[uuid.UUID]:
    yield uuid.uuid4()


class PoisonedRedis:
    """A client whose every use fails the case outright.

    Not a spy that records: the point is that the pre-check runs BEFORE any
    Redis call at all, so touching this object at all is the failure.
    """

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(
            f"the limiter reached Redis ({name!r}) with a session id that is not a slug; "
            "the pre-check must run before any counter is touched"
        )


class RecordingRedis:
    """A real client with every command it is sent written down.

    Wrapping the real store rather than faking it: the counting cases have to
    hold against Redis's own `INCR` semantics, while `EXPIRE`'s argument has to
    be assertable as the literal 600, which a TTL read back
    afterwards could only ever bound.
    """

    def __init__(self, client: Redis) -> None:
        self._client = client
        self.commands: list[tuple[str, tuple[Any, ...]]] = []

    async def incr(self, name: str, amount: int = 1) -> int:
        self.commands.append(("incr", (name, amount)))
        return int(await self._client.incr(name, amount))

    async def expire(self, name: str, time: int) -> bool:
        self.commands.append(("expire", (name, time)))
        return bool(await self._client.expire(name, time))

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(
            f"the limiter sent Redis an unexpected command {name!r}; decision C1 fixes the "
            "counter as one INCR plus one EXPIRE on the first hit of a window"
        )

    @property
    def expire_arguments(self) -> list[tuple[Any, ...]]:
        return [arguments for command, arguments in self.commands if command == "expire"]


def _unused_port() -> int:
    """A port nothing is listening on: bind, read the port, close it again."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


# --- the key, and the pre-check in front of it --------------------------------


def test_the_key_is_built_from_the_tenant_the_session_and_the_window() -> None:
    """Catches a key builder that leaves the TENANT out (or the window), which
    would make one counter serve two tenants that happened to collide on a
    session id, and would make the fixed window a counter that never resets.

    The window index is the literal 2967325, not `int(now.timestamp()) // 600`
    recomputed here: an assertion that repeated the implementation's arithmetic
    would agree with whatever bucketing it chose.
    """
    key = key_for(tenant_id=KEY_TENANT_ID, session_id=KEY_SESSION_ID, now=NOW)

    assert key == EXPECTED_KEY

    # The same window from its last second, and the next one from its first:
    # a builder keyed on the raw timestamp would answer three different keys.
    same_window = key_for(
        tenant_id=KEY_TENANT_ID,
        session_id=KEY_SESSION_ID,
        now=datetime(2026, 6, 2, 10, 19, 59, tzinfo=UTC),
    )
    next_window = key_for(
        tenant_id=KEY_TENANT_ID,
        session_id=KEY_SESSION_ID,
        now=datetime(2026, 6, 2, 10, 20, tzinfo=UTC),
    )
    assert same_window == EXPECTED_KEY
    assert next_window == f"ratelimit:{KEY_TENANT_ID}:{KEY_SESSION_ID}:{WINDOW_INDEX + 1}"


@pytest.mark.parametrize(
    "bad_session_id",
    [
        pytest.param("", id="empty"),
        pytest.param("5f7b1c2a-0000-4000-8000-000000000001\n", id="trailing-newline"),
        pytest.param("5f7b1c2a-0000-4000-8000-000000000001\r\n", id="trailing-crlf"),
        pytest.param("5f7b1c2a-0000-4000-8000-000000000001 ", id="trailing-space"),
        pytest.param("5F7B1C2A-0000-4000-8000-000000000001", id="uppercase"),
        pytest.param("ratelimit:x:y:1", id="colons-straddling-the-key-shape"),
    ],
)
def test_a_session_id_that_is_not_a_slug_is_refused_before_any_redis_call(
    bad_session_id: str,
) -> None:
    """Catches a limiter that builds its key from the session id and calls
    Redis first, checking the value afterwards or not at all: the client here
    raises on ANY attribute touched, so a single `INCR` fails the case outright
    rather than showing up as a key nothing ever reads again.

    The three trailing-whitespace rows are a project rule's: `$` in Python
    also matches before a trailing newline, so a limiter validating with an
    anchored `match` would count `sid` and `sid\\n` as two sessions and hand a
    client twice its budget.
    """
    poisoned = cast(Redis, PoisonedRedis())

    with pytest.raises(ToolError) as raised:
        asyncio.run(
            check_rate_limit(poisoned, tenant_id=KEY_TENANT_ID, session_id=bad_session_id, now=NOW)
        )

    assert raised.value.code is ErrorCode.VALIDATION


def test_the_session_id_bound_is_check_slugs_own_inclusive_bound() -> None:
    """Catches a limiter with a length gate of its own instead of `check_slug`'s.

    A local bound would drift from `checkpointer.thread_config`'s, and the two
    values — the one bounded before a checkpoint write and the one bounded
    before a counter — are the SAME session id (§1). Accepted at exactly 64,
    refused at 65, and 64 is `helpdesk.rag.naming.SLUG_LENGTH` rather than a
    number repeated here.
    """
    assert SLUG_LENGTH == 64

    at_the_bound = "a" * SLUG_LENGTH
    over_the_bound = "a" * (SLUG_LENGTH + 1)

    key = key_for(tenant_id=KEY_TENANT_ID, session_id=at_the_bound, now=NOW)
    assert key == f"ratelimit:{KEY_TENANT_ID}:{at_the_bound}:{WINDOW_INDEX}"

    with pytest.raises(ToolError) as raised:
        key_for(tenant_id=KEY_TENANT_ID, session_id=over_the_bound, now=NOW)
    assert raised.value.code is ErrorCode.VALIDATION


# --- the counter --------------------------------------------------------------


def test_the_thirtieth_message_passes_and_the_thirty_first_is_refused(
    redis_url: str, tenant_id: uuid.UUID, session_id: str
) -> None:
    """Catches an off-by-one on either side of the design's cap: a limiter
    refusing at 30 costs a session the message the spec grants it, and one
    admitting 31 is the runaway client the limit exists to bound.

    The cap is asserted as the literal 30 as well as driven, so a diff that
    widened `RATE_LIMIT_MESSAGES` and left the loop passing is red here.
    """
    assert RATE_LIMIT_MESSAGES == 30

    async def scenario() -> None:
        async with _client(redis_url) as client:
            for _ in range(RATE_LIMIT_MESSAGES):
                await check_rate_limit(client, tenant_id=tenant_id, session_id=session_id, now=NOW)
            with pytest.raises(RateLimitExceededError) as raised:
                await check_rate_limit(client, tenant_id=tenant_id, session_id=session_id, now=NOW)
            assert raised.value.code is ErrorCode.POLICY_BLOCKED

    asyncio.run(scenario())


def test_the_window_expires_after_exactly_six_hundred_seconds(
    redis_url: str, tenant_id: uuid.UUID, session_id: str
) -> None:
    """Catches a counter that never expires its key — the session would be
    barred for ever once it hit 30 — and one that re-sets the TTL on every
    message, which slides the window and bars a steady talker permanently.

    The 600 is read off the `EXPIRE` argument rather than off a TTL read back,
    because a TTL can only ever be bounded (599 or 600) and a project rule
    wants the value itself. The second window is reached by moving the clock,
    not by waiting: the key changes with the window index, which is what makes
    a fixed window cheap.
    """
    assert RATE_LIMIT_WINDOW_SECONDS == 600

    later = NOW + timedelta(seconds=RATE_LIMIT_WINDOW_SECONDS)

    async def scenario() -> None:
        async with _client(redis_url) as client:
            recorder = RecordingRedis(client)
            spy = cast(Redis, recorder)

            await check_rate_limit(spy, tenant_id=tenant_id, session_id=session_id, now=NOW)
            assert recorder.expire_arguments == [
                (key_for(tenant_id=tenant_id, session_id=session_id, now=NOW), 600)
            ]

            for _ in range(RATE_LIMIT_MESSAGES - 1):
                await check_rate_limit(spy, tenant_id=tenant_id, session_id=session_id, now=NOW)
            # Still exactly one EXPIRE: the first hit of the window set it.
            assert len(recorder.expire_arguments) == 1
            with pytest.raises(RateLimitExceededError):
                await check_rate_limit(spy, tenant_id=tenant_id, session_id=session_id, now=NOW)

            # The next window is a different key, so the same session gets its
            # full 30 again and the 31st is refused there too.
            for _ in range(RATE_LIMIT_MESSAGES):
                await check_rate_limit(spy, tenant_id=tenant_id, session_id=session_id, now=later)
            with pytest.raises(RateLimitExceededError):
                await check_rate_limit(spy, tenant_id=tenant_id, session_id=session_id, now=later)
            assert recorder.expire_arguments == [
                (key_for(tenant_id=tenant_id, session_id=session_id, now=NOW), 600),
                (key_for(tenant_id=tenant_id, session_id=session_id, now=later), 600),
            ]

    asyncio.run(scenario())


def test_an_unreachable_redis_refuses(tenant_id: uuid.UUID, session_id: str) -> None:
    """Catches decision C2 shipped by accident: a limiter that swallowed the
    `RedisError` and returned would let every session through unmetered for the
    length of a Redis outage, which is exactly when a runaway client is least
    visible.

    It also catches a limiter that lets the raw `redis.exceptions.RedisError`
    out — the chat route answers typed codes only, and a bare library exception
    there is FastAPI's untyped 500 with a stack trace.

    No `redis_url` fixture: this client points at a port nothing listens on.
    """

    async def scenario() -> None:
        client: Redis = Redis.from_url(
            f"redis://127.0.0.1:{_unused_port()}/0",
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        try:
            with pytest.raises(RateLimitUnavailableError) as raised:
                await check_rate_limit(client, tenant_id=tenant_id, session_id=session_id, now=NOW)
            assert raised.value.code is ErrorCode.INTERNAL
        finally:
            await client.aclose()

    asyncio.run(scenario())
