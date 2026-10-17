"""Idempotency-key reads, writes and the per-key lock.

Named helpers, not a plain `Repository`, because expiry is part of whether a
key exists; a generic read could replay a response that has run out.
"""

import hashlib
import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, Delete, delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.crud import Repository
from helpdesk.shared.models import IdempotencyKey

_ADVISORY_LOCK = text("SELECT pg_advisory_xact_lock(:classid, :objid)")
"""The two-argument form, so it never collides with the audit lock's keys."""

_LOCK_CLASSID = 0x1DE3

_SIGNED_32_BIT_MASK = (1 << 31) - 1


async def create_idempotency_key(
    session: AsyncSession,
    *,
    key: str,
    tool_name: str,
    request_fingerprint: str,
    response: dict[str, Any],
    expires_at: datetime,
) -> IdempotencyKey:
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
    """The live row for `key`, or None. An expired row reads as gone before any purge."""
    result = await session.execute(
        select(IdempotencyKey).where(IdempotencyKey.key == key, IdempotencyKey.expires_at > now)
    )
    return result.scalars().one_or_none()


async def _delete_count(session: AsyncSession, statement: Delete) -> int:
    """Rows removed. A DELETE returns a `CursorResult`, though it is typed as `Result`."""
    result = cast("CursorResult[Any]", await session.execute(statement))
    return result.rowcount


async def delete_expired_idempotency_key(session: AsyncSession, key: str, now: datetime) -> bool:
    """Drop the row for `key` only if expired, so the key can be claimed again.

    An expired row is invisible to reads but still holds the unique index.
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
    """Delete the tenant's expired keys and return how many went."""
    return await _delete_count(
        session, delete(IdempotencyKey).where(IdempotencyKey.expires_at <= now)
    )


def _lock_object_id(tenant_id: uuid.UUID, key: str) -> int:
    """A stable int for (tenant, key). A clash only makes two keys wait."""
    digest = hashlib.sha256(tenant_id.bytes + key.encode()).digest()
    return int.from_bytes(digest[:4], "big") & _SIGNED_32_BIT_MASK


async def acquire_idempotency_lock(session: AsyncSession, tenant_id: uuid.UUID, key: str) -> None:
    """Make two calls with the same key take turns, so only one runs the change.

    The second waits, then finds the stored row and replays it. The unique
    index is the backup if the lock is ever skipped.
    """
    _ = bound_tenant_id(session)  # raise early if no tenant is bound
    await session.execute(
        _ADVISORY_LOCK, {"classid": _LOCK_CLASSID, "objid": _lock_object_id(tenant_id, key)}
    )
