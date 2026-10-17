"""`sweep_leavers`: the daily job that offboards people whose last day has gone.

The sweep decides ONE thing — who is due — and nothing else. Every revocation,
every closed request and every event is `offboard_user`'s, called once per due
person exactly as the MCP tool calls it but with the system actor (an earlier change
decision D). That is why this module is fifty lines: a sweep that revoked
grants itself would be a second writer of access, with its own reason and its
own order, and the two would drift.

It sits in its own module rather than in `lifecycle.py` (decision G).
`lifecycle.py` owns the joiner, mover and leaver ENGINES; the schedule that
calls one of them is a different question, and the file is already long.

Due means `status = 'active'` and `end_date < now.date()` — strictly before,
because `end_date` is the LAST day the person holds access and `<=` would take
every account off them mid-handover (decision E, the design's own comparison).
Both halves of that boundary are pinned exactly by
`test_access_leavers.py::test_a_user_whose_end_date_was_yesterday_is_swept` and
`::test_a_user_whose_end_date_is_today_is_untouched`.

Everything runs in the caller's transaction, over the BOUND tenant only: the
runner opens one `tenant_session` per tenant and calls this inside each, so a
statement built outside the session's scope would offboard every tenant's
departures on the first tenant's tick (refused by
`::test_another_tenants_due_user_is_untouched`). One transaction per tenant
also means a failure on the fifth person rolls back the first four — deliberate
(plan §5): the alternative is half a day's departures in the log with no record
of what was skipped, and the runner retries a tick later.
"""

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.access.revoke import SYSTEM_ACTOR_KIND
from helpdesk.shared.models import User
from helpdesk.shared.policy.types import TargetStatus
from helpdesk.shared.access.directory import SimulatedDirectory
from helpdesk.shared.access.offboarding import offboard_user
from helpdesk.shared.access.lifecycle import AlreadyLeaverError

LEAVER_SWEEP_ACTOR_REF = "scheduler:leaver_sweep"
"""What `audit_events.actor_ref` reads for every event this job causes.

The one string an auditor greps to separate the nightly sweep's departures
from a person's, and the value the runbook prints. Beside
`actor_kind = 'system'`, never instead of it (decision D): a kind of `user`
with a system-shaped ref would file an automatic sweep among human decisions,
which is the confusion `expire_grants`' own system actor was written to avoid.
Pinned exactly, on both event types, by
`test_access_leavers.py::test_every_event_the_sweep_causes_carries_the_system_actor_and_its_ref`
and `::test_the_leaver_sweep_constants_are_exactly_the_planned_values`."""

_ACTIVE_USER_STATUS = TargetStatus.ACTIVE.value
"""Read off `TargetStatus`, like `lifecycle.LEAVER_STATUS`, so the status this
sweep looks for and the one an offboard writes cannot drift apart."""


async def _lock_due_users(session: AsyncSession, now: datetime) -> Sequence[User]:
    """The bound tenant's overdue people, locked for this transaction.

    Three conjuncts, each of which a case in `test_access_leavers.py` plants a
    row against on its own: `status = 'active'` (so somebody offboarded by
    hand last month is not swept again), `end_date IS NOT NULL` (so a
    permanent employee is never due — the case that catches a Python-side
    filter with a date fallback, which would offboard a whole tenant on its
    first tick) and the strict boundary itself.

    `now.date()` rather than `now`: `end_date` is a DATE, and comparing it to
    a timestamp would widen the stored day to midnight and skip the person
    whose last day was yesterday until the day after.

    `FOR UPDATE` through the ORM, so the tenant guard still injects its filter
    and the rows are held to the end of the caller's transaction. It is not
    what decision I relies on — a person turned `leaver` between this read and
    their own offboard is answered by the `AlreadyLeaverError` catch below,
    and the case pinning that outcome makes no kill claim for this lock.

    Ordered by end date then id so two runs over the same rows write their
    events in the same order.
    """
    result = await session.execute(
        select(User)
        .where(
            User.status == _ACTIVE_USER_STATUS,
            User.end_date.is_not(None),
            User.end_date < now.date(),
        )
        .order_by(User.end_date, User.id)
        .with_for_update()
    )
    return result.scalars().all()


async def sweep_leavers(session: AsyncSession, *, now: datetime) -> list[User]:
    """Offboard everyone whose last day has passed. The caller commits.

    Each person is offboarded with their OWN `end_date`, read off the row and
    never `now.date()`: the date somebody left is a fact about them, not about
    the day a job noticed, and reusing the tick's date would rewrite that
    history on the first run after a delayed deployment (pinned by
    `test_access_leavers.py::test_each_swept_user_keeps_their_own_end_date`,
    which sweeps two people ten days apart).

    `AlreadyLeaverError` is caught and ONLY that (decision I). It is raised
    after `lock_user` and before any write, so the session is clean and the
    end state that person's offboard wanted already holds; every other failure
    raises to the runner, which deletes the lock key and retries on the next
    tick (21a decision C). Catching it is what keeps one person offboarded by
    hand this morning from rolling back every other departure of the day
    (`::test_a_user_turned_leaver_mid_sweep_is_skipped_and_the_rest_still_go`).

    The returned list is the people actually offboarded, so the skipped ones
    are not in it. `jobs.py` drops it: the audit events are the record.
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
            # Decision I: somebody else offboarded this person between the
            # read and here. The end state this loop wanted already holds, so
            # it costs that person and not the tenant's whole sweep.
            continue
        offboarded.append(user)

    return offboarded


__all__ = ["LEAVER_SWEEP_ACTOR_REF", "sweep_leavers"]
