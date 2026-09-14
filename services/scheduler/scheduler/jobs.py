"""The jobs themselves, each a thin adaptation of an engine in the library.

Nothing here decides anything. A job takes the tenant-bound session the runner
opened and the instant the runner is answering for, and calls into `helpdesk`;
each sweep's rules, its audit events and its notifications belong to a module
of the library — `helpdesk.access.expiry`, `helpdesk.access.leavers`,
`helpdesk.access.review_schedule`, `helpdesk.access.gauges`,
`helpdesk.audit.health`, `helpdesk.audit.anchor` — so they can be exercised
with a session and a fixed `now` and no scheduler anywhere near the test. One
wrapper calls TWO of them (`run_metrics_sweep`, an earlier change decision A) and still
decides nothing: it takes two readings that belong on one scrape at one age.

21a registered one job; 21b adds the two DAILY ones, the expiry warnings and
the leaver sweep. 21c takes the design's last pair, the quarterly review
generation and its daily reminders, both registered daily: the generation is
quarterly by its PERIOD and not by its cadence (21c decision A), so a missed
run is made good on the next tick rather than in three months. 21d adds the
sixth, which is not one of the design's jobs at all — §11 lists the jobs that
CHANGE access, and `metrics_sweep` changes nothing: it takes the design's two
gauge readings, because a reading needs something to take it. 30bb adds the
seventh on the same footing: `chain_anchor` changes no access either, and it
is the one job here that WRITES a row, which is why it is not folded into the
sweep that changes nothing (30bb decision C).
"""

from datetime import datetime

from helpdesk.access.directory import SimulatedDirectory
from helpdesk.access.expiry import expire_grants, warn_expiring_grants
from helpdesk.access.gauges import refresh_active_grants_gauge
from helpdesk.access.leavers import sweep_leavers
from helpdesk.access.review_schedule import generate_reviews, send_review_reminders
from helpdesk.audit.anchor import anchor_tenant_chain
from helpdesk.audit.health import refresh_chain_gauge
from helpdesk.obs import metrics
from sqlalchemy.ext.asyncio import AsyncSession

metrics.SWEEP_GAUGES = metrics.install_sweep_gauges(metrics.REGISTRY)
"""Declare the design's two gauges HERE, in the module that holds the sweep.

An earlier change decision F. `build_metrics`' six are registered on every service
because a counter reading zero on a process that never moves it is the honest
answer; a GAUGE is not like that. An unlabelled `helpdesk_audit_chain_ok`
declared by agent-api would render `0` on its exposition from start-up — a
tampered audit log, on a service that verifies nothing — so the two gauges
exist only where something can move them. Importing this module is what a
process does to run the jobs, which makes it the one honest place to install
them. Asserted from the other side by
`test_obs_metrics.py::test_build_metrics_still_registers_exactly_the_six_metrics`."""

EXPIRE_GRANTS_JOB_NAME = "expire_grants"
"""the design's name for the sweep, and the segment of its Redis lock key.

Pinned together with the key it produces by
`test_scheduler_runner.py::test_the_runner_constants_are_exactly_the_planned_values`."""


async def run_expire_grants(session: AsyncSession, now: datetime) -> None:
    """Expire the bound tenant's lapsed grants (the design).

    The return value is dropped on purpose: the counter and the audit events
    are the record of what happened, and a runner that inspected the list
    would be a second reader of a decision the engine has already made.
    """
    await expire_grants(session, now=now)


EXPIRY_WARNINGS_JOB_NAME = "expiry_warnings"
"""the design's name for the warning sweep, and the segment of its lock key.

Pinned together with the key it produces by
`test_scheduler_daily_jobs.py::test_the_daily_job_constants_are_exactly_the_planned_values`."""

LEAVER_SWEEP_JOB_NAME = "leaver_sweep"
"""the design's name for the leaver sweep, and the segment of its lock key.

Pinned by the same case."""


async def run_expiry_warnings(session: AsyncSession, now: datetime) -> None:
    """Warn the bound tenant's holders about grants about to lapse (the design).

    `now` is the tick's instant, handed straight down: the band that decides
    who is warned has to answer for the same moment as every other job of the
    tick, and a clock read here would make the engine untestable at a fixed
    time. Pinned by
    `test_scheduler_daily_jobs.py::test_the_expiry_warnings_wrapper_hands_its_session_and_now_to_the_engine`.

    The return value is dropped for the reason `run_expire_grants` drops its
    own: the notifications are the record, and a runner reading the list would
    be a second reader of a decision the engine already made.
    """
    await warn_expiring_grants(session, now=now)


async def run_leaver_sweep(session: AsyncSession, now: datetime) -> None:
    """Offboard the bound tenant's overdue leavers (the design).

    Same shape, same reason, and the clock matters more here: the due test is
    `end_date < now.date()`, so a wrapper reading its own clock would decide
    somebody's last working day against a different date from the one the tick
    answers for. Pinned by
    `test_scheduler_daily_jobs.py::test_the_leaver_sweep_wrapper_hands_its_session_and_now_to_the_engine`.
    """
    await sweep_leavers(session, now=now)


REVIEW_GENERATION_JOB_NAME = "review_generation"
"""the design's name for the quarterly generation, and the segment of its lock key.

Pinned together with the key it produces by
`test_scheduler_review_jobs.py::test_the_review_job_constants_are_exactly_the_planned_values`."""

REVIEW_REMINDERS_JOB_NAME = "review_reminders"
"""the design's name for the reminder sweep, and the segment of its lock key.

Pinned by the same case."""


async def run_review_generation(session: AsyncSession, now: datetime) -> None:
    """Open the bound tenant's due reviews for this quarter (the design).

    `now` is the tick's instant, handed straight down: it decides the PERIOD
    every review of the run is filed under and the `due_at` every reviewer is
    held to, so a clock read here would let one tick's scopes straddle a
    quarter boundary in two directions and make the job untestable at a fixed
    time. Pinned by
    `test_scheduler_review_jobs.py::test_the_review_generation_wrapper_hands_its_session_and_now_to_the_engine`.

    The directory is built from the SAME session, so the reads that flag a
    grant answer for the tenant the runner bound. The return value is dropped
    for the reason every other wrapper drops its own: the reviews and their
    `review.opened` events are the record.
    """
    await generate_reviews(session, SimulatedDirectory(session), now=now)


async def run_review_reminders(session: AsyncSession, now: datetime) -> None:
    """Tell the bound tenant's late reviewers, and escalate the very late (the design).

    Same shape, same reason: both of this job's boundaries
    are written to the microsecond against `now`, so a wrapper reading its own
    clock would measure the overdue band and the fourteen-day escalation from a
    different instant than the tick answers for. Pinned by
    `::test_the_review_reminders_wrapper_hands_its_session_and_now_to_the_engine`.
    """
    await send_review_reminders(session, now=now)


METRICS_SWEEP_JOB_NAME = "metrics_sweep"
"""The name of the gauge sweep, and the segment of its lock key.

Not one of the design's five jobs: §11 lists the jobs that CHANGE access, and
this one changes nothing at all. It is here because the design's two gauges are
readings rather than events, and a reading needs something to take it. Pinned
together with the key it produces by
`test_scheduler_metrics_job.py::test_the_metrics_job_constants_are_exactly_the_planned_values`."""


async def run_metrics_sweep(session: AsyncSession, now: datetime) -> None:
    """Refresh both of the design's gauges for the bound tenant.

    One job for both readings (decision A): one lock key means the grant count
    and the chain verdict on a scrape are always the same age, where two
    cadence gates would let a dashboard show a fresh number beside a verdict
    from an hour ago with nothing on the page to say so.

    `now` is the tick's instant, handed straight down: it is the boundary that
    decides which grants are live (`expires_at > now`) and the exact complement
    of the expiry sweep's `<= now`, so a clock read here would let two jobs of
    one tick disagree about a grant lapsing between them. The chain verdict
    takes no `now` — it is about the rows that exist, not about an instant.

    Both return values are dropped, for the reason every other wrapper here
    drops its own: the gauges are the record. Pinned by
    `test_scheduler_metrics_job.py::test_the_metrics_sweep_wrapper_hands_its_session_and_now_to_both_engines`,
    which asserts both call lists, so dropping either engine fails.
    """
    await refresh_active_grants_gauge(session, now=now)
    await refresh_chain_gauge(session)


CHAIN_ANCHOR_JOB_NAME = "chain_anchor"
"""The name of the anchor sweep, and the segment of its Redis lock key.

Not one of the design's jobs either: it changes no access. It is a job of its own
rather than a line inside `metrics_sweep`, because that
sweep changes nothing at all and this one writes a row, and because the rate
at which a head is anchored should not be tied to how fresh a gauge is.
Pinned together with the key it produces by
`test_scheduler_chain_anchor_job.py::test_the_chain_anchor_job_constants_are_exactly_the_planned_values`."""


async def run_chain_anchor(session: AsyncSession, now: datetime) -> None:
    """Anchor the bound tenant's verified chain head.

    `now` is accepted because every job takes the tick's instant, and dropped
    because an anchor is a statement about the rows that exist: the engine
    reads no clock of its own beyond the moment it records the row, and an
    instant handed down would be a second, disagreeing record of when the head
    was taken. Pinned by
    `test_scheduler_chain_anchor_job.py::test_the_chain_anchor_wrapper_hands_its_session_to_the_engine`,
    whose double takes no `now` at all.

    The return value is dropped for the reason every other wrapper here drops
    its own: the anchor row and its log line are the record, and the decision
    not to write one was already made inside the engine.
    """
    await anchor_tenant_chain(session)
