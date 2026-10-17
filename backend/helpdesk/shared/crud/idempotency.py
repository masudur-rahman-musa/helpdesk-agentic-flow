"""Idempotency-key reads, writes and the advisory lock.

`IdempotencyKey` is a `TenantScoped` model like any other, so these could be
a plain `Repository`; they exist as named helpers because expiry is part of
what "the key exists" means. A caller that fetched the row generically would
have to remember to compare `expires_at` itself, and forgetting once means
replaying a response the system no longer promises to honour.

`acquire_idempotency_lock` holds the `text()` import for this module, the same
reason `crud/audit.py` holds it for the audit writer: both are listed in
TEXT_ALLOWLIST in the static guard test.
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
"""The two-argument (classid, objid) form, kept in a different key space from
`crud/audit.py`'s single-argument lock so an idempotency lock for a tenant
never blocks an audit append for the same tenant."""

_LOCK_CLASSID = 0x1DE3
"""Stable namespace for idempotency locks.  Only has to be stable; it shares
no key space with the audit writer's single-argument locks."""

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


def _lock_object_id(tenant_id: uuid.UUID, key: str) -> int:
    """A stable positive int4 for this (tenant, key).

    Two different keys that collide here only wait for each other; the
    correctness of a replay comes from the stored row, never from the lock.
    """
    digest = hashlib.sha256(tenant_id.bytes + key.encode()).digest()
    return int.from_bytes(digest[:4], "big") & _SIGNED_32_BIT_MASK


async def acquire_idempotency_lock(
    session: AsyncSession, tenant_id: uuid.UUID, key: str
) -> None:
    """Acquire the per-(tenant, key) transaction-scoped advisory lock.

    Serialises two concurrent calls with the same key so only one runs the
    effect; the loser blocks until the winner's transaction ends, then reads
    the stored row and replays.  The unique index on (tenant_id, key) is the
    backstop if the lock is ever bypassed.

    Uses the two-argument `pg_advisory_xact_lock(classid, objid)` form, which
    Postgres keeps in a different key space from `crud/audit.py`'s single-
    argument lock: an idempotency lock for a tenant can never block an audit
    append for the same tenant.  Pinned indirectly by
    `test_mcp_idempotency.py::test_two_concurrent_calls_run_the_effect_once`.

    The tenant actually used to derive the lock id is the session's bound one;
    a mismatched `tenant_id` argument is caught by the caller before this.
    """
    _ = bound_tenant_id(session)  # raise early if no tenant is bound
    await session.execute(
        _ADVISORY_LOCK, {"classid": _LOCK_CLASSID, "objid": _lock_object_id(tenant_id, key)}
    )
