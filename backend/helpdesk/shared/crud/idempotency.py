"""Idempotency-key reads and writes.

`IdempotencyKey` is a `TenantScoped` model like any other, so these could be
a plain `Repository`; they exist as named helpers because expiry is part of
what "the key exists" means. A caller that fetched the row generically would
have to remember to compare `expires_at` itself, and forgetting once means
replaying a response the system no longer promises to honour.
"""

from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, Delete, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.crud import Repository
from helpdesk.shared.models import IdempotencyKey


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
