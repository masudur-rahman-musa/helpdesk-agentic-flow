"""Audit-chain reads, the per-tenant append lock and old-anchor cleanup.

The lock ends with the transaction. Under READ COMMITTED the read after it sees
rows committed while waiting, which keeps the chain whole. Do not raise the
isolation level on a session that takes this lock.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy import ColumnElement, CursorResult, delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.audit.chain import GENESIS_HASH
from helpdesk.shared.models import AuditChainAnchor, AuditEvent

_ADVISORY_LOCK = text("SELECT pg_advisory_xact_lock(:key)")
"""The one-argument form, so it never collides with the idempotency lock's two-argument keys."""

_SIGNED_64_BIT_OFFSET = 1 << 63


def _lock_key(tenant_id: uuid.UUID) -> int:
    """The tenant id's low 64 bits as a signed bigint. A clash only makes two tenants wait."""
    return (tenant_id.int & ((1 << 64) - 1)) - _SIGNED_64_BIT_OFFSET


async def acquire_audit_lock(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Make appends for one tenant take turns, so the chain never forks."""
    await session.execute(_ADVISORY_LOCK, {"key": _lock_key(tenant_id)})


async def fetch_last_event_hash(session: AsyncSession, tenant_id: uuid.UUID) -> str:
    """The latest event's hash, or `GENESIS_HASH` for a tenant with no events."""
    result = await session.execute(
        select(AuditEvent.hash)
        .where(AuditEvent.tenant_id == tenant_id)
        .order_by(AuditEvent.seq.desc())
        .limit(1)
    )
    return result.scalar_one_or_none() or GENESIS_HASH


async def fetch_head_event(session: AsyncSession, tenant_id: uuid.UUID) -> tuple[int, str] | None:
    """The (seq, hash) of the latest event, or None when there is nothing to anchor."""
    result = await session.execute(
        select(AuditEvent.seq, AuditEvent.hash)
        .where(AuditEvent.tenant_id == tenant_id)
        .order_by(AuditEvent.seq.desc())
        .limit(1)
    )
    row = result.first()
    if row is None:
        return None
    return (row.seq, row.hash)


async def fetch_latest_chain_anchor(
    session: AsyncSession, tenant_id: uuid.UUID
) -> AuditChainAnchor | None:
    """The anchor with the highest `audit_seq`, or None.

    By seq, not time: a planted row with a newer time could hide a cut log.
    """
    result = await session.execute(
        select(AuditChainAnchor)
        .where(AuditChainAnchor.tenant_id == tenant_id)
        .order_by(AuditChainAnchor.audit_seq.desc())
        .limit(1)
    )
    return result.scalars().first()


async def fetch_events_ordered(session: AsyncSession, tenant_id: uuid.UUID) -> list[AuditEvent]:
    """All events in seq order; two events can share a timestamp, so time is not enough."""
    result = await session.execute(
        select(AuditEvent).where(AuditEvent.tenant_id == tenant_id).order_by(AuditEvent.seq)
    )
    return list(result.scalars().all())


async def delete_old_chain_anchors(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    cutoff: datetime,
) -> int:
    """Delete anchors older than `cutoff`, always keeping the newest. Returns the count."""
    head_seq = (
        select(func.max(AuditChainAnchor.audit_seq))
        .where(AuditChainAnchor.tenant_id == tenant_id)
        .scalar_subquery()
    )
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            delete(AuditChainAnchor).where(
                AuditChainAnchor.tenant_id == tenant_id,
                AuditChainAnchor.anchored_at < cutoff,
                AuditChainAnchor.audit_seq < head_seq,
            )
        ),
    )
    return result.rowcount


async def fetch_audit_events_page(
    session: AsyncSession,
    clauses: Sequence[ColumnElement[bool]],
    limit: int,
) -> tuple[AuditEvent, ...]:
    """At most `limit` events matching `clauses`, newest first."""
    result = await session.execute(
        select(AuditEvent).where(*clauses).order_by(AuditEvent.seq.desc()).limit(limit)
    )
    return tuple(result.scalars().all())
