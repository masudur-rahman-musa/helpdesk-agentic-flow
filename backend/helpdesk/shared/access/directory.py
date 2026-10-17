"""The directory: who people are, what their department should hold, what they hold.

Every read and change of directory state goes through `DirectoryProvider`,
so a real Entra, SCIM or LDAP adapter is one new class, not a rewrite.
`SimulatedDirectory` uses the seeded tables. Sessions are bound to one
tenant: another tenant's user reads as absent, and writes stamp the bound
tenant, so nothing here can touch another tenant's rows.
"""

import uuid
from datetime import date, datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.directory import (
    fetch_live_role_ids_for_user,
    fetch_role_templates_for_department,
)
from helpdesk.shared.crud.grants import ACTIVE_GRANT_STATUS, REVOKED_GRANT_STATUS
from helpdesk.shared.crud.users import find_user_by_id
from helpdesk.shared.models import Grant, User


class DirectoryReader(Protocol):
    """The read-only half, for callers that must never write."""

    async def lookup_user(self, user_id: uuid.UUID) -> User | None:
        """The bound tenant's user with this id, or None."""
        ...

    async def list_membership_role_ids(self, user_id: uuid.UUID) -> frozenset[uuid.UUID]:
        """Role ids of the user's department template; empty if no user or department."""
        ...

    async def list_account_role_ids(
        self, user_id: uuid.UUID, now: datetime
    ) -> frozenset[uuid.UUID]:
        """Role ids of the user's live grants as of `now`."""
        ...


class DirectoryProvider(DirectoryReader, Protocol):
    """The whole port: every read above, plus every change to directory state."""

    async def list_department_role_ids(self, department_id: uuid.UUID) -> frozenset[uuid.UUID]:
        """Role ids of a department's template. Onboarding asks before the person exists."""
        ...

    async def create_user(
        self,
        *,
        email: str,
        display_name: str,
        department_id: uuid.UUID,
        start_date: date,
        principals: tuple[str, ...],
        password_hash: str,
        status: str,
    ) -> User:
        """Put one person into the directory. The caller has already checked the input."""
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
        """Give the user this role. The caller has already re-checked policy."""
        ...

    async def revoke_grant(self, *, grant: Grant, reason: str, revoked_at: datetime) -> Grant:
        """Take this one grant away, as of `revoked_at`.

        One grant at a time, like a real directory adapter. Which grants a
        revoke covers is the caller's choice; widening the write here would
        revoke rows nothing audited.
        """
        ...


class SimulatedDirectory:
    """The seeded directory tables as a `DirectoryProvider`."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lookup_user(self, user_id: uuid.UUID) -> User | None:
        return await find_user_by_id(self._session, user_id)

    async def list_membership_role_ids(self, user_id: uuid.UUID) -> frozenset[uuid.UUID]:
        user = await self.lookup_user(user_id)
        if user is None or user.department_id is None:
            return frozenset()
        return await fetch_role_templates_for_department(self._session, user.department_id)

    async def list_account_role_ids(
        self, user_id: uuid.UUID, now: datetime
    ) -> frozenset[uuid.UUID]:
        """Live: status `active`, not revoked, and expiry (if any) still ahead."""
        return await fetch_live_role_ids_for_user(self._session, user_id, now)

    async def list_department_role_ids(self, department_id: uuid.UUID) -> frozenset[uuid.UUID]:
        """One department's template. Another tenant's department id gives an empty set."""
        return await fetch_role_templates_for_department(self._session, department_id)

    async def create_user(
        self,
        *,
        email: str,
        display_name: str,
        department_id: uuid.UUID,
        start_date: date,
        principals: tuple[str, ...],
        password_hash: str,
        status: str,
    ) -> User:
        """Insert one `users` row; flushed, so it commits with the grants and event."""
        return await Repository(self._session, User).create(
            email=email,
            display_name=display_name,
            department_id=department_id,
            start_date=start_date,
            principals=list(principals),
            password_hash=password_hash,
            status=status,
            end_date=None,
            password_reset_requested_at=None,
            account_unlocked_at=None,
        )

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
        """Insert one live `grants` row; flushed, so it commits with its audit event."""
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

    async def revoke_grant(self, *, grant: Grant, reason: str, revoked_at: datetime) -> Grant:
        """End one live `grants` row; flushed, so it commits with its audit event.

        `status` is set too, not just `revoked_at`: every liveness filter
        reads `status` first.
        """
        revoked = await Repository(self._session, Grant).update(
            grant.id,
            revoked_at=revoked_at,
            revoke_reason=reason,
            status=REVOKED_GRANT_STATUS,
        )
        if revoked is None:  # pragma: no cover - unreachable through the bound session
            raise RuntimeError(f"grant {grant.id} vanished from its own tenant while revoking")
        return revoked
