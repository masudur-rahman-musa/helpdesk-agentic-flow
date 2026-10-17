"""Directory-table reads: role templates and live grant role ids.

Every statement against `role_templates` and `grants` that the directory port
needs lives here, so `shared/access/directory.py` builds no statement of its own.
The tenant guard in `helpdesk.core.db` scopes every read to the bound session.
"""

import uuid
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.crud.grants import ACTIVE_GRANT_STATUS
from helpdesk.shared.models import Grant, RoleTemplate


async def fetch_role_templates_for_department(
    session: AsyncSession, department_id: uuid.UUID
) -> frozenset[uuid.UUID]:
    """The role ids the template assigns to the named department's members.

    Used by both `list_membership_role_ids` (asked by user, department resolved
    from the user row) and `list_department_role_ids` (asked directly by department
    id, from the onboarding path).  The bound session scopes the read: another
    tenant's department id reads as an empty template, not as its rows.  Pinned by
    `test_access_lifecycle.py::test_the_directory_port_answers_the_departments_template`.
    """
    result = await session.execute(
        select(RoleTemplate).where(RoleTemplate.department_id == department_id)
    )
    return frozenset(template.role_id for template in result.scalars())


async def fetch_live_role_ids_for_user(
    session: AsyncSession, user_id: uuid.UUID, now: datetime
) -> frozenset[uuid.UUID]:
    """The role ids of this user's live grants as of `now`.

    "Live" is the same three conjuncts as `crud.grants.is_live_grant`: status
    `active`, nothing revoked it, and expiry (if any) is still ahead.  Used by
    `SimulatedDirectory.list_account_role_ids` to answer the snapshot question
    "what does this person currently hold".  Pinned by the live-grant boundary
    test in `test_access_snapshot.py`.
    """
    result = await session.execute(
        select(Grant).where(
            Grant.user_id == user_id,
            Grant.status == ACTIVE_GRANT_STATUS,
            Grant.revoked_at.is_(None),
            or_(Grant.expires_at.is_(None), Grant.expires_at > now),
        )
    )
    return frozenset(grant.role_id for grant in result.scalars())
