"""Tenant-scoped user reads and the directory locks.

Every read here takes a `tenant_session`, so the session guard injects the
tenant filter: a matching row in another tenant behaves as not-found, and an
id is not a cross-tenant existence oracle. The department and system locks
live beside the user ones because a transfer and a review both read-compute-
write over a person and the scope they sit in.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.models import Department, System, User


async def find_user_by_email(session: AsyncSession, email: str) -> User | None:
    """The bound tenant's user with this email, or None.

    Takes a `tenant_session`: the session guard injects the tenant filter,
    so a matching email in another tenant behaves as not-found. Lives here
    so login (and later rows) never build their own query.
    """
    result = await session.execute(select(User).where(User.email == email))
    return result.scalars().one_or_none()


async def find_user_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    """The bound tenant's user with this id, or None.

    A user id belonging to another tenant reads as not-found, because the
    session guard adds the tenant filter: the id is not a capability. Pinned
    by `test_mcp_read_tools.py::test_a_tenant_b_token_cannot_read_a_tenant_a_user`.
    """
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalars().one_or_none()


async def lock_user(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    """The bound tenant's user, locked for the rest of this transaction.

    `SELECT … FOR UPDATE` through the ORM, so the tenant guard still injects
    its filter: a user id in another tenant reads exactly like one that does
    not exist (both None), and the id is not a cross-tenant existence oracle.

    The lock is what serialises two transfers of the same person (decision D).
    A transfer is a read-compute-write over the person AND their grants: it
    reads the department, diffs two templates against what they hold, then
    writes all three. `lock_live_grants_for_role` covers the revoke half only,
    so without this both callers read the old department and both apply the
    arriving role. The idempotency guard cannot help either, because two
    callers send two different keys. Pinned end to end by
    `test_mcp_transfer_user.py::test_two_callers_racing_the_same_transfer_leave_one_move`,
    whose mover holds nothing and whose old template is empty, so no
    revocation exists that could refuse the loser instead.
    """
    result = await session.execute(select(User).where(User.id == user_id).with_for_update())
    return result.scalars().one_or_none()


async def lock_department(session: AsyncSession, department_id: uuid.UUID) -> Department | None:
    """The bound tenant's department, locked for the rest of this transaction.

    `SELECT … FOR UPDATE` through the ORM, so the tenant guard still injects
    its filter: a department id in another tenant reads exactly like one that
    does not exist (both None), and the id is not a cross-tenant existence
    oracle. Pinned by
    `test_access_reviews.py::test_locking_a_scope_answers_the_same_way_for_another_tenants_row_and_a_missing_one`.

    The lock is what serialises two openings of the same review (decision G):
    opening is a read-compute-write — is a review already open for this
    (scope, period), and if not write one — and the idempotency guard cannot
    help, because two callers send two different keys. The loser waits, then
    re-reads the review the winner committed and is refused
    `ReviewAlreadyOpenError`. Pinned end to end by
    `test_mcp_reviews.py::test_two_callers_racing_the_same_review_leave_exactly_one`.
    """
    result = await session.execute(
        select(Department).where(Department.id == department_id).with_for_update()
    )
    return result.scalars().one_or_none()


async def lock_system(session: AsyncSession, system_id: uuid.UUID) -> System | None:
    """The bound tenant's system, locked for the rest of this transaction.

    The system-scoped twin of `lock_department`, with the same tenant-guard
    and serialisation properties; the two exist separately because a review
    over a department and a review over a system are different rows to hold,
    and one helper taking "whichever id is set" would have to re-decide the
    scope the caller already knows.
    """
    result = await session.execute(select(System).where(System.id == system_id).with_for_update())
    return result.scalars().one_or_none()
