"""User, department and system reads and locks.

Another tenant's row reads as missing, so an id reveals nothing.
"""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.models import Department, System, User


async def find_user_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(User.email == email))
    return result.scalars().one_or_none()


async def find_user_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalars().one_or_none()


async def lock_user(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    """The user, locked for this transaction.

    Makes two transfers of one person take turns; otherwise both read the old
    department and both add the new roles.
    """
    result = await session.execute(select(User).where(User.id == user_id).with_for_update())
    return result.scalars().one_or_none()


async def lock_department(session: AsyncSession, department_id: uuid.UUID) -> Department | None:
    """The department, locked, so two openings of one review take turns."""
    result = await session.execute(
        select(Department).where(Department.id == department_id).with_for_update()
    )
    return result.scalars().one_or_none()


async def lock_system(session: AsyncSession, system_id: uuid.UUID) -> System | None:
    """The system, locked, like `lock_department`."""
    result = await session.execute(select(System).where(System.id == system_id).with_for_update())
    return result.scalars().one_or_none()


async def lock_due_users(session: AsyncSession, now: datetime) -> list[User]:
    """Active people whose last day has passed, locked.

    Compares with `now.date()`: `end_date` is a date, and a timestamp would
    delay the sweep by a day.
    """
    from helpdesk.shared.policy.types import TargetStatus

    result = await session.execute(
        select(User)
        .where(
            User.status == TargetStatus.ACTIVE.value,
            User.end_date.is_not(None),
            User.end_date < now.date(),
        )
        .order_by(User.end_date, User.id)
        .with_for_update()
    )
    return list(result.scalars().all())


async def fetch_all_departments(session: AsyncSession) -> list[Department]:
    """Every department in a fixed order, so reruns write the same audit sequence."""
    result = await session.execute(select(Department).order_by(Department.name, Department.id))
    return list(result.scalars().all())


async def fetch_systems_by_sensitivity(
    session: AsyncSession, sensitivities: frozenset[str]
) -> list[System]:
    """Systems with one of these sensitivities, in a fixed order."""
    result = await session.execute(
        select(System)
        .where(System.sensitivity.in_(sorted(sensitivities)))
        .order_by(System.name, System.id)
    )
    return list(result.scalars().all())


async def fetch_active_it_admins(session: AsyncSession) -> list[User]:
    """Active IT admins; leavers keep the role on their row, so status is checked."""
    from helpdesk.shared.auth.rbac import Principal
    from helpdesk.shared.policy.types import TargetStatus

    result = await session.execute(
        select(User)
        .where(
            User.status == TargetStatus.ACTIVE.value,
            User.principals.contains([Principal.IT_ADMIN.value]),
        )
        .order_by(User.email, User.id)
    )
    return list(result.scalars().all())
