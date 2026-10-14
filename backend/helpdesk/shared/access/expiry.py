"""The two expiry sweeps: the one that ends access, and the one that warns (the design).

`expire_grants` ends a grant whose date arrived; `warn_expiring_grants` tells its holder a fortnight before that happens. They live together
because they read the same columns under complementary rules — the warning band
stops strictly above the instant the expiry sweep starts at, so no grant is
warned about and ended in one tick (`EXPIRY_WARNING_HORIZON_DAYS`).

Expiry is NOT revocation, and that is the whole shape of this module. A
revocation is somebody's decision: it carries a revoker, a reason and a
`revoked_at`, and it goes through `access.revoke.revoke_grant_row`. An expiry
is the absence of a decision — the date the grant was issued with simply
arrived — so the row keeps `revoked_at` and `revoke_reason` null and only its
status moves. A sweep that reused the revocation path would write a revocation
with no revoker into the audit log, which is a record of something that never
happened. Pinned by
`test_access_expiry.py::test_a_lapsed_grant_becomes_expired_without_being_revoked`,
which asserts both null fields beside the status.

The boundary is `expires_at <= now`, the exact
complement of `core.repository.is_live_grant`'s `expires_at > now`, so no
instant exists at which a grant is neither live nor expired. the design writes
`<`; the deviation is decision D's, and it is pinned by
`test_access_expiry.py::test_a_grant_expiring_at_exactly_now_is_expired`.

`now` is passed in by the runner rather than read from a clock here, the same
shape `revoke_role_access` uses: the read that selected the lapsed set and the
event that records it answer for one instant, so a grant that lapses between
the two cannot be recorded under a time it was still live.

Everything runs in the caller's transaction, over the BOUND tenant only. The
runner opens one `tenant_session` per tenant and calls this once inside each,
so a statement built outside the session's scope would expire every tenant's
grants on the first tenant's tick — refused by
`test_access_expiry.py::test_another_tenants_lapsed_grant_is_untouched`.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import String, cast, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.access.lifecycle import LEAVER_STATUS
from helpdesk.shared.audit import AuditEventInput, append_event
from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.core.repository import ACTIVE_GRANT_STATUS, Repository, find_user_by_id
from helpdesk.shared.models import Department, Grant, Notification, Role, User
from helpdesk.shared.models.notification import NotificationKind
from helpdesk.shared.notifications.notify import notify
from helpdesk.shared.obs.metrics import record_expiries

GRANT_EXPIRED_EVENT_TYPE = "grant.expired"
"""the design's name for "the grant's own date arrived".

Its own event type rather than `grant.revoked` with a system actor: an
auditor reading the log has to be able to separate access somebody ended from
access that ran out, and the two answer different questions about a tenant.
Pinned with its subject and every payload key by
`test_access_expiry.py::test_one_grant_expired_event_per_row_names_the_grant_and_its_expiry`."""

EXPIRED_GRANT_STATUS = "expired"
"""What `grants.status` reads once this sweep has been through.

Beside `ACTIVE_GRANT_STATUS` and `REVOKED_GRANT_STATUS` in spirit but declared
here, because this module is the only writer of the value. Pinned by the same
test as the null `revoked_at`."""

_ACTOR_KIND = "system"
"""Nobody decided this. `revoke.py`'s events are `user`, because the design gives
`revoke_access` five principals; a sweep has no principal at all, and filing
it as one would make an automatic expiry read as a person's call."""

_SUBJECT_TYPE = "grant"

_HOLDER_TITLE = "Your access expired"
_MANAGER_TITLE = "A team member's access expired"

EXPIRY_WARNING_HORIZON_DAYS = 14
"""How far ahead `warn_expiring_grants` looks.

The band is `(now, now + 14 days]` — inclusive at the top, strict at the
bottom, because a grant at exactly `now` is `expire_grants`' row and telling
somebody to renew access in the same tick that ends it puts two contradictory
entries in one bell. Both halves of the top boundary and the bottom one are
pinned exactly by
`test_access_expiry_warnings.py::test_a_grant_expiring_at_exactly_the_horizon_is_warned`,
`::test_a_grant_one_microsecond_past_the_horizon_is_not_warned` and
`::test_a_grant_expiring_at_exactly_now_is_not_warned`; the value itself by
`::test_the_expiry_warning_constants_are_exactly_the_planned_values`."""

_WARNING_TITLE = "Your access is expiring soon"
"""The whole title of every warning, the same for every grant.

Fixed rather than composed, so nothing a tenant can name reaches `notify`'s
200-character title cap: `roles.name` is a 256-character column, and a warning
that put the role in the title would stop warning at all — with a typed
`VALIDATION` raised to the runner — the day somebody names a role longer than
that. The role goes in the BODY, under the 2000-character cap. Pinned by
`test_access_expiry_warnings.py::test_the_role_name_is_in_the_body_and_the_title_is_the_fixed_literal`,
which plants a role name at exactly the column width."""


def _when(moment: datetime) -> str:
    """The expiry instant as a person reads it, in UTC."""
    return moment.strftime("%Y-%m-%d %H:%M UTC")


async def _lock_lapsed_grants(session: AsyncSession, now: datetime) -> Sequence[Grant]:
    """The bound tenant's lapsed grants, locked for this transaction.

    Four conjuncts, each of which a case in `test_access_expiry.py` plants a
    row against: `status = 'active'` (so an already-expired row is not swept
    again every five minutes, writing a duplicate event each time),
    `revoked_at IS NULL` (so a revocation is not rewritten as an expiry),
    `expires_at IS NOT NULL` (so a permanent grant is never touched) and the
    boundary itself.

    `FOR UPDATE` through the ORM, so the tenant guard still injects its filter
    and the rows are held to the end of the caller's transaction. Two runners
    that overlap therefore expire one grant once — the loser's set comes back
    empty on the status test. The OUTCOME is pinned by
    `test_access_expiry.py::test_two_callers_racing_one_overdue_grant_expire_it_once`,
    whose docstring makes no kill claim for the lock itself.

    Ordered by expiry then id so two runs over the same rows write their
    events in the same order.
    """
    result = await session.execute(
        select(Grant)
        .where(
            Grant.status == ACTIVE_GRANT_STATUS,
            Grant.revoked_at.is_(None),
            Grant.expires_at.is_not(None),
            Grant.expires_at <= now,
        )
        .order_by(Grant.expires_at, Grant.id)
        .with_for_update()
    )
    return result.scalars().all()


async def _department_manager(session: AsyncSession, holder: User) -> uuid.UUID | None:
    """The holder's department manager, or None when there is nobody to tell."""
    if holder.department_id is None:
        return None
    department = await Repository(session, Department).get(holder.department_id)
    return None if department is None else department.manager_user_id


async def _notify_recipients(session: AsyncSession, grant: Grant, expires_at: datetime) -> None:
    """One row for the holder, one for their manager.

    Two rows rather than one carrying a list of recipients: every read of
    `notifications` is one person's unread list. The manager row is skipped
    when the department has no manager and when the manager IS the holder — a
    department head would otherwise be told twice about their own grant, which
    only the second test can catch. Both skips are pinned by
    `test_access_expiry.py::test_a_department_with_no_manager_notifies_the_holder_only`
    and `::test_a_holder_who_is_their_own_manager_is_notified_once`.

    The role is named rather than referenced by id, because a person reading
    "your access to a uuid ended" learns nothing. Its name is bounded by the
    `roles.name` column and lands in the BODY, never the title, so the
    composed text cannot reach `notify`'s 200-character title cap.
    """
    holder = await find_user_by_id(session, grant.user_id)
    if holder is None:  # pragma: no cover - the FK makes this unreachable
        return
    role = await Repository(session, Role).get(grant.role_id)
    role_label = str(grant.role_id) if role is None else role.name
    when = _when(expires_at)

    await notify(
        session,
        user_id=holder.id,
        kind=NotificationKind.GRANT_EXPIRED,
        title=_HOLDER_TITLE,
        body=(
            f"Your access to {role_label} ended on {when} because the grant reached "
            "its expiry date. Ask for it again if you still need it."
        ),
        subject_type=_SUBJECT_TYPE,
        subject_id=str(grant.id),
    )

    manager_id = await _department_manager(session, holder)
    if manager_id is None or manager_id == holder.id:
        return
    await notify(
        session,
        user_id=manager_id,
        kind=NotificationKind.GRANT_EXPIRED,
        title=_MANAGER_TITLE,
        body=(
            f"{holder.display_name}'s access to {role_label} ended on {when} because "
            "the grant reached its expiry date."
        ),
        subject_type=_SUBJECT_TYPE,
        subject_id=str(grant.id),
    )


async def expire_grants(session: AsyncSession, *, now: datetime) -> list[Grant]:
    """Expire every lapsed grant of the bound tenant. The caller commits.

    One `grant.expired` event and up to two notifications per ROW, never one
    event per sweep: an auditor asks which grant went and when it was due, and
    a batch event answers neither. The payload is exactly `user_id`, `role_id`
    and `expires_at`, all read off the row — a `reason` or a `revoked_count`
    would describe this as a revocation (a project rule, pinned by
    `test_access_expiry.py::test_one_grant_expired_event_per_row_names_the_grant_and_its_expiry`).

    The counter moves once, by the size of the set, after the writes: a
    per-row increment inside the loop would count rows a later failure rolled
    back as expiries that happened.
    """
    tenant_id = bound_tenant_id(session)
    grants = await _lock_lapsed_grants(session, now)

    for grant in grants:
        expires_at = grant.expires_at
        if expires_at is None:  # pragma: no cover - the filter excludes these
            continue
        grant.status = EXPIRED_GRANT_STATUS
        await session.flush()
        await append_event(
            session,
            tenant_id,
            AuditEventInput(
                occurred_at=now,
                actor_kind=_ACTOR_KIND,
                actor_ref=None,
                session_id=None,
                trace_id=None,
                event_type=GRANT_EXPIRED_EVENT_TYPE,
                subject_type=_SUBJECT_TYPE,
                subject_id=str(grant.id),
                payload={
                    "user_id": str(grant.user_id),
                    "role_id": str(grant.role_id),
                    "expires_at": expires_at.isoformat(),
                },
            ),
        )
        await _notify_recipients(session, grant, expires_at)

    record_expiries(len(grants))
    return list(grants)


async def _grants_to_warn(session: AsyncSession, now: datetime) -> Sequence[Grant]:
    """The bound tenant's grants inside the warning band that have not been told.

    Six conjuncts and an anti-join, each of which a case in
    `test_access_expiry_warnings.py` plants a row against on its own. Three
    are `_lock_lapsed_grants`' own — `status = 'active'`, `revoked_at IS NULL`,
    `expires_at IS NOT NULL` — and the band is the other two. The join to
    `users` carries the fifth, decision C: a `leaver` is never warned, because
    the sweep in `access/leavers.py` is about to take everything off them and
    a note asking them to renew it is the system contradicting itself. Pinned
    by `::test_a_grant_held_by_a_leaver_is_not_warned`, which is the only case
    a filter written over `grants` alone cannot refuse.

    The anti-join IS the idempotence (decision A): the band is wide so that a
    warning stays owed until it has been given, which means the same grant is
    in it on every daily tick for a fortnight. Matching on kind AND
    `subject_id` rather than the subject alone is what keeps a grant that has
    already been expired once — `expire_grants` writes `grant_expired` against
    the very same id — from silently never being warned again
    (`::test_a_grant_already_carrying_an_expired_notification_is_still_warned`).

    No `FOR UPDATE`, unlike the lapsed set: nothing here moves a status, so
    two runners overlapping the same day cost a duplicate bell entry and
    nothing else.

    Ordered by expiry then id so two runs over the same rows write in the same
    order.
    """
    horizon = now + timedelta(days=EXPIRY_WARNING_HORIZON_DAYS)
    already_warned = select(Notification.id).where(
        Notification.kind == NotificationKind.GRANT_EXPIRING.value,
        Notification.subject_id == cast(Grant.id, String),
    )
    result = await session.execute(
        select(Grant)
        .join(User, User.id == Grant.user_id)
        .where(
            Grant.status == ACTIVE_GRANT_STATUS,
            Grant.revoked_at.is_(None),
            Grant.expires_at.is_not(None),
            Grant.expires_at > now,
            Grant.expires_at <= horizon,
            User.status != LEAVER_STATUS,
            ~exists(already_warned),
        )
        .order_by(Grant.expires_at, Grant.id)
    )
    return result.scalars().all()


async def warn_expiring_grants(session: AsyncSession, *, now: datetime) -> list[Grant]:
    """Warn the holder of every grant about to lapse. The caller commits.

    The holder ONLY (decision C), where `expire_grants` tells the holder and
    their manager: the design names both for the expiry and only a notification
    for the warning, and a renewal is the holder's to ask for. Pinned as the
    row set by
    `test_access_expiry_warnings.py::test_a_grant_expiring_inside_the_horizon_warns_its_holder_once`.

    One row per GRANT, carrying `subject_type` `grant` and the grant's own id:
    decision B keeps the "request renewal" link out of this library — a base
    URL is deployment configuration — so these two fields are the only thing
    row 23's route has to compose one from, and a subject naming the user or
    the role would link nowhere. Same test.

    No audit event and no counter. Nothing here changes what anybody holds;
    the record a warning leaves is the notification itself, and the design names
    no metric for this job.

    `now` is passed in, so the band that selected the set and the sentence
    written into the body answer for one instant.
    """
    grants = await _grants_to_warn(session, now)

    for grant in grants:
        expires_at = grant.expires_at
        if expires_at is None:  # pragma: no cover - the filter excludes these
            continue
        role = await Repository(session, Role).get(grant.role_id)
        role_label = str(grant.role_id) if role is None else role.name
        await notify(
            session,
            user_id=grant.user_id,
            kind=NotificationKind.GRANT_EXPIRING,
            title=_WARNING_TITLE,
            body=(
                f"Your access to {role_label} expires on {_when(expires_at)}. "
                "Ask for it again before then if you still need it."
            ),
            subject_type=_SUBJECT_TYPE,
            subject_id=str(grant.id),
        )

    return list(grants)
