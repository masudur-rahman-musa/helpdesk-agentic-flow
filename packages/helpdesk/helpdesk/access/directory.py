"""The directory port (the design, an earlier change decision A; mutation half, 11b).

Every read of directory state — who a user is, what their department
template says they should hold, what access they actually hold — and every
change to it goes through `DirectoryProvider`, so a future Entra/SCIM/LDAP
adapter is one new implementation of this Protocol, not a refactor.
`SimulatedDirectory` is the only v1 implementation: the seeded directory
tables themselves. An earlier change added `apply_grant`, the half `execute_grant`
writes through, and an earlier change `revoke_grant`, the half
`helpdesk.access.revoke.revoke_role_access` writes through. An earlier change added
the person half, `create_user`, and `list_department_role_ids` beside it.

Sessions here come from `tenant_session`, so the guard in `helpdesk.core.db`
filters every query to the bound tenant; a user id from another tenant reads
as absent, never as the row. Writes go through `Repository`, which stamps
the bound tenant and refuses `tenant_id` as a field, so nothing here can
place a row in another tenant either.
"""

import uuid
from datetime import date, datetime
from typing import Protocol

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.core.repository import (
    ACTIVE_GRANT_STATUS,
    REVOKED_GRANT_STATUS,
    Repository,
    find_user_by_id,
)
from helpdesk.models import Grant, RoleTemplate, User


class DirectoryReader(Protocol):
    """What the snapshot builder may ask the directory (and nothing more).

    Split out from `DirectoryProvider` by an earlier change so that a caller which
    only READS can be typed as one. `helpdesk.access.lifecycle`'s deciding
    pass is that caller: it needs the directory's answers with this
    onboarding's already-decided roles folded in, and typing it as the full
    provider would have meant giving a read-only view mutation methods it
    must never have. Nothing here writes.
    """

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


class DirectoryProvider(DirectoryReader, Protocol):
    """The whole port: every read above, plus every change to directory state.

    `list_department_role_ids` sits here rather than on `DirectoryReader`
    because the reader is exactly what `build_request_snapshot` may ask, and
    a snapshot is always about a USER. Asking by department is onboarding's
    question, and onboarding holds the whole provider anyway.
    """

    async def list_department_role_ids(self, department_id: uuid.UUID) -> frozenset[uuid.UUID]:
        """Role ids of a DEPARTMENT's template, asked by department.

        Distinct from `list_membership_role_ids`, which asks the same question
        by USER: onboarding has to read the template before the joiner exists,
        so there is no user to ask about yet. Pinned by
        `test_access_lifecycle.py::test_the_directory_port_answers_the_departments_template`,
        which asks two departments whose templates differ, so an
        implementation delegating to the per-user half cannot pass.
        """
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
        """Put one person into the directory (the design's joiner half).

        The mutation half for people, as `apply_grant` is for access: a real
        Entra/SCIM adapter creates the account here, and nothing outside this
        Protocol has to change. No decision is taken in this method — the
        caller (`helpdesk.access.lifecycle.onboard_user`) has already refused a
        duplicate address and resolved the department, and this only records
        the person.
        """
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

    async def revoke_grant(self, *, grant: Grant, reason: str, revoked_at: datetime) -> Grant:
        """Take this ONE grant away, as of `revoked_at`.

        Singular on purpose, matching `apply_grant`: a
        real Entra/SCIM adapter removes one assignment at a time and must not
        carry the policy that decides which assignments those are. WHICH rows
        a revocation covers is `helpdesk.access.revoke.revoke_role_access`'s
        decision, and an implementation of this method that widened the write
        past the grant it was handed would revoke rows nothing audited.
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

    async def list_department_role_ids(self, department_id: uuid.UUID) -> frozenset[uuid.UUID]:
        """The `role_templates` rows of ONE department, read by department id.

        The bound session filters by tenant, so another tenant's department id
        answers the empty set rather than its template — an onboarding cannot
        import a foreign department's roles even if the id were guessed. The
        caller resolves the department first and refuses an unknown one
        `NOT_FOUND`, so an empty answer here means an empty template.
        """
        result = await self._session.execute(
            select(RoleTemplate).where(RoleTemplate.department_id == department_id)
        )
        return frozenset(template.role_id for template in result.scalars())

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
        """Insert one `users` row through the tenant-scoped repository.

        Flushed, not committed, exactly like `apply_grant`: the caller's
        transaction owns the person, their template grants and their
        `user.onboarded` event together, so a refusal anywhere in the
        onboarding takes all three (pinned by
        `test_access_lifecycle.py::test_a_duplicate_email_is_refused_before_any_write`).

        `end_date` and both self-service flags are written explicitly as None,
        so a joiner can never inherit a leaver's departure date or another
        account's reset stamp. `tenant_id` is NOT a parameter: `Repository`
        stamps the bound tenant and refuses the field, which is what keeps this
        method from placing a person in another tenant.
        """
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

    async def revoke_grant(self, *, grant: Grant, reason: str, revoked_at: datetime) -> Grant:
        """End one live `grants` row through the tenant-scoped repository.

        All THREE columns are written, not just `revoked_at`: `status` is what
        every liveness filter reads first, so a row stamped with a timestamp
        and left `active` still answers `list_my_access`. Pinned by
        `test_access_revoke.py::test_revoking_marks_the_row_revoked_with_the_reason_and_timestamp`.

        Flushed, not committed, exactly like `apply_grant`: the caller's
        transaction owns the row and its `grant.revoked` event, so a failure
        afterwards takes both (pinned by
        `test_access_revoke.py::test_the_revocations_and_their_events_roll_back_together`).

        `update` answers None for an id in another tenant, which cannot happen
        here — the grant was read through this same bound session — so a None
        is a wiring bug and is raised rather than swallowed.
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
