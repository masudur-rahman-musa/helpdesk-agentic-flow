"""The loop, the job registry, and the Redis lock that is the cadence gate.

A plain asyncio loop rather than a scheduler library, so every job stays
callable from a test. The lock is `SET <key> NX PX <cadence>` and is never
released on success: one key says both "is it due" and "is someone on it",
and the next window opens when Redis expires it. A job that raises has its
key deleted, so it retries next tick instead of a full cadence later. Every
job is idempotent, so an overlap is safe.
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
"""How often the loop wakes. Also the soonest a failed job can retry."""

EXPIRE_GRANTS_CADENCE_SECONDS = 300
"""Cadence and lock TTL are one number: the key expiring opens the next window."""

DAILY_CADENCE_SECONDS = 86400
"""A daily job runs at whatever time it first took its key, not a fixed hour."""

METRICS_SWEEP_CADENCE_SECONDS = 300
"""Not daily: a day-old gauge is a wrong number. The cost is a full log walk."""

CHAIN_ANCHOR_CADENCE_SECONDS = 900
"""Also how long a truncated log can go unseen, so it is kept short."""

JOB_LOCK_PREFIX = "helpdesk:joblock:"
"""Every job lock lives under this, so one `KEYS` lists them all."""

JOB_NAME_MAX_LENGTH = 64
"""Redis would store a key of any length, so the name is capped first."""

JOB_NAME_PATTERN = r"[a-z][a-z0-9_]*"
"""Matched with `fullmatch`, so a trailing newline cannot make a look-alike key."""

_JOB_NAME_RE = re.compile(JOB_NAME_PATTERN)


class InvalidJobNameError(ToolError):
    """A job name that cannot safely become part of a Redis key."""

    code = ErrorCode.VALIDATION


def check_job_name(name: str) -> str:
    """The accepted job name, or a typed refusal. Length first, before the regex."""
    if len(name) > JOB_NAME_MAX_LENGTH:
        raise InvalidJobNameError(
            f"a job name longer than {JOB_NAME_MAX_LENGTH} characters cannot be "
            f"a lock key: {len(name)}"
        )
    if _JOB_NAME_RE.fullmatch(name) is None:
        raise InvalidJobNameError(f"{name!r} is not a job name: it must match {JOB_NAME_PATTERN}")
    return name


def job_lock_key(name: str) -> str:
    """The job part of a lock key."""
    return f"{JOB_LOCK_PREFIX}{name}"


def tenant_lock_key(name: str, tenant_id: uuid.UUID) -> str:
    """One tenant's lock key for one job."""
    return f"{JOB_LOCK_PREFIX}{name}:{tenant_id}"


JobRun = Callable[[AsyncSession, datetime], Awaitable[None]]


@dataclass(frozen=True)
class Job:
    """One periodic job. `run` gets a tenant-bound session and the tick's instant."""

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
"""Every name is checked at import, so a bad one fails at start-up."""


async def _claim_for_tenant(redis: Redis, job: Job, tenant_id: uuid.UUID) -> bool:
    """Take this tenant's lock for this window, in one atomic `SET NX PX`."""
    claimed = await redis.set(
        tenant_lock_key(job.name, tenant_id),
        uuid.uuid4().hex,
        nx=True,
        px=job.cadence_seconds * 1000,
    )
    return bool(claimed)


async def tick(factory: async_sessionmaker[AsyncSession], redis: Redis, *, now: datetime) -> None:
    """One pass over the registry: claim what is due, run it, keep the lock.

    Each tenant has its own lock and transaction, so one tenant's error never
    re-runs the others. Errors are logged and kept in, since an escaped error
    would kill the loop while health checks still pass.
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
    """Tick every `TICK_SECONDS` until cancelled. `now` is read once per tick."""
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
