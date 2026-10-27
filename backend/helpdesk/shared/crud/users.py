"""User, department and system reads and locks.

Another tenant's row reads as missing, so an id reveals nothing.
"""

import uuid
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.core.errors import NotFoundError
from helpdesk.shared.core.naming import new_gateway_user_id
from helpdesk.shared.models import Department, System, User


async def find_user_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(User.email == email))
    return result.scalars().one_or_none()


async def find_user_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalars().one_or_none()


async def gateway_user_id_for(session: AsyncSession, user_id: uuid.UUID) -> str:
    """The user's random gateway id, made on first use and never changed.

    One `UPDATE ... WHERE gateway_user_id IS NULL` makes it, so two first
    uses at once wait on the row lock and both read back the one stored id. Does not commit.
    """
    await session.execute(
        update(User)
        .where(User.id == user_id, User.gateway_user_id.is_(None))
        .values(gateway_user_id=new_gateway_user_id())
        .execution_options(synchronize_session=False)
    )
    result = await session.execute(select(User.gateway_user_id).where(User.id == user_id))
    stored = result.scalar_one_or_none()
    if stored is None:
        raise NotFoundError("no such user in this tenant")
    return stored


async def list_user_display_names(session: AsyncSession) -> list[str]:
    """Every person's display name in the session's own tenant, for the
    spelling fix. Whole rows, so the tenant guard scopes them."""
    result = await session.execute(select(User))
    return [user.display_name for user in result.scalars().all()]


async def list_users_in_department_named(
    session: AsyncSession, tenant_id: uuid.UUID, department_name: str
) -> list[User]:
    """The tenant's users whose department has this name, case ignored."""
    result = await session.execute(
        select(User)
        .join(Department, Department.id == User.department_id)
        .where(
            User.tenant_id == tenant_id,
            func.lower(Department.name) == department_name.lower(),
        )
    )
    return list(result.scalars().all())


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

    return await fetch_active_holders(session, Principal.IT_ADMIN.value)


async def fetch_active_holders(session: AsyncSession, principal: str) -> list[User]:
    """Active people holding one principal (`it_admin`, `hr`), in a fixed order.

    Leavers keep their principals on the row, so status is checked.
    """
    from helpdesk.shared.policy.types import TargetStatus

    result = await session.execute(
        select(User)
        .where(
            User.status == TargetStatus.ACTIVE.value,
            User.principals.contains([principal]),
        )
        .order_by(User.email, User.id)
    )
    return list(result.scalars().all())
