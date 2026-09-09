"""The jobs themselves, each a thin adaptation of an engine in the library.

Nothing here decides anything. A job takes the tenant-bound session the runner
opened and the instant the runner is answering for, and calls one function in
`helpdesk`; each sweep's rules, its audit events and its notifications belong
to `helpdesk.access.expiry` or `helpdesk.access.leavers`, so they can be
exercised with a session and a fixed `now` and no scheduler anywhere near the
test.

21a registered one job; 21b adds the two DAILY ones, the expiry warnings and
the leaver sweep. 21c takes the review generation and its reminders.
"""

from datetime import datetime

from helpdesk.access.expiry import expire_grants, warn_expiring_grants
from helpdesk.access.leavers import sweep_leavers
from sqlalchemy.ext.asyncio import AsyncSession

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
