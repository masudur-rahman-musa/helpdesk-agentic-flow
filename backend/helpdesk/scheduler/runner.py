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
that outlives its own cadence can overlap the next one — harmless for seven of
the eight registered jobs, because each is idempotent: `expire_grants` has its
row lock and its `status = 'active'` test, `leaver_sweep` the same pair over
`users`, `expiry_warnings` its anti-join against the notification it already
wrote, `review_generation` its per-period scope check
(21c decision A), `metrics_sweep` writes nothing at all — it takes a
reading and sets two gauges, so an overlap costs one repeated measurement —
`chain_anchor` skips a head already anchored, with the unique index on
(`tenant_id`, `audit_seq`) refusing the pair that raced past that check (plan
30bb §5), and `anchor_retention` deletes rows a second run no longer finds.
`review_reminders` is the exception and deliberately so:
21c decision E has no dedupe at all, because the daily cadence gate IS the
dedupe, so an overlap there costs one duplicate bell entry and nothing else.

Decision C is the other half: a job that RAISES has its key deleted, so a
transient Postgres blip costs one tick (60 seconds) instead of a full cadence
— 300 seconds for the expiry sweep, and a whole day for either job registered
at `DAILY_CADENCE_SECONDS`, which is what makes deleting the key matter more
for them than for the job 21a shipped. A job that merely fails for one tenant
still counts as raising for this
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

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from helpdesk.scheduler import jobs
from helpdesk.shared.core.config import load_settings
from helpdesk.shared.core.db import (
    create_session_factory,
    dispose_shared_engines,
    shared_engine,
    tenant_session,
)
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.core.observability.metrics import record_job_failure, record_job_run
from helpdesk.shared.core.redis import close_loop_clients, get_redis
from helpdesk.shared.crud.tenants import list_tenant_ids

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

DAILY_CADENCE_SECONDS = 86400
"""the design's "daily", and the `PX` of the lock key of all four daily jobs.

One number for the cadence and the TTL, for the reason
`EXPIRE_GRANTS_CADENCE_SECONDS` is: the key's expiry IS what opens the next
window. A job whose declared cadence and lock TTL disagreed would run every
tick or never again — at 3600 the leaver sweep would offboard 24 times a day,
the warning sweep would lean entirely on its dedupe, and 21c's reminder sweep,
which has no dedupe at all, would nag every late reviewer 24 times. Pinned
exactly by
`test_scheduler_daily_jobs.py::test_the_daily_job_constants_are_exactly_the_planned_values`
and `test_scheduler_review_jobs.py::test_the_review_job_constants_are_exactly_the_planned_values`,
and on the real registry by
`test_scheduler_daily_jobs.py::test_a_tick_leaves_both_daily_keys_with_a_one_day_ttl`
and `test_scheduler_review_jobs.py::test_a_tick_leaves_both_review_keys_with_a_one_day_ttl`.

Decision H of an earlier change: a daily job therefore runs at whatever clock time it
first claimed its key, drifting by up to one tick a day rather than at a fixed
hour. Three of the four are idempotent, so the hour changes nothing an auditor
reads; the reminder sweep writes one notification per run by design (21c
decision E), so the hour is the only thing deciding when a reviewer is told.
A fixed hour would need the last-run state 21a decision B deliberately does not
keep."""

METRICS_SWEEP_CADENCE_SECONDS = 300
"""How often the design's two gauges are re-read, and the `PX` of their key.

One number for the cadence and the TTL, for the reason
`EXPIRE_GRANTS_CADENCE_SECONDS` is: the key's expiry IS what opens the next
window. An earlier change decision C chose 300 over reusing `DAILY_CADENCE_SECONDS`,
because a gauge is read as "how many right now" — a daily refresh makes
`helpdesk_grants_active` a WRONG number for 23 hours rather than a missing
one, and reports a chain nobody has walked since yesterday as verified. The
trade is that the sweep walks every audit event of every tenant every five
minutes; the cost grows with the log, and this constant is the lever. Pinned
exactly by
`test_scheduler_metrics_job.py::test_the_metrics_job_constants_are_exactly_the_planned_values`
and, on the real registry, by
`::test_a_tick_leaves_the_metrics_key_with_a_three_hundred_second_ttl`."""

CHAIN_ANCHOR_CADENCE_SECONDS = 900
"""How often a tenant's verified head is re-anchored, and the `PX` of its key.

One number for the cadence and the TTL, for the reason
`EXPIRE_GRANTS_CADENCE_SECONDS` is: the key's expiry IS what opens the next
window. It is also the window in which a truncation is invisible — everything
deleted between the last anchored head and the next one is covered by no
anchor — so a day's cadence would leave an attacker a day to work in, and a
tick's cadence would walk every audit event of every tenant every minute and
grow the anchor table sixty times faster. Pinned exactly by
`test_scheduler_chain_anchor_job.py::test_the_chain_anchor_job_constants_are_exactly_the_planned_values`
and, on the real registry, by
`::test_a_tick_leaves_the_chain_anchor_key_with_a_nine_hundred_second_ttl`."""

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
    """The Redis key that is this job's cadence gate (one-argument form, unchanged).

    Kept for the constants tests and for operator queries such as
    `KEYS helpdesk:joblock:*`. The per-tenant variant is `tenant_lock_key`.
    """
    return f"{JOB_LOCK_PREFIX}{name}"


def tenant_lock_key(name: str, tenant_id: uuid.UUID) -> str:
    """The Redis key that is THIS TENANT'S cadence gate for one job.

    `helpdesk:joblock:<name>:<tenant_id>` — the job segment is the same as
    `job_lock_key(name)` with the tenant uuid appended after a colon. An
    operator can still scan all locks with `KEYS helpdesk:joblock:*`, and can
    narrow to one job's tenants with `KEYS helpdesk:joblock:<name>:*`. Pinned
    as a shape by
    `test_scheduler_runner.py::test_tenant_lock_key_is_the_job_key_then_the_tenant_uuid`.
    """
    return f"{JOB_LOCK_PREFIX}{name}:{tenant_id}"


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

EXPIRY_WARNINGS_JOB = Job(
    name=check_job_name(jobs.EXPIRY_WARNINGS_JOB_NAME),
    cadence_seconds=DAILY_CADENCE_SECONDS,
    run=jobs.run_expiry_warnings,
)

LEAVER_SWEEP_JOB = Job(
    name=check_job_name(jobs.LEAVER_SWEEP_JOB_NAME),
    cadence_seconds=DAILY_CADENCE_SECONDS,
    run=jobs.run_leaver_sweep,
)

REVIEW_GENERATION_JOB = Job(
    name=check_job_name(jobs.REVIEW_GENERATION_JOB_NAME),
    cadence_seconds=DAILY_CADENCE_SECONDS,
    run=jobs.run_review_generation,
)

REVIEW_REMINDERS_JOB = Job(
    name=check_job_name(jobs.REVIEW_REMINDERS_JOB_NAME),
    cadence_seconds=DAILY_CADENCE_SECONDS,
    run=jobs.run_review_reminders,
)

METRICS_SWEEP_JOB = Job(
    name=check_job_name(jobs.METRICS_SWEEP_JOB_NAME),
    cadence_seconds=METRICS_SWEEP_CADENCE_SECONDS,
    run=jobs.run_metrics_sweep,
)

CHAIN_ANCHOR_JOB = Job(
    name=check_job_name(jobs.CHAIN_ANCHOR_JOB_NAME),
    cadence_seconds=CHAIN_ANCHOR_CADENCE_SECONDS,
    run=jobs.run_chain_anchor,
)

ANCHOR_RETENTION_JOB = Job(
    name=check_job_name(jobs.ANCHOR_RETENTION_JOB_NAME),
    cadence_seconds=DAILY_CADENCE_SECONDS,
    run=jobs.run_anchor_retention,
)

REGISTRY: dict[str, Job] = {
    job.name: job
    for job in (
        EXPIRE_GRANTS_JOB,
        EXPIRY_WARNINGS_JOB,
        LEAVER_SWEEP_JOB,
        REVIEW_GENERATION_JOB,
        REVIEW_REMINDERS_JOB,
        METRICS_SWEEP_JOB,
        CHAIN_ANCHOR_JOB,
        ANCHOR_RETENTION_JOB,
    )
}
"""The eight jobs this service runs: 21a's five-minute sweep, 21b's daily pair,
21c's pair, 21d's gauge sweep, 30bb's anchor sweep, and 30cc's retention sweep.

Every name goes through `check_job_name` at construction, so a name that could
not safely become a Redis key segment fails at import rather than inside a
`SET` at three in the morning. Pinned per job by
`test_scheduler_daily_jobs.py::test_each_new_job_is_registered_at_the_daily_cadence_under_a_valid_name`
`test_scheduler_review_jobs.py::test_each_review_job_is_registered_at_the_daily_cadence_under_a_valid_name`
and
`test_scheduler_metrics_job.py::test_the_metrics_job_is_registered_at_its_cadence_under_a_valid_name`
and
`test_scheduler_chain_anchor_job.py::test_the_chain_anchor_job_is_registered_at_its_cadence_under_a_valid_name`
and
`test_scheduler_anchor_retention_job.py::test_the_anchor_retention_job_is_registered_at_the_daily_cadence_under_a_valid_name`;
and as a whole — eight distinct names, every older job still present — by
`test_scheduler_daily_jobs.py::test_the_registry_still_holds_the_five_minute_sweep_beside_the_daily_pair`
and
`test_scheduler_metrics_job.py::test_the_registry_holds_all_eight_jobs_under_eight_distinct_names`."""


async def _claim_for_tenant(redis: Redis, job: Job, tenant_id: uuid.UUID) -> bool:
    """Take THIS TENANT'S cadence gate for this window, or find it already taken.

    `SET … NX PX` is ONE operation: an `EXISTS` followed by a `SET` would let
    two runners both read "free" and both run. The value is a token nobody
    reads today — the key's presence is the whole gate — and it is written
    anyway so an operator can tell which instance opened the window. Pinned
    by the per-tenant TTL cases in `test_scheduler_runner.py`.
    """
    claimed = await redis.set(
        tenant_lock_key(job.name, tenant_id),
        uuid.uuid4().hex,
        nx=True,
        px=job.cadence_seconds * 1000,
    )
    return bool(claimed)


async def tick(factory: async_sessionmaker[AsyncSession], redis: Redis, *, now: datetime) -> None:
    """One pass over the registry: claim what is due, run it, keep the gate.

    `REGISTRY` is read here rather than captured, so a test can install its
    own. The lock is now per (job, tenant): each tenant claims its own key,
    runs in its own transaction, and frees only its own key on failure — so
    one tenant's error never re-runs the others.

    A claimed key is LEFT in place on success — decision B, the key is the
    gate — and deleted when that tenant's run failed, decision C applied per
    tenant. A `CancelledError` (loop shutdown) frees the key it was holding
    and propagates, so the loop task ends cleanly and `/healthz` can detect
    the death. All other exceptions are logged and do not escape: this is a
    lifespan task, and an error elsewhere kills the loop and silences every
    health check.
    """
    try:
        async with factory() as session:
            tenant_ids = await list_tenant_ids(session)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("scheduler could not load tenant list")
        return

    for job in list(REGISTRY.values()):
        try:
            check_job_name(job.name)
        except Exception:
            logger.exception("scheduler job could not run", extra={"job": job.name})
            continue

        for tenant_id in tenant_ids:
            try:
                if not await _claim_for_tenant(redis, job, tenant_id):
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("scheduler job could not run", extra={"job": job.name})
                continue

            try:
                async with tenant_session(factory, tenant_id) as session:
                    await job.run(session, now)
                record_job_run(job=job.name)
            except asyncio.CancelledError:
                await redis.delete(tenant_lock_key(job.name, tenant_id))
                raise
            except Exception:
                await redis.delete(tenant_lock_key(job.name, tenant_id))
                record_job_run(job=job.name)
                record_job_failure(job=job.name)
                logger.exception(
                    "scheduler job failed for one tenant",
                    extra={"job": job.name, "tenant": tenant_id},
                )


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
    "CHAIN_ANCHOR_CADENCE_SECONDS",
    "DAILY_CADENCE_SECONDS",
    "EXPIRE_GRANTS_CADENCE_SECONDS",
    "JOB_LOCK_PREFIX",
    "JOB_NAME_MAX_LENGTH",
    "JOB_NAME_PATTERN",
    "METRICS_SWEEP_CADENCE_SECONDS",
    "REGISTRY",
    "TICK_SECONDS",
    "InvalidJobNameError",
    "Job",
    "check_job_name",
    "job_lock_key",
    "run_loop",
    "tenant_lock_key",
    "tick",
]
