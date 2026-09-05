"""The directory port (the design, an earlier change decision A; mutation half, 11b).

Every read of directory state — who a user is, what their department
template says they should hold, what access they actually hold — and every
change to it goes through `DirectoryProvider`, so a future Entra/SCIM/LDAP
adapter is one new implementation of this Protocol, not a refactor.
`SimulatedDirectory` is the only v1 implementation: the seeded directory
tables themselves. An earlier change added `apply_grant`, the half `execute_grant`
writes through; revocation lands with row 11c.

Sessions here come from `tenant_session`, so the guard in `helpdesk.core.db`
filters every query to the bound tenant; a user id from another tenant reads
as absent, never as the row. Writes go through `Repository`, which stamps
the bound tenant and refuses `tenant_id` as a field, so nothing here can
place a row in another tenant either.
"""

import uuid
from datetime import datetime
from typing import Protocol

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.core.repository import ACTIVE_GRANT_STATUS, Repository, find_user_by_id
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

    async def apply_grant(
        self,
        *,
        user_id: uuid.UUID,
        role_id: uuid.UUID,
        request_id: uuid.UUID | None,
        granted_by_user_id: uuid.UUID | None,
        granted_at: datetime,
        expires_at: datetime | None,
    ) -> Grant:
        """Give the user this role, live from `granted_at`.

        The mutation half of the port (the design): whether "the directory"
        is these tables or a remote IdP, nothing outside this Protocol
        changes access. The decision to grant is NOT taken here — the caller
        (`helpdesk.access.execute.execute_grant`) has already re-classified
        and refused a conflict or a block, and this method only records it.
        """
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

    async def apply_grant(
        self,
        *,
        user_id: uuid.UUID,
        role_id: uuid.UUID,
        request_id: uuid.UUID | None,
        granted_by_user_id: uuid.UUID | None,
        granted_at: datetime,
        expires_at: datetime | None,
    ) -> Grant:
        """Insert one live `grants` row through the tenant-scoped repository.

        Flushed, not committed: the caller's transaction owns it, which is
        what lets `execute_grant`'s grant and its `grant.executed` event
        commit or roll back together (pinned by
        `test_access_execute.py::test_the_grant_and_its_event_roll_back_together`).
        `revoked_at` and `revoke_reason` are written explicitly as None so a
        new grant can never inherit a revocation.
        """
        return await Repository(self._session, Grant).create(
            user_id=user_id,
            role_id=role_id,
            request_id=request_id,
            granted_by_user_id=granted_by_user_id,
            granted_at=granted_at,
            expires_at=expires_at,
            revoked_at=None,
            revoke_reason=None,
            status=ACTIVE_GRANT_STATUS,
        )
