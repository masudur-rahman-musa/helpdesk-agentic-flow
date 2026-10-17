"""Tenant-scoped user reads, directory locks and the due-user sweep read.

Every read here takes a `tenant_session`, so the session guard injects the
tenant filter: a matching row in another tenant behaves as not-found, and an
id is not a cross-tenant existence oracle. The department and system locks
live beside the user ones because a transfer and a review both read-compute-
write over a person and the scope they sit in.
"""

import uuid
from datetime import datetime

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


async def lock_due_users(session: AsyncSession, now: datetime) -> list[User]:
    """The bound tenant's overdue people, locked for this transaction.

    Three conjuncts: `status = 'active'` (so somebody offboarded by hand last
    month is not swept again), `end_date IS NOT NULL` (so a permanent employee
    is never due) and the strict boundary itself (`end_date < now.date()`).

    `now.date()` rather than `now`: `end_date` is a DATE, and comparing it to a
    timestamp would widen the stored day to midnight and skip the person whose
    last day was yesterday until the day after.

    `FOR UPDATE` through the ORM so the tenant guard still injects its filter and
    the rows are held to the end of the caller's transaction.  Pinned by
    `test_access_leavers.py::test_a_user_whose_end_date_was_yesterday_is_swept` and
    `::test_a_user_whose_end_date_is_today_is_untouched`.

    Ordered by end date then id so two runs over the same rows write their events
    in the same order.
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
    """Every department of the bound tenant, ordered by name then id.

    The order is alphabetical so two runs over the same tenant agree on the
    sequence of reviews opened, making the audit log reproducible on a re-run.
    Used by the quarterly review generator.
    """
    result = await session.execute(select(Department).order_by(Department.name, Department.id))
    return list(result.scalars().all())


async def fetch_systems_by_sensitivity(
    session: AsyncSession, sensitivities: frozenset[str]
) -> list[System]:
    """The bound tenant's systems whose sensitivity is in `sensitivities`, ordered.

    The caller passes `GENERATED_SYSTEM_SENSITIVITIES` so the filter lives in
    the scheduler module and this function stays generic. Ordered by name then
    id for the same reason as `fetch_all_departments`: reproducible event order.
    """
    result = await session.execute(
        select(System)
        .where(System.sensitivity.in_(sorted(sensitivities)))
        .order_by(System.name, System.id)
    )
    return list(result.scalars().all())


async def fetch_active_it_admins(session: AsyncSession) -> list[User]:
    """Every active `it_admin` of the bound tenant, ordered by email then id.

    Used by the review escalation path.  `status = 'active'` excludes leavers
    who still carry the principal on their row.  The principal is matched inside
    the array with `@>`, so a person holding `["employee", "it_admin"]` is
    included.  Ordered so two runs over the same tenant agree on the list.
    """
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
