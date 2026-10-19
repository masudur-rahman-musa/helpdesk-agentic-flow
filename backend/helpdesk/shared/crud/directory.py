"""Directory reads: department role templates, live grant role ids, and the role catalogue."""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.crud.grants import ACTIVE_GRANT_STATUS
from helpdesk.shared.models import Grant, Role, RoleTemplate, System


async def fetch_role_templates_for_department(
    session: AsyncSession, department_id: uuid.UUID
) -> frozenset[uuid.UUID]:
    """Role ids the department's template gives; another tenant's department reads empty."""
    result = await session.execute(
        select(RoleTemplate).where(RoleTemplate.department_id == department_id)
    )
    return frozenset(template.role_id for template in result.scalars())


async def fetch_live_role_ids_for_user(
    session: AsyncSession, user_id: uuid.UUID, now: datetime
) -> frozenset[uuid.UUID]:
    """Role ids of this user's live grants: active, not revoked, not expired."""
    result = await session.execute(
        select(Grant).where(
            Grant.user_id == user_id,
            Grant.status == ACTIVE_GRANT_STATUS,
            Grant.revoked_at.is_(None),
            or_(Grant.expires_at.is_(None), Grant.expires_at > now),
        )
    )
    return frozenset(grant.role_id for grant in result.scalars())


async def list_role_catalogue(
    session: AsyncSession, system_slug: str | None, limit: int
) -> Sequence[tuple[Role, System]]:
    """The tenant's roles with their systems, by system then role slug.

    Whole rows are selected so the tenant guard scopes both tables. The
    system filter is an exact slug match.
    """
    query = select(Role, System).join(System, System.id == Role.system_id)
    if system_slug is not None:
        query = query.where(System.slug == system_slug)
    result = await session.execute(query.order_by(System.slug, Role.slug).limit(limit))
    return [(role, system) for role, system in result.all()]
