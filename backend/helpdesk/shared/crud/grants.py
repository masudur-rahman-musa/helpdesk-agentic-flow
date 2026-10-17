"""Grant reads, the meaning of a "live" grant, and the row locks.

"Live" is defined once here, so no tool can disagree with the reads about it.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.models import Grant, Role, System, User

ACTIVE_GRANT_STATUS = "active"

REVOKED_GRANT_STATUS = "revoked"
"""Defined here, not in `access.revoke`, to avoid an import cycle; that module re-exports it."""


class ActiveGrant(BaseModel):
    """One live grant with the role and system named, so callers need no second read."""

    model_config = ConfigDict(frozen=True)

    grant_id: uuid.UUID
    role_slug: str
    role_name: str
    privilege: str
    system_slug: str
    system_name: str
    granted_at: datetime
    expires_at: datetime | None


async def list_active_grants_for_user(
    session: AsyncSession, user_id: uuid.UUID, now: datetime
) -> Sequence[ActiveGrant]:
    """A user's live grants, by system then role slug.

    `expires_at` is the first moment the grant is gone, hence the strict `>`.
    Whole rows are selected so the tenant guard scopes all three tables.
    """
    result = await session.execute(
        select(Grant, Role, System)
        .join(Role, Role.id == Grant.role_id)
        .join(System, System.id == Role.system_id)
        .where(
            Grant.user_id == user_id,
            Grant.status == ACTIVE_GRANT_STATUS,
            Grant.revoked_at.is_(None),
            or_(Grant.expires_at.is_(None), Grant.expires_at > now),
        )
        .order_by(System.slug, Role.slug)
    )
    return [
        ActiveGrant(
            grant_id=grant.id,
            role_slug=role.slug,
            role_name=role.name,
            privilege=role.privilege,
            system_slug=system.slug,
            system_name=system.name,
            granted_at=grant.granted_at,
            expires_at=grant.expires_at,
        )
        for grant, role, system in result.all()
    ]


async def list_granted_role_ids(session: AsyncSession, user_id: uuid.UUID) -> frozenset[uuid.UUID]:
    """Every role this user holds as a review sees it, lapsed rows included.

    A lapsed grant the sweep has not reached still counts toward a duty
    conflict. Not limited to the review's scope, since the other half of a
    conflict is nearly always in a different system.
    """
    result = await session.execute(
        select(Grant.role_id).where(Grant.user_id == user_id, *_granted_clauses())
    )
    return frozenset(result.scalars().all())


def is_live_grant(grant: Grant, now: datetime) -> bool:
    """The Python twin of the SQL liveness filter, kept beside it so they agree."""
    return (
        grant.status == ACTIVE_GRANT_STATUS
        and grant.revoked_at is None
        and (grant.expires_at is None or grant.expires_at > now)
    )


def is_active_grant(grant: Grant) -> bool:
    """Active and not revoked, with no expiry test: the Python twin of `_granted_clauses`.

    A lapsed grant stays active until the sweep runs, and a review must still revoke it.
    """
    return grant.status == ACTIVE_GRANT_STATUS and grant.revoked_at is None


class ScopedGrant(BaseModel):
    """One grant in a review's scope, with the holder's department for flagging."""

    model_config = ConfigDict(frozen=True)

    grant_id: uuid.UUID
    user_id: uuid.UUID
    role_id: uuid.UUID
    holder_department_id: uuid.UUID | None
    expires_at: datetime | None


def _granted_clauses() -> tuple[Any, ...]:
    """Active and not revoked, with no expiry test.

    A grant past its expiry stays `active` until the sweep runs. Reviews must
    see it, since that is exactly what they flag as `stale`.
    """
    return (Grant.status == ACTIVE_GRANT_STATUS, Grant.revoked_at.is_(None))


def _live_grant_clauses(now: datetime) -> tuple[Any, ...]:
    return (*_granted_clauses(), or_(Grant.expires_at.is_(None), Grant.expires_at > now))


async def count_live_grants(session: AsyncSession, now: datetime) -> int:
    """How many live grants the tenant holds now, as one count.

    The tenant filter is written out because the session guard only covers
    queries that load whole rows, and a count loads none.
    """
    result = await session.execute(
        select(func.count(Grant.id)).where(
            Grant.tenant_id == bound_tenant_id(session), *_live_grant_clauses(now)
        )
    )
    return int(result.scalar_one())


async def _list_scoped_grants(
    session: AsyncSession, scope_clause: ColumnElement[bool], *, join_role: bool
) -> Sequence[ScopedGrant]:
    """Shared body of the two scope listings, grouped by person in a fixed order.

    Uses `_granted_clauses`, so lapsed rows are included for the `stale` flag.
    """
    statement = select(Grant, User).join(User, User.id == Grant.user_id)
    if join_role:
        statement = statement.join(Role, Role.id == Grant.role_id)
    result = await session.execute(
        statement.where(scope_clause, *_granted_clauses()).order_by(
            User.email, Grant.granted_at, Grant.id
        )
    )
    return [
        ScopedGrant(
            grant_id=grant.id,
            user_id=grant.user_id,
            role_id=grant.role_id,
            holder_department_id=user.department_id,
            expires_at=grant.expires_at,
        )
        for grant, user in result.all()
    ]


async def list_active_grants_in_department(
    session: AsyncSession, department_id: uuid.UUID
) -> Sequence[ScopedGrant]:
    """Grants of the department's current members, in any system."""
    return await _list_scoped_grants(session, User.department_id == department_id, join_role=False)


async def list_active_grants_for_system(
    session: AsyncSession, system_id: uuid.UUID
) -> Sequence[ScopedGrant]:
    """Grants for one system's roles, whoever holds them."""
    return await _list_scoped_grants(session, Role.system_id == system_id, join_role=True)


async def lock_grant_row(session: AsyncSession, grant_id: uuid.UUID) -> Grant | None:
    """The grant, locked; None if missing or another tenant's.

    Not filtered on liveness, so a reviewer can still act on a lapsed grant.
    Locking the grant, not the review item, makes two decisions take turns and
    shares one lock order with revocation, so the two cannot deadlock.
    """
    result = await session.execute(select(Grant).where(Grant.id == grant_id).with_for_update())
    return result.scalars().one_or_none()


async def lock_live_grants_for_role(
    session: AsyncSession, user_id: uuid.UUID, role_id: uuid.UUID, now: datetime
) -> Sequence[Grant]:
    """All live grants for one (user, role), locked, oldest first.

    A user can hold several rows for one role, so revoking only one would leave
    access alive. The lock makes two revocations of the same role take turns.
    """
    result = await session.execute(
        select(Grant)
        .where(
            Grant.user_id == user_id,
            Grant.role_id == role_id,
            Grant.status == ACTIVE_GRANT_STATUS,
            Grant.revoked_at.is_(None),
            or_(Grant.expires_at.is_(None), Grant.expires_at > now),
        )
        .order_by(Grant.granted_at, Grant.id)
        .with_for_update()
    )
    return result.scalars().all()


async def lock_lapsed_grants(session: AsyncSession, now: datetime) -> list[Grant]:
    """Active, unrevoked grants past their expiry, locked.

    Two overlapping sweeps expire a grant once: the second finds it no longer active.
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
    return list(result.scalars().all())


async def fetch_grants_to_warn(
    session: AsyncSession, now: datetime, horizon: datetime
) -> list[Grant]:
    """Grants expiring between `now` and `horizon` whose holder has not been warned.

    The check against past warnings keeps the daily job from warning twice.
    Leavers are skipped.
    """
    from sqlalchemy import String, cast, exists

    from helpdesk.shared.models.notification import Notification, NotificationKind
    from helpdesk.shared.policy.types import TargetStatus

    _LEAVER_STATUS = TargetStatus.LEAVER.value

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
            User.status != _LEAVER_STATUS,
            ~exists(already_warned),
        )
        .order_by(Grant.expires_at, Grant.id)
    )
    return list(result.scalars().all())


async def lock_live_grants_for_request(
    session: AsyncSession, request_id: uuid.UUID, now: datetime
) -> Sequence[Grant]:
    """All live grants one request produced, locked, oldest first.

    A rollback undoes a whole request, which may hold several rows. The lock
    keeps a parallel revocation from changing the set mid-rollback. Oldest
    first matters: the rollback window is measured from the first grant.
    """
    result = await session.execute(
        select(Grant)
        .where(Grant.request_id == request_id, *_live_grant_clauses(now))
        .order_by(Grant.granted_at, Grant.id)
        .with_for_update()
    )
    return result.scalars().all()
