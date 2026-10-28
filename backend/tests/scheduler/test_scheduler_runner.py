"""The scheduler's job registry, its cadence-gate lock and its tick.

Written before the implementation exists. Expected surface (the implementer
must provide exactly this):

``scheduler.runner``
    ``TICK_SECONDS = 60`` — how often the loop wakes.
    ``EXPIRE_GRANTS_CADENCE_SECONDS = 300`` — the product spec five minutes, and the
        `PX` of the lock key.
    ``JOB_LOCK_PREFIX = "helpdesk:joblock:"``
    ``JOB_NAME_MAX_LENGTH = 64`` · ``JOB_NAME_PATTERN = r"[a-z][a-z0-9_]*"``
    ``def job_lock_key(name: str) -> str`` — ``JOB_LOCK_PREFIX + name``.
    ``def check_job_name(name: str) -> str`` — the accepted name, or a
        ``ToolError`` whose ``code`` is ``ErrorCode.VALIDATION``. Matched with
        ``re.fullmatch``, length-capped against
        ``JOB_NAME_MAX_LENGTH`` (the name becomes a Redis key
        segment, and Redis has no length backstop of its own).
    ``class Job`` — a frozen dataclass carrying ``name``, ``cadence_seconds``
        and ``run``, an ``async (session, now) -> None`` called once per
        tenant with a `tenant_session`-bound session.
    ``REGISTRY: dict[str, Job]`` — the jobs this service runs; at first it registers
        exactly ``expire_grants``.
    ``async def tick(factory, redis, *, now) -> None`` — for every job in
        ``REGISTRY``, claim the cadence gate with
        ``SET <key> <token> NX PX <cadence*1000>`` and, if claimed, run the
        job once per tenant. A job that raises has its key DELETED
 and stops neither the tick nor the other jobs; one
        tenant raising does not stop the other tenants.
    ``async def run_loop() -> None`` — the lifespan task: ``tick`` every
        ``TICK_SECONDS``.

``helpdesk.shared.core.redis``
    ``REDIS_TIMEOUT_SECONDS = 2`` · ``def get_redis(settings) -> Redis``
    (both halves asserted on the CONSTRUCTED client) ·
    ``async def close_loop_clients() -> None``.

One database and one dedicated Redis index for the whole
run, both built once. Per-test isolation is a namespace: every case that
needs a lock key owns a uuid-suffixed JOB NAME, so its key
(`helpdesk:joblock:<name>`) cannot collide with another case's — the one
exception is the case that pins the literal `helpdesk:joblock:expire_grants`,
which deletes exactly that key first.

This file carries the service in its name, and no basename
in it collides with an existing test file.

No pytest-asyncio: each case is a sync function driving one `asyncio.run`.
"""

import asyncio
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest
from prometheus_client import CollectorRegistry
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from helpdesk.scheduler.runner import (
    EXPIRE_GRANTS_CADENCE_SECONDS,
    JOB_LOCK_PREFIX,
    JOB_NAME_MAX_LENGTH,
    JOB_NAME_PATTERN,
    REGISTRY,
    TICK_SECONDS,
    Job,
    check_job_name,
    job_lock_key,
    tenant_lock_key,
    tick,
)
from helpdesk.shared.core.config import load_settings
from helpdesk.shared.core.db import bound_tenant_id, create_engine, create_session_factory
from helpdesk.shared.core.observability import metrics as metrics_module
from helpdesk.shared.core.observability.metrics import build_metrics
from helpdesk.shared.core.redis import REDIS_TIMEOUT_SECONDS, close_loop_clients, get_redis
from helpdesk.shared.crud import Repository
from helpdesk.shared.models import Base, Ticket
from tests.support.seed import SeededTenant, seed_tenant

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)

# This suite's dedicated Redis database index. 15/14 belong to the helpdesk
# suite and 12/11 to mcp-server, so this pair cannot collide with either.
TEST_REDIS_DB = 10
_FALLBACK_TEST_REDIS_DB = 9


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

_PG_UNREACHABLE_MSG = (
    "Postgres is unreachable at the configured DATABASE_URL.\n"
    "Scheduler tests require the compose stack: run `docker compose up -d` first.\n"
    "Original error: {error!r}"
)
_REDIS_UNREACHABLE_MSG = (
    "Redis is unreachable at the configured REDIS_URL.\n"
    "Scheduler tests require the compose stack: run `docker compose up -d` first.\n"
    "Original error: {error!r}"
)


# --- backing services, built once ---------------------------


async def _admin_execute(admin_url: str, sql: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def scheduler_db() -> Iterator[tuple[str, list[SeededTenant]]]:
    """One throwaway database with TWO seeded tenants, created once.

    Two, because the per-tenant claim of the tick is only observable when
    there is more than one tenant to visit.
    """
    try:
        settings = load_settings()
    except ValidationError as exc:
        pytest.fail(
            f"DATABASE_URL is not configured (env or .env). Copy .env.example to .env.\n{exc}",
            pytrace=False,
        )
    try:
        asyncio.run(_admin_execute(settings.database_url, "SELECT 1"))
    except Exception as exc:
        pytest.fail(_PG_UNREACHABLE_MSG.format(error=exc), pytrace=False)

    name = f"helpdesk_test_scheduler_{uuid.uuid4().hex[:12]}"
    asyncio.run(_admin_execute(settings.database_url, f'CREATE DATABASE "{name}"'))
    url = make_url(settings.database_url).set(database=name).render_as_string(hide_password=False)

    async def prepare() -> list[SeededTenant]:
        engine = create_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            factory = create_session_factory(engine)
            return [
                await seed_tenant(factory, "scheduler-tenant-a"),
                await seed_tenant(factory, "scheduler-tenant-b"),
            ]
        finally:
            await engine.dispose()

    tenants = asyncio.run(prepare())
    yield url, tenants
    asyncio.run(_admin_execute(settings.database_url, f'DROP DATABASE "{name}" WITH (FORCE)'))


def _redis_url_for_db(url: str, index: int) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{index}", parts.query, parts.fragment))


def _redis_db_index(url: str) -> int:
    path = urlsplit(url).path.lstrip("/")
    return int(path) if path.isdigit() else 0


@pytest.fixture(scope="session")
def scheduler_redis_url() -> str:
    """Connectivity gate for a dedicated Redis index; fails loudly, never skips.

    Nothing here FLUSHDBs: per-test isolation is the job-name namespace below,
    so the index is only ever written under `helpdesk:joblock:` keys this
    suite owns. The host check stays anyway — a scheduler suite pointed at a
    shared Redis would take real locks.
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
            f"REDIS_URL points at {host!r}, not this machine. The scheduler tests take "
            "job locks and will not do that against a remote or shared Redis.",
            pytrace=False,
        )
    configured = _redis_db_index(settings.redis_url)
    index = _worker_redis_index(TEST_REDIS_DB, _FALLBACK_TEST_REDIS_DB, configured)
    url = _redis_url_for_db(settings.redis_url, index)

    async def ping() -> None:
        client: Redis = Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
        try:
            await client.ping()
        finally:
            await client.aclose()

    try:
        asyncio.run(ping())
    except Exception as exc:
        pytest.fail(_REDIS_UNREACHABLE_MSG.format(error=exc), pytrace=False)
    return url


@pytest.fixture
def job_name() -> str:
    """A job name of this case's own: the per-test key namespace.

    Lower-case letters and digits only, so it is a name `check_job_name` must
    accept — a fixture that produced an invalid name would make every case
    using it fail for the fixture's reason instead of the code's.
    """
    return f"job_{uuid.uuid4().hex[:12]}"


# --- driving the tick ---------------------------------------------------------


def _run_tick(
    database_url: str,
    redis_url: str,
    jobs: dict[str, Job],
    *,
    times: int = 1,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, int | None]:
    """Run `tick` `times` times over `jobs`; return each job's key TTL after.

    A TTL of `None` means the key is gone (Redis answers -2 for a missing key
    and -1 for a key with no expiry; both are distinguished below).
    """
    monkeypatch.setattr("helpdesk.scheduler.runner.REGISTRY", jobs)

    async def scenario() -> dict[str, int | None]:
        engine = create_engine(database_url)
        client: Redis = Redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
        try:
            factory = create_session_factory(engine)
            for _ in range(times):
                await tick(factory, client, now=NOW)
            ttls: dict[str, int | None] = {}
            for name in jobs:
                ttl = int(await client.ttl(job_lock_key(name)))
                ttls[name] = None if ttl == -2 else ttl
            return ttls
        finally:
            await client.aclose()
            await engine.dispose()

    return asyncio.run(scenario())


def _delete_key(redis_url: str, key: str) -> None:
    async def scenario() -> None:
        client: Redis = Redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
        try:
            await client.delete(key)
        finally:
            await client.aclose()

    asyncio.run(scenario())


def _get_key_ttl(redis_url: str, key: str) -> int | None:
    """TTL of a specific Redis key; None if the key is gone.

    Redis answers -2 for a missing key and -1 for a key with no expiry.
    Both are treated as None here: a key with no expiry is a lock that
    never opens, which is as wrong as one that was deleted.
    """

    async def scenario() -> int | None:
        client: Redis = Redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
        try:
            raw = int(await client.ttl(key))
            return None if raw < 0 else raw
        finally:
            await client.aclose()

    return asyncio.run(scenario())


def _tickets(database_url: str, tenant_id: uuid.UUID) -> int:
    """How many `tickets` rows this tenant holds, read outside the tick."""

    async def scenario() -> int:
        engine = create_engine(database_url)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(
                    text("SELECT count(*) FROM tickets WHERE tenant_id = :tenant"),
                    {"tenant": tenant_id},
                )
                return int(result.scalar_one())
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _recording_job(name: str, seen: list[uuid.UUID]) -> Job:
    """A job that records the tenant its session is bound to."""

    async def run(session: AsyncSession, now: datetime) -> None:
        seen.append(bound_tenant_id(session))

    return Job(name=name, cadence_seconds=EXPIRE_GRANTS_CADENCE_SECONDS, run=run)


# --- the constants -----------------------------------------


def test_the_runner_constants_are_exactly_the_expected_values() -> None:
    """Catches a cadence, a tick or a key prefix quietly changed. Every one of
    them is a number the rest of this file asserts against: a tick of 600 with
    a cadence of 300 runs each job twice as rarely as the product spec asks, and a
    changed prefix silently orphans every lock a running instance holds while
    the new one takes fresh ones."""
    assert TICK_SECONDS == 60
    assert EXPIRE_GRANTS_CADENCE_SECONDS == 300
    assert JOB_LOCK_PREFIX == "helpdesk:joblock:"
    assert JOB_NAME_MAX_LENGTH == 64
    assert JOB_NAME_PATTERN == r"[a-z][a-z0-9_]*"
    assert job_lock_key("expire_grants") == "helpdesk:joblock:expire_grants"


def test_the_registry_holds_expire_grants_at_the_planned_cadence() -> None:
    """Catches a job registered under a name that is not the one the product spec and
    every lock key in this file spell, and a job registered with its own
    cadence literal rather than the constant — the key's TTL IS the cadence
    gate, so a job whose declared cadence and whose TTL disagree either runs
    twice per window or never runs at all."""
    assert "expire_grants" in REGISTRY
    job = REGISTRY["expire_grants"]
    assert job.name == "expire_grants"
    assert job.cadence_seconds == EXPIRE_GRANTS_CADENCE_SECONDS
    for name, registered in REGISTRY.items():
        assert check_job_name(name) == name
        assert registered.name == name


# --- job names ----------------------------------


def test_a_job_name_of_exactly_the_cap_is_accepted() -> None:
    """The inclusive half of the boundary. The cap is the narrower of the two
    gates (the pattern admits any length), so this is where the boundary
    belongs. Catches an off-by-one `>=` that refuses the
    longest name the service is declared to allow."""
    at_cap = "a" * JOB_NAME_MAX_LENGTH

    assert check_job_name(at_cap) == at_cap


def test_a_job_name_one_character_over_the_cap_is_refused() -> None:
    """The exclusive half: the name becomes a Redis key segment, and Redis
    has no length backstop of its own, so nothing else in
    the system would ever refuse it — an unbounded name is simply stored as
    an unbounded key."""
    with pytest.raises(Exception) as excinfo:
        check_job_name("a" * (JOB_NAME_MAX_LENGTH + 1))

    assert getattr(excinfo.value, "code", None) is not None, "must be a typed ToolError"


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("expire_grants\n", id="trailing-newline"),
        pytest.param("expire_grants\r\n", id="trailing-crlf"),
        pytest.param("expire_grants ", id="trailing-space"),
        pytest.param("\nexpire_grants", id="leading-newline"),
        pytest.param("", id="empty"),
        pytest.param("1expire", id="leading-digit"),
        pytest.param("Expire_Grants", id="upper-case"),
        pytest.param("expire-grants", id="hyphen"),
        pytest.param("expire:grants", id="key-separator"),
    ],
)
def test_the_job_name_pattern_refuses_these(name: str) -> None:
    """The rejection table. `re.match` and a Pydantic
    `Field(pattern=...)` both anchor only at the start, so all three of the
    trailing-whitespace rows are ACCEPTED by them and only `re.fullmatch`
    refuses: a name carrying a newline becomes a Redis key carrying a
    newline, which no operator inspecting `KEYS helpdesk:joblock:*` can tell
    apart from the real one — two jobs, one apparent lock. The `:` row is the
    same hazard one level up: a name may not invent a key segment."""
    with pytest.raises(Exception) as excinfo:
        check_job_name(name)

    assert getattr(excinfo.value, "code", None) is not None, "must be a typed ToolError"


# --- the cadence gate -----------------------------


def test_a_due_job_leaves_a_lock_key_per_tenant_with_the_cadence_ttl(
    scheduler_db: tuple[str, list[SeededTenant]],
    scheduler_redis_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The REAL registry, so the literal per-tenant key names
    and the literal TTL are pinned together.

    Catches a runner that still takes one lock per job instead of one per
    (job, tenant): the old single key `helpdesk:joblock:expire_grants`
    would be present while both per-tenant keys are missing, and an
    operator watching `KEYS helpdesk:joblock:*` would see one entry instead
    of two — a runner that cannot tell one tenant's gate from another's.
    Also catches a lock taken with no expiry (the job would never run
    again after one tick) and a lock released at the end of the run
    (the key itself is the cadence gate, so a released key
    means the job re-runs on the next tick).
    """
    database_url, tenants = scheduler_db
    for tenant in tenants:
        _delete_key(
            scheduler_redis_url,
            tenant_lock_key("expire_grants", tenant.tenant_id),
        )

    _run_tick(database_url, scheduler_redis_url, dict(REGISTRY), monkeypatch=monkeypatch)

    for tenant in tenants:
        key = tenant_lock_key("expire_grants", tenant.tenant_id)
        ttl = _get_key_ttl(scheduler_redis_url, key)
        assert ttl == EXPIRE_GRANTS_CADENCE_SECONDS, f"expected TTL 300 for {key}, got {ttl}"
        assert ttl == 300


def test_a_second_tick_in_the_same_window_does_not_run_the_job(
    scheduler_db: tuple[str, list[SeededTenant]],
    scheduler_redis_url: str,
    job_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `NX` half of the cadence gate. Two ticks inside one cadence window: the
    second must find the key already set and do nothing.

    Catches a plain `SET` (which succeeds every time and re-arms the TTL, so
    the job runs on every 60-second tick instead of every 300), and a gate
    that checks `EXISTS` before setting — the read and the write would not be
    one operation, which is the whole reason the code spells `SET NX PX`.
    Counted per tenant: with two seeded tenants, one claimed window is two
    runs, and two claimed windows would be four.
    """
    database_url, tenants = scheduler_db
    seen: list[uuid.UUID] = []
    _delete_key(scheduler_redis_url, job_lock_key(job_name))

    _run_tick(
        database_url,
        scheduler_redis_url,
        {job_name: _recording_job(job_name, seen)},
        times=2,
        monkeypatch=monkeypatch,
    )

    assert sorted(seen) == sorted(tenant.tenant_id for tenant in tenants)


def test_the_tick_opens_one_tenant_session_per_tenant(
    scheduler_db: tuple[str, list[SeededTenant]],
    scheduler_redis_url: str,
    job_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a tick that runs each job once against an UNBOUND session:
    every job in this service reads and writes tenant-scoped tables, and an
    unbound session raises `UnboundTenantQueryError` on the first query —
    which the error handling would then swallow silently, so the
    sweep would appear to run for ever while expiring nothing. Also catches a
    tick that visits the first tenant only."""
    database_url, tenants = scheduler_db
    seen: list[uuid.UUID] = []
    _delete_key(scheduler_redis_url, job_lock_key(job_name))

    _run_tick(
        database_url,
        scheduler_redis_url,
        {job_name: _recording_job(job_name, seen)},
        monkeypatch=monkeypatch,
    )

    assert sorted(seen) == sorted(tenant.tenant_id for tenant in tenants)
    assert len(seen) == len(set(seen)), "one session per tenant, not one per job per retry"


def test_a_job_that_raises_has_its_key_deleted_and_leaves_the_other_job_running(
    scheduler_db: tuple[str, list[SeededTenant]],
    scheduler_redis_url: str,
    job_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both halves in one case, now checked per-tenant key.

    The key deletion is what turns a transient Postgres blip into a
    60-second delay instead of a 300-second one: catches a runner that
    leaves the per-tenant key (the failed tenant waits a full cadence) and
    one that lets the exception out of the tick (the loop dies, or the
    second job never runs at all). The healthy job's per-tenant keys are
    asserted to SURVIVE, so a runner that deletes every key after every run
    fails here rather than passing as "deletes the failed ones".
    """
    database_url, tenants = scheduler_db
    raiser = f"{job_name}_raiser"
    seen: list[uuid.UUID] = []

    async def boom(session: AsyncSession, now: datetime) -> None:
        raise RuntimeError("the job failed, as this case needs it to")

    for name in (raiser, job_name):
        for tenant in tenants:
            _delete_key(scheduler_redis_url, tenant_lock_key(name, tenant.tenant_id))

    _run_tick(
        database_url,
        scheduler_redis_url,
        {
            raiser: Job(name=raiser, cadence_seconds=EXPIRE_GRANTS_CADENCE_SECONDS, run=boom),
            job_name: _recording_job(job_name, seen),
        },
        monkeypatch=monkeypatch,
    )

    for tenant in tenants:
        raiser_key = tenant_lock_key(raiser, tenant.tenant_id)
        assert _get_key_ttl(scheduler_redis_url, raiser_key) is None, (
            f"failed job's per-tenant key must be deleted, got TTL for {raiser_key}"
        )
        healthy_key = tenant_lock_key(job_name, tenant.tenant_id)
        assert _get_key_ttl(scheduler_redis_url, healthy_key) == EXPIRE_GRANTS_CADENCE_SECONDS, (
            f"healthy job's per-tenant key must survive with TTL 300, got None for {healthy_key}"
        )
    assert seen, "the other job in the registry still ran"


def test_one_tenant_raising_leaves_the_other_tenants_work_committed(
    scheduler_db: tuple[str, list[SeededTenant]],
    scheduler_redis_url: str,
    job_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second half. One tenant's data can be broken in a way
    another's is not, and a sweep that gives up on the first failure would
    leave every later tenant's grants unexpired indefinitely, with nothing but
    a log line to say so.

    The job writes a row per tenant and raises for the FIRST tenant it is
    given, so a tick that shares one transaction across tenants — or that
    stops at the first exception — leaves the other tenant's row missing.
    Counted before and after, because these tenants already carry a seeded
    ticket each.
    """
    database_url, tenants = scheduler_db
    failing, surviving = tenants[0], tenants[1]
    before = {tenant.tenant_id: _tickets(database_url, tenant.tenant_id) for tenant in tenants}
    users = {tenant.tenant_id: tenant.user_id for tenant in tenants}

    async def run(session: AsyncSession, now: datetime) -> None:
        tenant_id = bound_tenant_id(session)
        await Repository(session, Ticket).create(
            user_id=users[tenant_id],
            kind="general",
            title=f"fake scheduler ticket {job_name}",
            body="written by the scheduler tick test",
            status="open",
            session_id=None,
        )
        if tenant_id == failing.tenant_id:
            raise RuntimeError("this tenant's job failed")

    _delete_key(scheduler_redis_url, job_lock_key(job_name))

    _run_tick(
        database_url,
        scheduler_redis_url,
        {job_name: Job(name=job_name, cadence_seconds=EXPIRE_GRANTS_CADENCE_SECONDS, run=run)},
        monkeypatch=monkeypatch,
    )

    assert _tickets(database_url, surviving.tenant_id) == before[surviving.tenant_id] + 1
    assert _tickets(database_url, failing.tenant_id) == before[failing.tenant_id]


# --- the Redis client -----------------------------


def test_the_redis_client_carries_both_timeout_halves(
    scheduler_redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asserted on the CONSTRUCTED client, not on the constant:
    a test that only reads `REDIS_TIMEOUT_SECONDS` passes with every
    timeout argument deleted from the module.

    Both halves, because a connect timeout alone leaves a completed handshake
    free to hang on the read — and a blackholed Redis would then hold the
    scheduler's only loop for ever, so no job of any tenant ever runs again
    and nothing reports it, since the loop is not what `/readyz` reads.
    """
    monkeypatch.setenv("REDIS_URL", scheduler_redis_url)

    async def scenario() -> dict[str, Any]:
        try:
            client: Redis = get_redis(load_settings())
            kwargs: dict[str, Any] = client.connection_pool.connection_kwargs
            return dict(kwargs)
        finally:
            await close_loop_clients()

    kwargs = asyncio.run(scenario())

    assert REDIS_TIMEOUT_SECONDS == 2
    assert kwargs["socket_connect_timeout"] == 2
    assert kwargs["socket_timeout"] == 2


# --- per-tenant lock key -------------------------------------------


def test_tenant_lock_key_is_the_job_key_then_the_tenant_uuid() -> None:
    """Catches an implementation that omits the tenant segment, returns the
    plain job key, or swaps the job and tenant segments — all three produce a
    key that cannot distinguish one tenant's gate from another's, so the first
    tenant to claim it blocks every other tenant for the full cadence.
    """
    tid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    expected = f"helpdesk:joblock:expire_grants:{tid}"

    result = tenant_lock_key("expire_grants", tid)

    assert result == expected


def test_a_failed_tenant_frees_only_its_own_lock_and_keeps_the_others(
    scheduler_db: tuple[str, list[SeededTenant]],
    scheduler_redis_url: str,
    job_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a runner that on any tenant failure deletes the whole-job key
    instead of the per-tenant key — the other tenant's gate would then also
    be freed, so it retries on the next tick even though it succeeded.

    Two tenants: the job raises for tenant A (matched by bound_tenant_id)
    and succeeds for tenant B. After one tick, tenant A's per-tenant key is
    gone and tenant B's per-tenant key has the cadence TTL.
    """
    database_url, tenants = scheduler_db
    tenant_a, tenant_b = tenants[0], tenants[1]

    for tenant in tenants:
        _delete_key(scheduler_redis_url, tenant_lock_key(job_name, tenant.tenant_id))

    async def boom_for_a(session: AsyncSession, now: datetime) -> None:
        if bound_tenant_id(session) == tenant_a.tenant_id:
            raise RuntimeError("tenant A's job failed")

    monkeypatch.setattr(
        "helpdesk.scheduler.runner.REGISTRY",
        {
            job_name: Job(
                name=job_name, cadence_seconds=EXPIRE_GRANTS_CADENCE_SECONDS, run=boom_for_a
            )
        },
    )

    async def scenario() -> None:
        engine = create_engine(database_url)
        client: Redis = Redis.from_url(
            scheduler_redis_url, socket_connect_timeout=2, socket_timeout=2
        )
        try:
            factory = create_session_factory(engine)
            await tick(factory, client, now=NOW)
        finally:
            await client.aclose()
            await engine.dispose()

    asyncio.run(scenario())

    key_a = tenant_lock_key(job_name, tenant_a.tenant_id)
    key_b = tenant_lock_key(job_name, tenant_b.tenant_id)
    assert _get_key_ttl(scheduler_redis_url, key_a) is None, (
        "failed tenant's per-tenant key must be deleted"
    )
    assert _get_key_ttl(scheduler_redis_url, key_b) == EXPIRE_GRANTS_CADENCE_SECONDS, (
        "succeeding tenant's per-tenant key must survive with the cadence TTL"
    )


def test_the_next_tick_reruns_only_the_tenant_that_failed(
    scheduler_db: tuple[str, list[SeededTenant]],
    scheduler_redis_url: str,
    job_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a runner that either re-runs both tenants on the second tick
    (because it freed both keys) or skips tenant A on the second tick
    (because it left the broken key in place).

    After a first tick where only tenant A fails, a second tick must visit
    only tenant A — tenant B's key still has its TTL so the second tick
    skips it.
    """
    database_url, tenants = scheduler_db
    tenant_a, tenant_b = tenants[0], tenants[1]

    for tenant in tenants:
        _delete_key(scheduler_redis_url, tenant_lock_key(job_name, tenant.tenant_id))

    tick_count: dict[uuid.UUID, int] = {tenant_a.tenant_id: 0, tenant_b.tenant_id: 0}

    async def counting_run(session: AsyncSession, now: datetime) -> None:
        tid = bound_tenant_id(session)
        tick_count[tid] = tick_count.get(tid, 0) + 1
        if tid == tenant_a.tenant_id and tick_count[tid] == 1:
            raise RuntimeError("tenant A fails on first tick")

    monkeypatch.setattr(
        "helpdesk.scheduler.runner.REGISTRY",
        {
            job_name: Job(
                name=job_name, cadence_seconds=EXPIRE_GRANTS_CADENCE_SECONDS, run=counting_run
            )
        },
    )

    async def scenario() -> None:
        engine = create_engine(database_url)
        client: Redis = Redis.from_url(
            scheduler_redis_url, socket_connect_timeout=2, socket_timeout=2
        )
        try:
            factory = create_session_factory(engine)
            await tick(factory, client, now=NOW)
            await tick(factory, client, now=NOW)
        finally:
            await client.aclose()
            await engine.dispose()

    asyncio.run(scenario())

    assert tick_count[tenant_a.tenant_id] == 2, (
        "tenant A must be visited twice: once on first tick (failed), once on second tick"
    )
    assert tick_count[tenant_b.tenant_id] == 1, (
        "tenant B must be visited only once: its key survived the first tick so the second skips it"
    )


def test_a_run_cancelled_midway_frees_its_lock(
    scheduler_db: tuple[str, list[SeededTenant]],
    scheduler_redis_url: str,
    job_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a runner whose finally block is absent or swallows CancelledError
    before re-raising — the tenant's per-tenant key would remain set, so the
    scheduler would never retry that tenant until the cadence window expired,
    and the orchestrator-requested shutdown would appear to hang at the task.

    The job raises CancelledError for its one tenant. The per-tenant key must
    be deleted AND the CancelledError must propagate out of tick.
    """
    database_url, tenants = scheduler_db
    tenant = tenants[0]

    _delete_key(scheduler_redis_url, tenant_lock_key(job_name, tenant.tenant_id))

    async def cancel_me(session: AsyncSession, now: datetime) -> None:
        raise asyncio.CancelledError("cancelled mid-run")

    monkeypatch.setattr(
        "helpdesk.scheduler.runner.REGISTRY",
        {
            job_name: Job(
                name=job_name, cadence_seconds=EXPIRE_GRANTS_CADENCE_SECONDS, run=cancel_me
            )
        },
    )

    async def scenario() -> None:
        engine = create_engine(database_url)
        client: Redis = Redis.from_url(
            scheduler_redis_url, socket_connect_timeout=2, socket_timeout=2
        )
        try:
            factory = create_session_factory(engine)
            with pytest.raises(asyncio.CancelledError):
                await tick(factory, client, now=NOW)
        finally:
            await client.aclose()
            await engine.dispose()

    asyncio.run(scenario())

    key = tenant_lock_key(job_name, tenant.tenant_id)
    assert _get_key_ttl(scheduler_redis_url, key) is None, (
        "CancelledError must free the per-tenant key before propagating"
    )


# --- Prometheus counters -------------------------------------------


def test_the_job_run_counter_rises_once_per_tenant(
    scheduler_db: tuple[str, list[SeededTenant]],
    scheduler_redis_url: str,
    job_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a runner that increments the run counter once per job instead
    of once per tenant — two tenants visited would then read 1 instead of 2,
    making the counter useless for capacity planning by tenant count.

    A fresh Prometheus registry is monkeypatched over the module-level pair
    so the assertion is an absolute value, not a delta against whatever prior
    tests incremented.
    """
    database_url, tenants = scheduler_db

    fresh_registry = CollectorRegistry()
    fresh_metrics = build_metrics(fresh_registry)
    monkeypatch.setattr(metrics_module, "REGISTRY", fresh_registry)
    monkeypatch.setattr(metrics_module, "METRICS", fresh_metrics)

    for tenant in tenants:
        _delete_key(scheduler_redis_url, tenant_lock_key(job_name, tenant.tenant_id))

    async def noop(session: AsyncSession, now: datetime) -> None:
        pass

    monkeypatch.setattr(
        "helpdesk.scheduler.runner.REGISTRY",
        {job_name: Job(name=job_name, cadence_seconds=EXPIRE_GRANTS_CADENCE_SECONDS, run=noop)},
    )

    async def scenario() -> None:
        engine = create_engine(database_url)
        client: Redis = Redis.from_url(
            scheduler_redis_url, socket_connect_timeout=2, socket_timeout=2
        )
        try:
            factory = create_session_factory(engine)
            await tick(factory, client, now=NOW)
        finally:
            await client.aclose()
            await engine.dispose()

    asyncio.run(scenario())

    value = fresh_registry.get_sample_value("helpdesk_job_runs_total", {"job": job_name})
    assert value == 2.0, (
        f"expected helpdesk_job_runs_total{{job={job_name!r}}} == 2.0 (one per tenant), got {value}"
    )


def test_the_job_failure_counter_rises_once_per_failed_tenant(
    scheduler_db: tuple[str, list[SeededTenant]],
    scheduler_redis_url: str,
    job_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a runner that either does not increment job_failures_total at
    all, increments it once per job instead of per tenant, or increments it
    even for tenants that succeeded.

    Two tenants: the job raises for tenant A only. After one tick,
    job_runs_total must be 2 (both tenants attempted) and
    job_failures_total must be 1 (only tenant A failed).
    """
    database_url, tenants = scheduler_db
    tenant_a = tenants[0]

    fresh_registry = CollectorRegistry()
    fresh_metrics = build_metrics(fresh_registry)
    monkeypatch.setattr(metrics_module, "REGISTRY", fresh_registry)
    monkeypatch.setattr(metrics_module, "METRICS", fresh_metrics)

    for tenant in tenants:
        _delete_key(scheduler_redis_url, tenant_lock_key(job_name, tenant.tenant_id))

    async def fail_a(session: AsyncSession, now: datetime) -> None:
        if bound_tenant_id(session) == tenant_a.tenant_id:
            raise RuntimeError("tenant A fails")

    monkeypatch.setattr(
        "helpdesk.scheduler.runner.REGISTRY",
        {job_name: Job(name=job_name, cadence_seconds=EXPIRE_GRANTS_CADENCE_SECONDS, run=fail_a)},
    )

    async def scenario() -> None:
        engine = create_engine(database_url)
        client: Redis = Redis.from_url(
            scheduler_redis_url, socket_connect_timeout=2, socket_timeout=2
        )
        try:
            factory = create_session_factory(engine)
            await tick(factory, client, now=NOW)
        finally:
            await client.aclose()
            await engine.dispose()

    asyncio.run(scenario())

    runs = fresh_registry.get_sample_value("helpdesk_job_runs_total", {"job": job_name})
    failures = fresh_registry.get_sample_value("helpdesk_job_failures_total", {"job": job_name})
    assert runs == 2.0, f"expected job_runs_total == 2.0, got {runs}"
    assert failures == 1.0, f"expected job_failures_total == 1.0, got {failures}"
