"""The two expiry sweeps: one ends access, one warns before it ends.

Expiry is not revocation: nobody decided it, so the row keeps `revoked_at`
null and only its status moves. A grant is expired when `expires_at <= now`,
the exact opposite of `is_live_grant`, so no instant leaves it in neither.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.audit import AuditEventInput, append_event
from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.core.observability.metrics import record_expiries
from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.grants import fetch_grants_to_warn, lock_lapsed_grants
from helpdesk.shared.crud.users import find_user_by_id
from helpdesk.shared.models import Department, Grant, Role, User
from helpdesk.shared.models.notification import NotificationKind
from helpdesk.shared.notifications.notify import notify

GRANT_EXPIRED_EVENT_TYPE = "grant.expired"
"""Its own event type, so an auditor can tell access that ran out from access someone ended."""

EXPIRED_GRANT_STATUS = "expired"

_ACTOR_KIND = "system"
"""Nobody decided this, so the event must not read as a person's call."""

_SUBJECT_TYPE = "grant"

_HOLDER_TITLE = "Your access expired"
_MANAGER_TITLE = "A team member's access expired"

EXPIRY_WARNING_HORIZON_DAYS = 14
"""The warning band is `(now, now + 14 days]`; a grant at `now` is being expired instead."""

_WARNING_TITLE = "Your access is expiring soon"
"""Fixed, so a long role name can never push it past `notify`'s title cap."""


def _when(moment: datetime) -> str:
    """The expiry instant as a person reads it, in UTC."""
    return moment.strftime("%Y-%m-%d %H:%M UTC")


async def _lock_lapsed_grants(session: AsyncSession, now: datetime) -> Sequence[Grant]:
    """The bound tenant's lapsed grants, locked, so two overlapping runs expire each once."""
    return await lock_lapsed_grants(session, now)


async def _department_manager(session: AsyncSession, holder: User) -> uuid.UUID | None:
    """The holder's department manager, or None when there is nobody to tell."""
    if holder.department_id is None:
        return None
    department = await Repository(session, Department).get(holder.department_id)
    return None if department is None else department.manager_user_id


async def _notify_recipients(session: AsyncSession, grant: Grant, expires_at: datetime) -> None:
    """Tell the holder, and their manager unless there is none or it is the holder."""
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

    One event per grant. The counter moves once after the writes, so a
    rolled-back row is never counted.
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
    """Grants inside the warning band not yet warned about.

    Leavers are left out, since their access is about to be removed. The earlier
    warning check matches on kind and grant id, so a past expiry notice does not
    block a warning.
    """
    horizon = now + timedelta(days=EXPIRY_WARNING_HORIZON_DAYS)
    return await fetch_grants_to_warn(session, now, horizon)


async def warn_expiring_grants(session: AsyncSession, *, now: datetime) -> list[Grant]:
    """Warn the holder of every grant about to lapse. The caller commits.

    No audit event: nothing anyone holds has changed.
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
