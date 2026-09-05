"""The directory port, read half (the design, an earlier change decision A).

Every read of directory state — who a user is, what their department
template says they should hold, what access they actually hold — goes
through `DirectoryProvider`, so a future Entra/SCIM/LDAP adapter is one new
implementation of this Protocol, not a refactor. `SimulatedDirectory` is the
only v1 implementation: the seeded directory tables themselves. The mutation
half (apply grant, revoke grant) lands in row 11b with `execute_grant`.

Sessions here come from `tenant_session`, so the guard in `helpdesk.core.db`
filters every query to the bound tenant; a user id from another tenant reads
as absent, never as the row.
"""

import uuid
from datetime import datetime
from typing import Protocol

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.core.repository import ACTIVE_GRANT_STATUS, find_user_by_id
from helpdesk.models import Grant, RoleTemplate, User


class DirectoryProvider(Protocol):
    """What the snapshot builder may ask the directory (and nothing more)."""

    async def lookup_user(self, user_id: uuid.UUID) -> User | None:
        """The bound tenant's user with this id, or None."""
        ...

    async def list_membership_role_ids(self, user_id: uuid.UUID) -> frozenset[uuid.UUID]:
        """Role ids of the user's department template.

        Empty frozenset for an unknown user or one with no department — a
        user outside every template is a valid directory answer, not an
        error (pinned by `test_access_snapshot.py::
        test_a_user_with_no_department_has_no_membership_roles`).
        """
        ...

    async def list_account_role_ids(
        self, user_id: uuid.UUID, now: datetime
    ) -> frozenset[uuid.UUID]:
        """Role ids of the user's live grants as of `now`."""
        ...


class SimulatedDirectory:
    """The seeded directory tables as a `DirectoryProvider` (the design)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lookup_user(self, user_id: uuid.UUID) -> User | None:
        return await find_user_by_id(self._session, user_id)

    async def list_membership_role_ids(self, user_id: uuid.UUID) -> frozenset[uuid.UUID]:
        user = await self.lookup_user(user_id)
        if user is None or user.department_id is None:
            return frozenset()
        result = await self._session.execute(
            select(RoleTemplate).where(RoleTemplate.department_id == user.department_id)
        )
        return frozenset(template.role_id for template in result.scalars())

    async def list_account_role_ids(
        self, user_id: uuid.UUID, now: datetime
    ) -> frozenset[uuid.UUID]:
        """Live means the same three conjuncts as `list_active_grants_for_user`:
        status `active`, nothing revoked it, and expiry (if any) still ahead —
        `expires_at` names the first instant the grant is gone, so strict `>`.
        """
        result = await self._session.execute(
            select(Grant).where(
                Grant.user_id == user_id,
                Grant.status == ACTIVE_GRANT_STATUS,
                Grant.revoked_at.is_(None),
                or_(Grant.expires_at.is_(None), Grant.expires_at > now),
            )
        )
        return frozenset(grant.role_id for grant in result.scalars())
