"""Audit-chain reads, the advisory lock and the anchor-retention delete.

Every statement against `audit_events` and `audit_chain_anchors` — and the one
`text()` advisory lock that serialises per-tenant appends — lives here.  This is
the only file in the project allowed to call `sqlalchemy.text`, beside the three
health-probe routes and `crud/idempotency.py` (TEXT_ALLOWLIST in the static guard
test).

The advisory lock is transaction-scoped, so Postgres releases it when the caller's
transaction ends, whether it commits or rolls back.  Under READ COMMITTED (the
engine default) the read that follows the lock sees rows committed after the lock
was granted, which is the whole correctness argument.  Do not raise the isolation
level on a session that calls `acquire_audit_lock` without revisiting this.
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
"""The single-argument (bigint) form Postgres keeps in its own key space.

`crud/idempotency.py` uses the two-argument (classid, objid) form: the two key
spaces never collide, so an idempotency lock for a tenant can never block an
audit append for the same tenant (and vice versa).  Pinned by
`test_audit_writer.py::test_the_advisory_lock_uses_the_single_argument_form`."""

_SIGNED_64_BIT_OFFSET = 1 << 63


def _lock_key(tenant_id: uuid.UUID) -> int:
    """The tenant's low 64 bits as a signed bigint for `pg_advisory_xact_lock`.

    A collision between two tenants only serialises their appends against each
    other; it cannot mix or corrupt either chain.  Computed from the tenant id
    rather than stored so the value is always the same for the same tenant.
    """
    return (tenant_id.int & ((1 << 64) - 1)) - _SIGNED_64_BIT_OFFSET


async def acquire_audit_lock(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Acquire the per-tenant transaction-scoped advisory lock for audit appends.

    Serialises concurrent callers of `append_event` for the same tenant: the
    second blocks until the first's transaction ends (commit or rollback), then
    re-reads the chain head and appends correctly.  Pinned by
    `test_audit_writer.py::test_two_concurrent_appends_produce_a_chain_not_a_fork`.
    """
    await session.execute(_ADVISORY_LOCK, {"key": _lock_key(tenant_id)})


async def fetch_last_event_hash(session: AsyncSession, tenant_id: uuid.UUID) -> str:
    """The hash of the most recent event in this tenant's chain, or `GENESIS_HASH`.

    `GENESIS_HASH` is the agreed starting point for a tenant with no events:
    the first event's `prev_hash` field carries it, so the chain verifier can
    walk all the way to the beginning without a special case.  Ordered by `seq`
    descending and limited to one, so the scan is a single index probe.
    """
    result = await session.execute(
        select(AuditEvent.hash)
        .where(AuditEvent.tenant_id == tenant_id)
        .order_by(AuditEvent.seq.desc())
        .limit(1)
    )
    return result.scalar_one_or_none() or GENESIS_HASH


async def fetch_head_event(
    session: AsyncSession, tenant_id: uuid.UUID
) -> tuple[int, str] | None:
    """The (seq, hash) of this tenant's most recent event, or None.

    Called by `anchor_tenant_chain` after the chain verifies, to record the
    head as an anchor row.  Returns None for a tenant with no events, which the
    caller treats as "nothing to anchor".  Pinned by
    `test_audit_anchor.py::test_a_tenant_with_no_events_returns_no_anchor`.
    """
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
    """The anchor with the highest `audit_seq` for this tenant, or None.

    By seq, never by `anchored_at` or insertion order: the two orderings agree
    on every row `anchor.py` writes and disagree on a row planted around it.
    The wrong ordering verifies a truncated log against a head below the
    truncation.  Pinned by
    `test_audit_anchor.py::test_latest_chain_anchor_reads_the_highest_seq_not_the_newest_row`.
    """
    result = await session.execute(
        select(AuditChainAnchor)
        .where(AuditChainAnchor.tenant_id == tenant_id)
        .order_by(AuditChainAnchor.audit_seq.desc())
        .limit(1)
    )
    return result.scalars().first()


async def fetch_events_ordered(
    session: AsyncSession, tenant_id: uuid.UUID
) -> list[AuditEvent]:
    """All events of this tenant in seq order, for chain verification.

    Called by `verify_tenant_chain`.  `seq` order is the only total order
    over a tenant's chain — two events can share an `occurred_at` instant.
    The ORM guard adds the tenant filter through `with_loader_criteria`.
    """
    result = await session.execute(
        select(AuditEvent).where(AuditEvent.tenant_id == tenant_id).order_by(AuditEvent.seq)
    )
    return list(result.scalars().all())


async def delete_old_chain_anchors(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    cutoff: datetime,
) -> int:
    """Delete this tenant's anchors strictly before `cutoff` that are not the head.

    The head is excluded by the highest `audit_seq` subquery, not by `anchored_at`,
    so a tenant with one very old anchor keeps it even if it predates the cutoff.
    Returns how many rows were deleted.  Pinned by
    `test_audit_anchor_retention.py::test_a_tenants_only_anchor_is_kept_however_old`.
    """
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
    """At most `limit` of the bound tenant's events matching `clauses`, newest seq first.

    `clauses` is the already-built list from `audit.search._filters`, which stays in
    that module since it is pure (no I/O).  The ORM guard adds the tenant filter
    through `with_loader_criteria`.  Pinned by
    `test_audit_search.py::test_the_scan_cap_is_read_exactly_and_hands_back_a_cursor`.
    """
    result = await session.execute(
        select(AuditEvent).where(*clauses).order_by(AuditEvent.seq.desc()).limit(limit)
    )
    return tuple(result.scalars().all())
