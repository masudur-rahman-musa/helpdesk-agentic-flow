"""The daily job that offboards people whose last day has passed.

The sweep only decides who is due. All revoking and logging is done by
`offboard_user`, the same engine the MCP tool calls, so access has one writer.

Due means `status = 'active'` and `end_date < now.date()`. Strictly before,
because `end_date` is the last day the person still holds access. One
transaction per tenant: if one person fails, the whole day rolls back and the
runner retries, rather than leaving half the departures logged.
"""

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.access.directory import SimulatedDirectory
from helpdesk.shared.access.lifecycle import AlreadyLeaverError
from helpdesk.shared.access.offboarding import offboard_user
from helpdesk.shared.access.revoke import SYSTEM_ACTOR_KIND
from helpdesk.shared.crud.users import lock_due_users
from helpdesk.shared.models import User

LEAVER_SWEEP_ACTOR_REF = "scheduler:leaver_sweep"
"""What `audit_events.actor_ref` reads on every event this job causes.

The string an auditor searches for to tell the nightly sweep from a person.
It sits beside `actor_kind = 'system'`, never in place of it.
"""


async def _lock_due_users(session: AsyncSession, now: datetime) -> Sequence[User]:
    """The bound tenant's overdue people, locked for this transaction.

    `end_date IS NOT NULL` keeps a permanent employee from ever being due.
    `now.date()`, not `now`, because `end_date` is a date: comparing it to a
    timestamp would skip yesterday's leaver until the day after.
    """
    return await lock_due_users(session, now)


async def sweep_leavers(session: AsyncSession, *, now: datetime) -> list[User]:
    """Offboard everyone whose last day has passed. The caller commits.

    Each person keeps their own `end_date`, never the tick's date: when someone
    left is a fact about them, not about when a job noticed.

    Only `AlreadyLeaverError` is caught. It is raised before any write, so the
    session is clean and the person is already offboarded; catching it stops one
    manual offboard from rolling back the whole day. Every other failure goes to
    the runner, which retries on the next tick. Returns only the people this run
    offboarded.
    """
    directory = SimulatedDirectory(session)
    due = await _lock_due_users(session, now)

    offboarded: list[User] = []
    for user in due:
        end_date = user.end_date
        if end_date is None:  # pragma: no cover - the filter excludes these
            continue
        try:
            await offboard_user(
                session,
                directory,
                user_id=user.id,
                end_date=end_date,
                offboarded_by_user_id=None,
                actor_ref=LEAVER_SWEEP_ACTOR_REF,
                actor_kind=SYSTEM_ACTOR_KIND,
                session_id=None,
                now=now,
            )
        except AlreadyLeaverError:
            # Someone offboarded this person since the read. Skip them, not
            # the whole sweep.
            continue
        offboarded.append(user)

    return offboarded


__all__ = ["LEAVER_SWEEP_ACTOR_REF", "sweep_leavers"]
