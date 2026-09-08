"""The jobs themselves, each a thin adaptation of an engine in the library.

Nothing here decides anything. A job takes the tenant-bound session the runner
opened and the instant the runner is answering for, and calls one function in
`helpdesk`; the sweep's rules, its audit event and its notifications belong to
`helpdesk.access.expiry`, so they can be exercised with a session and a fixed
`now` and no scheduler anywhere near the test.

21a registers exactly one job. 21b adds the expiry warnings and the leaver
sweep, 21c the review generation and its reminders.
"""

from datetime import datetime

from helpdesk.access.expiry import expire_grants
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
