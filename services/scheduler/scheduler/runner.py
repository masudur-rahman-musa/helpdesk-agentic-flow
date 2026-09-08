"""The loop, the job registry, and the Redis lock that IS the cadence gate.

An earlier change decision A, recorded as a design note: a plain asyncio loop with Redis
locks rather than APScheduler 4. APScheduler 4 is still pre-release and brings
a job store whose failure modes we would own; this is fifty lines, and every
job stays callable straight from a test with a session and a fixed `now`.

Decision B is the shape worth reading twice. The lock is not a lease held for
the duration of a run with a separate record of when the job last ran — it IS
the cadence gate: `SET <key> <token> NX PX <cadence>`, and the key is never
released. One key answers both questions a scheduler has ("is this job due"
and "is another instance already on it"), so two runners in one window run the
job once, and the next window opens when Redis expires the key. The trade is
written down rather than discovered: a job cannot be forced early, and a run
that outlives its own cadence can overlap the next one — harmless for
`expire_grants`, whose row lock and `status = 'active'` test make the second
run a no-op.

Decision C is the other half: a job that RAISES has its key deleted, so a
transient Postgres blip costs one tick (60 seconds) instead of a full cadence
(300). A job that merely fails for one tenant still counts as raising for this
purpose; the other tenants' work is already committed by then, because each
tenant gets its own transaction.

The tick never lets an exception out. It is a lifespan task: an exception
escaping it kills the loop, and a scheduler whose loop has quietly died still
answers `/healthz` and `/readyz` and expires nothing for ever.
"""

import asyncio
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from helpdesk.core.db import (
    create_session_factory,
    dispose_shared_engines,
    shared_engine,
    tenant_session,
)
from helpdesk.core.repository import list_tenant_ids
from helpdesk.core.settings import load_settings
from helpdesk.errors import ErrorCode, ToolError
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scheduler import jobs
from scheduler.redis_client import close_loop_clients, get_redis

logger = logging.getLogger(__name__)

TICK_SECONDS = 60
"""How often the loop wakes to ask which jobs are due.

The floor on how soon a job retries after a failure, and on how late a job can
be relative to its cadence. Pinned exactly by
`test_scheduler_runner.py::test_the_runner_constants_are_exactly_the_planned_values`."""

EXPIRE_GRANTS_CADENCE_SECONDS = 300
"""the design's five minutes, and the `PX` of the expiry sweep's lock key.

The cadence and the TTL are ONE number by construction — the key's expiry is
what makes the next window open — so a job whose declared cadence and whose
lock TTL disagreed would either run every tick or never again. Pinned by the
constants case and, on the real registry, by
`::test_a_due_job_runs_and_leaves_its_lock_key_with_the_cadence_ttl`."""

JOB_LOCK_PREFIX = "helpdesk:joblock:"
"""The namespace every job lock lives under, so an operator can see them all
with one `KEYS helpdesk:joblock:*`. Changing it orphans every lock a running
instance holds while the new one takes fresh ones."""

JOB_NAME_MAX_LENGTH = 64
"""The cap on a job name, checked before it becomes part of a Redis key.

Redis has no length backstop of its own: an unbounded name is simply stored
as an unbounded key. The narrower of the two gates — the
pattern admits any length — so the inclusive boundary is written against this
one, by
`::test_a_job_name_of_exactly_the_cap_is_accepted` and
`::test_a_job_name_one_character_over_the_cap_is_refused`."""

JOB_NAME_PATTERN = r"[a-z][a-z0-9_]*"
"""What a job name may contain, matched with `re.fullmatch`.

`re.match` and a Pydantic `Field(pattern=...)` anchor only at the start, so
both accept `"expire_grants\\n"` — a Redis key carrying a newline, which no
operator reading `KEYS` output can tell from the real one: two jobs, one
apparent lock. `:` is excluded for the level-up version of the same hazard, a
name inventing a key segment. The rejection table is
`::test_the_job_name_pattern_refuses_these`."""

_JOB_NAME_RE = re.compile(JOB_NAME_PATTERN)


class InvalidJobNameError(ToolError):
    """A job name that cannot safely become a Redis key segment.

    `VALIDATION`, the the design code for an input that has to be fixed rather
    than retried. Raised before any key is touched; pinned as a typed error by
    the boundary and rejection cases in `test_scheduler_runner.py`.
    """

    code = ErrorCode.VALIDATION


def check_job_name(name: str) -> str:
    """The accepted job name, or a typed refusal.

    The length first and the shape second, so an enormous string is refused
    without being run through a regex.
    """
    if len(name) > JOB_NAME_MAX_LENGTH:
        raise InvalidJobNameError(
            f"a job name longer than {JOB_NAME_MAX_LENGTH} characters cannot be "
            f"a lock key: {len(name)}"
        )
    if _JOB_NAME_RE.fullmatch(name) is None:
        raise InvalidJobNameError(f"{name!r} is not a job name: it must match {JOB_NAME_PATTERN}")
    return name


def job_lock_key(name: str) -> str:
    """The Redis key that is this job's cadence gate."""
    return f"{JOB_LOCK_PREFIX}{name}"


JobRun = Callable[[AsyncSession, datetime], Awaitable[None]]


@dataclass(frozen=True)
class Job:
    """One periodic job: its name, how often it runs, and what it does.

    `run` is called once per tenant with a session already bound to that
    tenant and the instant the whole tick is answering for.
    """

    name: str
    cadence_seconds: int
    run: JobRun


EXPIRE_GRANTS_JOB = Job(
    name=check_job_name(jobs.EXPIRE_GRANTS_JOB_NAME),
    cadence_seconds=EXPIRE_GRANTS_CADENCE_SECONDS,
    run=jobs.run_expire_grants,
)

REGISTRY: dict[str, Job] = {EXPIRE_GRANTS_JOB.name: EXPIRE_GRANTS_JOB}
"""The jobs this service runs. 21a registers exactly one."""


async def _claim(redis: Redis, job: Job) -> bool:
    """Take the cadence gate for this window, or find it already taken.

    `SET … NX PX` is ONE operation: an `EXISTS` followed by a `SET` would let
    two runners both read "free" and both run. The value is a token nobody
    reads today — the key's presence is the whole gate — and it is written
    anyway so an operator can tell which instance opened the window.
    """
    claimed = await redis.set(
        job_lock_key(job.name),
        uuid.uuid4().hex,
        nx=True,
        px=job.cadence_seconds * 1000,
    )
    return bool(claimed)


async def _run_for_every_tenant(
    factory: async_sessionmaker[AsyncSession], job: Job, now: datetime
) -> bool:
    """Run one job once per tenant. True when every tenant succeeded.

    One `tenant_session` — and so one transaction — per tenant, for two
    reasons that are really one: the jobs read and write tenant-scoped tables,
    which an unbound session refuses outright, and one tenant's broken data
    must not roll back or skip another's. A failure is logged and the sweep
    moves to the next tenant, which is what
    `::test_one_tenant_raising_leaves_the_other_tenants_work_committed`
    asserts by counting rows on both sides.
    """
    async with factory() as session:
        tenant_ids = await list_tenant_ids(session)

    healthy = True
    for tenant_id in tenant_ids:
        try:
            async with tenant_session(factory, tenant_id) as session:
                await job.run(session, now)
        except Exception:
            healthy = False
            logger.exception(
                "scheduler job failed for one tenant", extra={"job": job.name, "tenant": tenant_id}
            )
    return healthy


async def tick(factory: async_sessionmaker[AsyncSession], redis: Redis, *, now: datetime) -> None:
    """One pass over the registry: claim what is due, run it, keep the gate.

    `REGISTRY` is read here rather than captured, so a test can install its
    own. A claimed job's key is LEFT in place on success — decision B, the key
    is the gate — and deleted when the run failed for any tenant, decision C.
    Nothing raises out of here: this is a lifespan task, and a loop that dies
    leaves a service that answers every health check and does no work.
    """
    for job in list(REGISTRY.values()):
        try:
            check_job_name(job.name)
            if not await _claim(redis, job):
                continue
            if not await _run_for_every_tenant(factory, job, now):
                await redis.delete(job_lock_key(job.name))
        except Exception:
            logger.exception("scheduler job could not run", extra={"job": job.name})


async def run_loop() -> None:
    """Tick for ever, every `TICK_SECONDS`, until the lifespan cancels it.

    `now` is read once per tick and handed down, so every job of one tick — and
    both the read that selects a lapsed set and the event that records it —
    answer for one instant.
    """
    settings = load_settings()
    factory = create_session_factory(shared_engine(settings.database_url))
    redis = get_redis(settings)
    try:
        while True:
            await tick(factory, redis, now=datetime.now(UTC))
            await asyncio.sleep(TICK_SECONDS)
    finally:
        await close_loop_clients()
        await dispose_shared_engines()


__all__ = [
    "EXPIRE_GRANTS_CADENCE_SECONDS",
    "JOB_LOCK_PREFIX",
    "JOB_NAME_MAX_LENGTH",
    "JOB_NAME_PATTERN",
    "REGISTRY",
    "TICK_SECONDS",
    "InvalidJobNameError",
    "Job",
    "check_job_name",
    "job_lock_key",
    "run_loop",
    "tick",
]
