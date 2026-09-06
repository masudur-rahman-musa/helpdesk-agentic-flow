"""Tenant-scoped repository helpers: the only DB path for services.

A `Repository` wraps a session produced by `tenant_session` and never takes
a tenant id as an argument: reads are filtered by the session guard in
`helpdesk.core.db`, and `create` stamps the bound tenant. Ids belonging to
another tenant behave as not-found (`get`/`update` return None, `delete`
returns False) — never the row, never a cross-tenant effect.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from pydantic import BaseModel, ConfigDict
from sqlalchemy import CursorResult, Delete, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.core.db import bound_tenant_id
from helpdesk.models import Grant, IdempotencyKey, Role, System, Tenant, TenantScoped, User


async def find_tenant_by_slug(session: AsyncSession, slug: str) -> Tenant | None:
    """Resolve a tenant by its slug, or None.

    Tenants are the scoping root, not tenant-scoped rows, so `Repository`
    cannot fetch them; this helper exists so services still never build
    their own queries (login resolves the slug before any tenant is bound).
    """
    result = await session.execute(select(Tenant).where(Tenant.slug == slug))
    return result.scalars().one_or_none()


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


ACTIVE_GRANT_STATUS = "active"
"""The one `grants.status` value that can still be a live grant."""

REVOKED_GRANT_STATUS = "revoked"
"""What `grants.status` says once access was taken away.

Beside its sibling rather than in `helpdesk.access.revoke`, which is where
the plan's file list put it: the directory port writes the value and
`access.revoke` imports the port, so defining it there would close an import
cycle. `helpdesk.access.revoke` re-exports it, so the name is importable from
the module that owns the policy. Pinned to the exact string by
`test_access_revoke.py::test_revoking_marks_the_row_revoked_with_the_reason_and_timestamp`."""


class ActiveGrant(BaseModel):
    """One live grant, with the role and system named rather than referenced.

    A Pydantic model rather than a row tuple because this crosses a layer
    boundary (the MCP `list_my_access` tool renders it straight out), and
    ids alone would force every caller to re-read the directory.
    """

    model_config = ConfigDict(frozen=True)

    grant_id: uuid.UUID
    role_slug: str
    role_name: str
    privilege: str
    system_slug: str
    system_name: str
    granted_at: datetime
    expires_at: datetime | None


async def list_active_grants_for_user(
    session: AsyncSession, user_id: uuid.UUID, now: datetime
) -> Sequence[ActiveGrant]:
    """The bound tenant's live grants for one user, by system then role slug.

    The order is alphabetical, not chronological: a stable order a reader can
    scan, pinned by
    `test_mcp_read_tools.py::test_active_grants_come_back_in_system_then_role_order`.
    Nothing here promises "most recent first"; a caller that wants that sorts
    on `granted_at` itself.

    "Live" is three conjuncts, all of them here rather than in the caller:
    the status is `active`, nothing revoked it, and either it never expires
    or `expires_at` is still ahead. `expires_at` names the first instant the
    grant is gone, so the comparison is strict `>`; the microsecond pair in
    `test_mcp_read_tools.py::test_the_expiry_boundary_is_exclusive_at_expires_at`
    pins that, and one hidden fixture row per conjunct means dropping any one
    of the three fails a named case.

    Whole entities are selected, not columns: the tenant guard in
    `helpdesk.core.db` injects its filter per mapped entity, so every one of
    the three tables is scoped by the session rather than by this `where`.
    """
    result = await session.execute(
        select(Grant, Role, System)
        .join(Role, Role.id == Grant.role_id)
        .join(System, System.id == Role.system_id)
        .where(
            Grant.user_id == user_id,
            Grant.status == ACTIVE_GRANT_STATUS,
            Grant.revoked_at.is_(None),
            or_(Grant.expires_at.is_(None), Grant.expires_at > now),
        )
        .order_by(System.slug, Role.slug)
    )
    return [
        ActiveGrant(
            grant_id=grant.id,
            role_slug=role.slug,
            role_name=role.name,
            privilege=role.privilege,
            system_slug=system.slug,
            system_name=system.name,
            granted_at=grant.granted_at,
            expires_at=grant.expires_at,
        )
        for grant, role, system in result.all()
    ]


def is_live_grant(grant: Grant, now: datetime) -> bool:
    """Is this already-loaded grant live as of `now`?

    The Python half of the three conjuncts every SQL liveness filter here
    spells out (`list_active_grants_for_user`, `lock_live_grants_for_role`,
    and the directory's `list_account_role_ids`): status `active`, nothing
    revoked it, and either it never expires or `expires_at` — the first
    instant the grant is gone — is still ahead, so the comparison is strict.

    It lives beside them rather than in the caller that needs it
    (`mcp_server.tools.access.revoke_access`, which has to answer for the ONE
    grant a caller named before locking the set) so the two answers cannot
    drift into a tool that refuses `POLICY_BLOCKED` for a grant every read
    still shows as live. That they agree is pinned by
    `test_access_revoke.py::test_the_python_liveness_predicate_agrees_with_the_locked_read`,
    which crosses the four states against `lock_live_grants_for_role` itself.
    """
    return (
        grant.status == ACTIVE_GRANT_STATUS
        and grant.revoked_at is None
        and (grant.expires_at is None or grant.expires_at > now)
    )


async def lock_live_grants_for_role(
    session: AsyncSession, user_id: uuid.UUID, role_id: uuid.UUID, now: datetime
) -> Sequence[Grant]:
    """The bound tenant's LIVE grants for one (user, role), locked for this
    transaction.

    "Live" is the same three conjuncts as `list_active_grants_for_user`, and
    the set is the unit rather than one row because nothing makes a grant
    unique per (user, role): two requests under two idempotency keys both
    execute, so revoking the row a caller named would leave access alive
    (pinned by
    `test_access_revoke.py::test_a_second_live_row_for_the_same_role_is_revoked_too`).

    `SELECT … FOR UPDATE` through the ORM, so the tenant guard still injects
    its filter and the rows are held until the caller's transaction ends. The
    lock is what serialises two revocations of the same role: two callers send
    two different idempotency keys, so the guard cannot (pinned by
    `test_mcp_revoke_access.py::test_two_callers_racing_the_same_revocation_write_one_event_per_row`).
    It covers rows that exist when it is taken; a grant committed afterwards is
    a new live grant, not a missed revocation.

    Ordered by `granted_at` then `id` so the revoked set reads oldest first and
    two runs over the same rows produce the same event order, pinned by
    `test_access_revoke.py::test_the_revoked_set_comes_back_oldest_first`
    (planted newest first, so an unordered read fails).
    """
    result = await session.execute(
        select(Grant)
        .where(
            Grant.user_id == user_id,
            Grant.role_id == role_id,
            Grant.status == ACTIVE_GRANT_STATUS,
            Grant.revoked_at.is_(None),
            or_(Grant.expires_at.is_(None), Grant.expires_at > now),
        )
        .order_by(Grant.granted_at, Grant.id)
        .with_for_update()
    )
    return result.scalars().all()


# Set by the session/mixin, never by callers: accepting them in **fields
# would let an update move a row across tenants behind the query guard.
_PROTECTED_FIELDS = frozenset({"id", "tenant_id"})


def _reject_protected(fields: dict[str, Any]) -> None:
    protected = _PROTECTED_FIELDS & fields.keys()
    if protected:
        raise ValueError(
            f"protected fields cannot be set through the repository: {sorted(protected)}"
        )


class Repository[M: TenantScoped]:
    def __init__(self, session: AsyncSession, model: type[M]) -> None:
        self._session = session
        self._model = model

    async def get(self, id: uuid.UUID, /) -> M | None:
        result = await self._session.execute(select(self._model).where(self._model.id == id))
        return result.scalars().one_or_none()

    async def list(self) -> Sequence[M]:
        result = await self._session.execute(select(self._model))
        return result.scalars().all()

    async def create(self, **fields: Any) -> M:
        _reject_protected(fields)
        obj = self._model(tenant_id=bound_tenant_id(self._session), **fields)
        self._session.add(obj)
        await self._session.flush()
        return obj

    async def update(self, id: uuid.UUID, /, **fields: Any) -> M | None:
        _reject_protected(fields)
        obj = await self.get(id)
        if obj is None:
            return None
        for name, value in fields.items():
            setattr(obj, name, value)
        await self._session.flush()
        return obj

    async def delete(self, id: uuid.UUID, /) -> bool:
        obj = await self.get(id)
        if obj is None:
            return False
        await self._session.delete(obj)
        await self._session.flush()
        return True


# --- idempotency keys ---------------------------------------------
#
# `IdempotencyKey` is a `TenantScoped` model like any other, so these could be
# a plain `Repository`; they exist as named helpers because expiry is part of
# what "the key exists" means. A caller that fetched the row generically would
# have to remember to compare `expires_at` itself, and forgetting once means
# replaying a response the system no longer promises to honour.


async def create_idempotency_key(
    session: AsyncSession,
    *,
    key: str,
    tool_name: str,
    request_fingerprint: str,
    response: dict[str, Any],
    expires_at: datetime,
) -> IdempotencyKey:
    """Claim `key` for the bound tenant; the caller's transaction commits it."""
    return await Repository(session, IdempotencyKey).create(
        key=key,
        tool_name=tool_name,
        request_fingerprint=request_fingerprint,
        response=response,
        expires_at=expires_at,
    )


async def find_idempotency_key(
    session: AsyncSession, key: str, now: datetime
) -> IdempotencyKey | None:
    """The bound tenant's live row for `key`, or None.

    Expiry is honoured on read: a row whose `expires_at` has arrived is
    absent even though it is still on disk, so the key is free again before
    any purge has run. `expires_at` names the first instant it is gone.
    """
    result = await session.execute(
        select(IdempotencyKey).where(IdempotencyKey.key == key, IdempotencyKey.expires_at > now)
    )
    return result.scalars().one_or_none()


async def _delete_count(session: AsyncSession, statement: Delete) -> int:
    """Rows removed by an ORM DELETE.

    `AsyncSession.execute` is typed as returning a plain `Result`, which has
    no `rowcount`; every DELETE actually returns a `CursorResult`, so the
    narrowing is safe and lives here rather than at each call site.
    """
    result = cast("CursorResult[Any]", await session.execute(statement))
    return result.rowcount


async def delete_expired_idempotency_key(session: AsyncSession, key: str, now: datetime) -> bool:
    """Drop the bound tenant's row for `key` if it has expired; True if one went.

    The guard needs this before re-claiming an expired key: the row is
    invisible to `find_idempotency_key` but still holds the unique index.
    The `expires_at` condition is in the statement, not in the caller, so
    this can never remove a key that is still being honoured.
    """
    return (
        await _delete_count(
            session,
            delete(IdempotencyKey).where(
                IdempotencyKey.key == key, IdempotencyKey.expires_at <= now
            ),
        )
        > 0
    )


async def purge_expired(session: AsyncSession, now: datetime) -> int:
    """Delete the bound tenant's expired keys; returns how many went.

    Tenant-scoped like every other query here, so a scheduled purge
    loops tenants rather than sweeping the table in one unscoped statement.
    """
    return await _delete_count(
        session, delete(IdempotencyKey).where(IdempotencyKey.expires_at <= now)
    )
