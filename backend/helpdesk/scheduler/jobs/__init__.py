"""The jobs themselves, each a thin wrapper over an engine in the library.

Nothing here decides anything. A job gets the tenant-bound session and the
tick's instant from the runner and calls into `helpdesk`. Every wrapper hands
`now` down rather than reading a clock, so all jobs of one tick answer for the
same moment and each engine can be tested at a fixed time. Return values are
dropped: the audit events, notifications and gauges are the record.
"""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.scheduler.jobs.expiry import expire_grants, warn_expiring_grants
from helpdesk.scheduler.jobs.leavers import sweep_leavers
from helpdesk.scheduler.jobs.metrics_sweep import refresh_active_grants_gauge, refresh_chain_gauge
from helpdesk.scheduler.jobs.review_schedule import generate_reviews, send_review_reminders
from helpdesk.shared.access.directory import SimulatedDirectory
from helpdesk.shared.audit.anchor import anchor_tenant_chain
from helpdesk.shared.audit.retention import purge_chain_anchors
from helpdesk.shared.core.observability import metrics

metrics.SWEEP_GAUGES = metrics.install_sweep_gauges(metrics.REGISTRY)
"""Install the two sweep gauges only in the process that runs the jobs.

A gauge, unlike a counter, should not read zero on a service that never sets
it: `helpdesk_audit_chain_ok` showing `0` on the agent API would look like a
tampered audit log."""

EXPIRE_GRANTS_JOB_NAME = "expire_grants"


async def run_expire_grants(session: AsyncSession, now: datetime) -> None:
    """Expire the bound tenant's lapsed grants."""
    await expire_grants(session, now=now)


EXPIRY_WARNINGS_JOB_NAME = "expiry_warnings"

LEAVER_SWEEP_JOB_NAME = "leaver_sweep"


async def run_expiry_warnings(session: AsyncSession, now: datetime) -> None:
    """Warn the bound tenant's holders about grants about to lapse."""
    await warn_expiring_grants(session, now=now)


async def run_leaver_sweep(session: AsyncSession, now: datetime) -> None:
    """Offboard the bound tenant's overdue leavers.

    The due test is `end_date < now.date()`, so a wrapper with its own clock
    could judge someone's last working day against the wrong date.
    """
    await sweep_leavers(session, now=now)


REVIEW_GENERATION_JOB_NAME = "review_generation"

REVIEW_REMINDERS_JOB_NAME = "review_reminders"


async def run_review_generation(session: AsyncSession, now: datetime) -> None:
    """Open the bound tenant's due reviews for this quarter.

    `now` sets the quarter every review is filed under and the `due_at` each
    reviewer is held to. The directory uses the same session, so its reads
    answer for the tenant the runner bound.
    """
    await generate_reviews(session, SimulatedDirectory(session), now=now)


async def run_review_reminders(session: AsyncSession, now: datetime) -> None:
    """Remind the bound tenant's late reviewers, and escalate the very late."""
    await send_review_reminders(session, now=now)


METRICS_SWEEP_JOB_NAME = "metrics_sweep"


async def run_metrics_sweep(session: AsyncSession, now: datetime) -> None:
    """Refresh both sweep gauges for the bound tenant.

    One job for both readings, so the grant count and the chain verdict on a
    scrape are always the same age. A grant is live while `expires_at > now`,
    the exact opposite of the expiry sweep's `<= now`.
    """
    await refresh_active_grants_gauge(session, now=now)
    await refresh_chain_gauge(session)


CHAIN_ANCHOR_JOB_NAME = "chain_anchor"


async def run_chain_anchor(session: AsyncSession, now: datetime) -> None:
    """Anchor the bound tenant's verified chain head.

    `now` is ignored: an anchor is about the rows that exist, and the engine
    records its own time. Kept apart from the metrics sweep because this job
    writes a row and that one writes nothing.
    """
    await anchor_tenant_chain(session)


ANCHOR_RETENTION_JOB_NAME = "anchor_retention"


async def run_anchor_retention(session: AsyncSession, now: datetime) -> None:
    """Purge the bound tenant's anchors past the retention window.

    Kept apart from `chain_anchor` so the job that writes anchors never also
    deletes them.
    """
    await purge_chain_anchors(session, now)
