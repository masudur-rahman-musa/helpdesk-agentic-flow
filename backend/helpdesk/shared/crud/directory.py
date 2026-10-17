"""Directory reads: department role templates and live grant role ids."""

import uuid
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.crud.grants import ACTIVE_GRANT_STATUS
from helpdesk.shared.models import Grant, RoleTemplate


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
