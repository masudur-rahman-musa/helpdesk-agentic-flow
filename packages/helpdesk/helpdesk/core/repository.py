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

from sqlalchemy import CursorResult, Delete, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.core.db import bound_tenant_id
from helpdesk.models import IdempotencyKey, Tenant, TenantScoped, User


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
