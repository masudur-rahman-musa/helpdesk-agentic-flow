"""The two review jobs: who is due a review this quarter, and who is late (the design).

`generate_reviews` decides NOTHING about a review. It answers one question —
which scopes of this tenant are due this quarter — and hands each one to
`open_review`, which already owns the reviewer resolution, the item pass, the
flagging and the audit event. That is why this module is short: a generator
that wrote reviews itself would be a second writer with its own order and its
own idea of what a flag means, and the two would drift.

`send_review_reminders` is the other half, and it writes nothing but
notifications. No audit event: nothing here changes what anybody holds or what
any reviewer decided, and the design names no event for a reminder.

It sits beside `reviews.py` rather than inside it, the
shape `leavers.py` has next to `lifecycle.py`: `reviews.py` owns the review
ENGINE, and the schedule that calls it is a different question in an already
long file. `quarter_period` and `check_period` are the exception and stay
there, because `period` is that engine's own field.

Everything runs in the caller's transaction, over the BOUND tenant only: the
runner opens one `tenant_session` per tenant and calls these inside each, so a
statement built outside the session's scope would generate every tenant's
reviews on the first tenant's tick (refused by
`test_access_review_generation.py::test_another_tenants_scopes_are_never_generated_for`
and `test_access_review_reminders.py::test_another_tenants_overdue_review_is_never_reminded_about`).
One transaction per tenant also means a failure on the fifth scope rolls back
the first four — deliberate (plan §5), for `sweep_leavers`' reason: a partial
quarter whose audit log is half the reviews with no record of what was skipped
is worse than a retry a tick away.
"""

import logging
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.access.directory import DirectoryReader
from helpdesk.shared.access.reviews import (
    ReviewAlreadyOpenError,
    check_period,
    open_review,
    quarter_period,
)
from helpdesk.shared.access.revoke import SYSTEM_ACTOR_KIND
from helpdesk.shared.auth.rbac import Principal
from helpdesk.shared.core.repository import Repository
from helpdesk.shared.models import AccessReview, Department, ReviewStatus, System, User
from helpdesk.shared.models.notification import NotificationKind
from helpdesk.shared.notifications.notify import notify
from helpdesk.shared.policy.types import TargetStatus

logger = logging.getLogger(__name__)

REVIEW_DUE_DAYS = 14
"""How long a generated review gets before it counts as overdue (decision B).

`open_review`'s own default is a null `due_at`, and a review with no due date
can never be overdue — which would leave `send_review_reminders` below with
nothing to read at all and the design's reminder as dead code. The same horizon
as `EXPIRY_WARNING_HORIZON_DAYS`, so somebody reading the two features sees
one number rather than two. Pinned exactly by
`test_access_review_generation.py::test_the_review_generation_constants_are_exactly_the_planned_values`."""

GENERATED_SYSTEM_SENSITIVITIES = frozenset({"high", "critical"})
"""Which systems get a review of their own every quarter (the design).

Departments are generated for unconditionally; a system is only worth a
standing quarterly review when losing track of who holds it matters. Read as a
set rather than a comparison, so widening it is one edit here. Pinned as the
exact set by the constants case, and each excluded value has a case of its own
(`::test_a_system_of_this_sensitivity_is_never_generated_for`) so a widened
filter fails on exactly one row."""

REVIEW_GENERATION_ACTOR_REF = "scheduler:review_generation"
"""What `audit_events.actor_ref` reads for every `review.opened` this job causes.

The one string an auditor greps to separate the quarterly sweep's reviews from
a manager's, and the value the runbook prints. Beside `actor_kind = 'system'`,
never instead of it (decision D): a `user` kind with a system-shaped ref would
file an automatic sweep among people's decisions, which is the confusion
`LEAVER_SWEEP_ACTOR_REF` and `expire_grants`' own system actor were written to
avoid. Pinned, with the kind, by
`::test_every_review_opened_event_the_generator_causes_carries_the_system_actor`."""

REVIEW_ESCALATION_DAYS = 14
"""How far past `due_at` an open review goes before every `it_admin` hears of it.

the design's "14 days overdue", and the comparison is INCLUSIVE (decision J): at
exactly `now - 14 days` it escalates, because "14 days overdue" reads as "at
least 14 days" and the inclusive side is the one that fires on the day the
runbook says it will. The reminder band stays strict, because a review due
exactly now is due and not overdue. Both halves of both boundaries are pinned
to the microsecond in `test_access_review_reminders.py`, and the value itself
by `::test_the_review_reminder_constants_are_exactly_the_planned_values`."""

_SUBJECT_TYPE = "access_review"

_REMINDER_TITLE = "An access review is overdue"
"""The fixed title of every reminder, with no scope name in it.

The scope goes in the BODY on purpose: `departments.name` is a 256-character
column and `notify` refuses a title over 200 characters with a typed
`VALIDATION` error before any row is written, so a title composing the name
would stop this job dead — for the whole tenant, every day — the moment
somebody named a department longer than that. Pinned at exactly that length by
`test_access_review_reminders.py::test_the_scope_name_is_in_the_body_and_the_title_is_the_fixed_literal`."""

_ESCALATION_TITLE = "An access review is 14 days overdue"
"""The escalation's own title, fixed for `_REMINDER_TITLE`'s reason.

A different sentence from the reminder, because the two land in different
people's lists and an `it_admin` reading their bell has to be able to tell a
review they are being told about from one they own."""

_ACTIVE_USER_STATUS = TargetStatus.ACTIVE.value
"""Read off `TargetStatus`, like `leavers._ACTIVE_USER_STATUS`, so the status
this job escalates to and the one an offboard writes cannot drift apart."""

_IT_ADMIN = Principal.IT_ADMIN.value
"""the design's escalation audience, read off the RBAC principal rather than
spelled again, so a renamed principal moves both."""


def _when(moment: datetime) -> str:
    """The due instant as a person reads it, in UTC. `expiry.py`'s format."""
    return moment.strftime("%Y-%m-%d %H:%M UTC")


async def _departments_to_review(session: AsyncSession) -> Sequence[Department]:
    """Every department of the bound tenant, ordered so two runs agree.

    No sensitivity test and no filter but the tenant's: the design reviews every
    department each quarter, and the only scope skipped is one with no owner —
    decided in `generate_reviews` rather than here, so the two scope reads
    apply decision C in one place.
    """
    result = await session.execute(select(Department).order_by(Department.name, Department.id))
    return result.scalars().all()


async def _systems_to_review(session: AsyncSession) -> Sequence[System]:
    """The bound tenant's high and critical systems, ordered so two runs agree.

    The filter is `GENERATED_SYSTEM_SENSITIVITIES` and is applied in SQL rather
    than in Python, so a tenant with a thousand low-sensitivity systems does
    not read them all to discard them.
    """
    result = await session.execute(
        select(System)
        .where(System.sensitivity.in_(sorted(GENERATED_SYSTEM_SENSITIVITIES)))
        .order_by(System.name, System.id)
    )
    return result.scalars().all()


async def _scopes_already_reviewed(
    session: AsyncSession, period: str
) -> set[tuple[uuid.UUID | None, uuid.UUID | None]]:
    """Every (department, system) this tenant already has a review for, this period.

    ANY status, not just `open`, which is the difference between decision A
    working and not: a scope holding no active grants opens a review that is
    already `completed` (12c decision F), and `_refuse_if_open` blocks on
    `open` alone — so an empty department would collect one review and one
    `review.opened` event per DAY for a whole quarter, and criterion 5's "a
    second run opens no second review for any scope" could never be true.
    Recorded as a deviation from §2's prose, which named the caught
    `ReviewAlreadyOpenError` as the whole of the idempotence.

    One read per run rather than one per scope. It is scoped to `period`, so a
    completed review of LAST quarter blocks nothing — `reviews._OPEN_STATUSES`
    carries the same reasoning for the engine's own refusal.
    """
    result = await session.execute(select(AccessReview).where(AccessReview.period == period))
    return {(review.department_id, review.system_id) for review in result.scalars().all()}


async def generate_reviews(
    session: AsyncSession, directory: DirectoryReader, *, now: datetime
) -> list[AccessReview]:
    """Open this quarter's reviews for every due scope. The caller commits.

    The period is `quarter_period(now)` run through `check_period` before
    anything is read: it lands in a sized column and is the
    label every idempotence test in this module compares by, so a value that
    could not be stored has to be refused before the first review is built,
    not after four of them.

    `now` is read once by the caller and used for the period, the due date and
    every event, so every scope of one run answers for one instant. A run that
    straddles a quarter boundary therefore files all of its scopes in the
    outgoing quarter and the next run opens the new one's (plan §5).

    `ReviewAlreadyOpenError` is caught per scope and NOTHING else is: the
    already-open case is a scope somebody reviewed by hand this morning, and
    every other failure belongs to the runner, which deletes the job's lock key
    and retries on the next tick (21a decision C). Pinned both ways by
    `test_access_review_generation.py::test_a_scope_with_an_open_review_is_skipped_and_the_other_scope_still_opens`
    and `::test_a_failure_other_than_already_open_propagates_out_of_generate_reviews`.

    A scope whose owner is null is skipped and logged (decision C), never given
    a fallback reviewer: `access_reviews.reviewer_user_id` is not nullable, so
    something has to be chosen, and inventing one files a quarterly review
    against somebody who never owned the scope. The gap is meant to read as a
    scope with no review.
    """
    period = check_period(quarter_period(now))
    due_at = now + timedelta(days=REVIEW_DUE_DAYS)
    already = await _scopes_already_reviewed(session, period)

    opened: list[AccessReview] = []
    for department in await _departments_to_review(session):
        result = await _open_one(
            session,
            directory,
            department_id=department.id,
            system_id=None,
            owner_user_id=department.manager_user_id,
            period=period,
            due_at=due_at,
            already=already,
            now=now,
        )
        if result is not None:
            opened.append(result)

    for system in await _systems_to_review(session):
        result = await _open_one(
            session,
            directory,
            department_id=None,
            system_id=system.id,
            owner_user_id=system.owner_user_id,
            period=period,
            due_at=due_at,
            already=already,
            now=now,
        )
        if result is not None:
            opened.append(result)

    return opened


async def _open_one(
    session: AsyncSession,
    directory: DirectoryReader,
    *,
    department_id: uuid.UUID | None,
    system_id: uuid.UUID | None,
    owner_user_id: uuid.UUID | None,
    period: str,
    due_at: datetime,
    already: set[tuple[uuid.UUID | None, uuid.UUID | None]],
    now: datetime,
) -> AccessReview | None:
    """One scope's review, or None when the scope is skipped.

    Both scope kinds go through here so decision C's owner skip and decision
    A's period skip are written once: a department read and
    a system read that each applied them separately is exactly the pair where
    one gets fixed and the other does not.
    """
    if owner_user_id is None:
        logger.info(
            "review generation skipped a scope with no owner",
            extra={"department_id": department_id, "system_id": system_id, "period": period},
        )
        return None
    if (department_id, system_id) in already:
        return None

    try:
        result = await open_review(
            session,
            directory,
            department_id=department_id,
            system_id=system_id,
            period=period,
            opened_by_user_id=None,
            due_at=due_at,
            actor_ref=REVIEW_GENERATION_ACTOR_REF,
            actor_kind=SYSTEM_ACTOR_KIND,
            session_id=None,
            now=now,
        )
    except ReviewAlreadyOpenError:
        # A review opened for this scope and period between the read above and
        # here — another runner, or a manager on the tool. The end state this
        # loop wanted already holds, so it costs that scope and not the
        # tenant's whole quarter.
        return None
    return result.review


async def _overdue_reviews(session: AsyncSession, now: datetime) -> Sequence[AccessReview]:
    """The bound tenant's late reviews, ordered so two runs agree.

    Three conjuncts, each of which a case in `test_access_review_reminders.py`
    plants a row against on its own, so a dropped one fails exactly one case:
    `status = 'open'` (a completed review is answered and nagging about it
    would train reviewers to ignore the bell), `due_at IS NOT NULL` (a review
    opened through the tool takes `open_review`'s null default and is not late,
    it is undated), and the strict boundary itself.

    Strict `<` against `due_at` (decision J's other half): a review due exactly
    now is due, not overdue. Both halves pinned to the microsecond by
    `::test_a_review_due_at_exactly_now_is_not_overdue` and
    `::test_a_review_one_microsecond_past_its_due_date_is_overdue`.

    No `FOR UPDATE` and no dedupe: nothing here moves a status, the daily
    cadence gate IS the dedupe (decision E), and two runners overlapping a day
    cost a duplicate bell entry and nothing else (plan §5).
    """
    result = await session.execute(
        select(AccessReview)
        .where(
            AccessReview.status == ReviewStatus.OPEN.value,
            AccessReview.due_at.is_not(None),
            AccessReview.due_at < now,
        )
        .order_by(AccessReview.due_at, AccessReview.id)
    )
    return result.scalars().all()


async def _active_it_admins(session: AsyncSession) -> Sequence[User]:
    """Every active `it_admin` of the bound tenant, ordered so two runs agree.

    `status = 'active'` is the whole of the leaver test: somebody offboarded
    last month still carries their principals on the row, and escalating to
    them is a message nobody reads (`::test_a_leaver_holding_it_admin_is_never_escalated_to`).
    The principal is matched inside the array with `@>` rather than against the
    whole list, so a person holding `["employee", "it_admin"]` is included
    (`::test_a_review_exactly_fourteen_days_overdue_escalates_to_every_active_it_admin`
    plants exactly that pair).
    """
    result = await session.execute(
        select(User)
        .where(
            User.status == _ACTIVE_USER_STATUS,
            User.principals.contains([_IT_ADMIN]),
        )
        .order_by(User.email, User.id)
    )
    return result.scalars().all()


async def _scope_name(session: AsyncSession, review: AccessReview) -> str:
    """What the review is OF, as a person reads it.

    Falls back to the scope's id when the row cannot be read, so a reminder is
    still delivered for a scope renamed or removed under it: the notification
    is the only thing telling the reviewer they are late, and a missing name is
    a worse sentence, not a reason to write nothing.
    """
    if review.department_id is not None:
        department = await Repository(session, Department).get(review.department_id)
        return str(review.department_id) if department is None else department.name
    if review.system_id is not None:
        system = await Repository(session, System).get(review.system_id)
        return str(review.system_id) if system is None else system.name
    return "an unnamed scope"  # pragma: no cover - a review always names one scope


async def send_review_reminders(session: AsyncSession, *, now: datetime) -> list[AccessReview]:
    """Remind every late reviewer, and escalate the very late. The caller commits.

    One `review_overdue` row per overdue review, to the REVIEWER: the only
    person who can act on it, and the person the design names. The row carries
    `subject_type` `access_review` and the review's own id, which are the only
    two fields row 23's route can compose a link to the review queue from, so a
    subject naming the department or the reviewer would link nowhere.

    A review also past `now - REVIEW_ESCALATION_DAYS` writes, IN ADDITION, one
    `review_escalated` row to every active `it_admin` (decision F). the design
    reads "Overdue reviews → reminder to reviewer; 14 days overdue → escalate
    to `it_admin`" — an addition, not a substitution, and a reviewer who stops
    being told at day fourteen is the one person who can still act. Pinned by
    `::test_an_escalating_review_still_writes_the_reviewers_own_reminder`.

    The `it_admin` list is read at most ONCE per run and only when something
    actually escalates, rather than once per review: a tenant with two hundred
    late reviews would otherwise pay the same query two hundred times to send
    the same people two hundred rows.

    `now` is passed in, so the band that selected the set, the escalation
    boundary and the sentence written into the body all answer for one instant.
    """
    overdue = await _overdue_reviews(session, now)
    escalation_boundary = now - timedelta(days=REVIEW_ESCALATION_DAYS)
    admins: Sequence[User] | None = None

    for review in overdue:
        due_at = review.due_at
        if due_at is None:  # pragma: no cover - the filter excludes these
            continue
        scope_name = await _scope_name(session, review)
        await notify(
            session,
            user_id=review.reviewer_user_id,
            kind=NotificationKind.REVIEW_OVERDUE,
            title=_REMINDER_TITLE,
            body=(
                f"The {review.period} access review of {scope_name} was due on "
                f"{_when(due_at)} and is still open. Decide its remaining items."
            ),
            subject_type=_SUBJECT_TYPE,
            subject_id=str(review.id),
        )

        if due_at > escalation_boundary:
            continue
        if admins is None:
            admins = await _active_it_admins(session)
        for admin in admins:
            await notify(
                session,
                user_id=admin.id,
                kind=NotificationKind.REVIEW_ESCALATED,
                title=_ESCALATION_TITLE,
                body=(
                    f"The {review.period} access review of {scope_name} was due on "
                    f"{_when(due_at)} and has been open for {REVIEW_ESCALATION_DAYS} days "
                    "past its date."
                ),
                subject_type=_SUBJECT_TYPE,
                subject_id=str(review.id),
            )

    return list(overdue)


__all__ = [
    "GENERATED_SYSTEM_SENSITIVITIES",
    "REVIEW_DUE_DAYS",
    "REVIEW_ESCALATION_DAYS",
    "REVIEW_GENERATION_ACTOR_REF",
    "generate_reviews",
    "send_review_reminders",
]
