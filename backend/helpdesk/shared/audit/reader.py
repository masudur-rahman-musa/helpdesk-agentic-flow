"""The two leaf functions that read from the audit tables: `latest_chain_anchor` reads the tenant's stored chain head, and `verify_tenant_chain` walks the events and checks them against it. Both are here so `anchor.py` can import from a leaf module with no cycle."""

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.audit.chain import ChainAnchor, VerifyResult, verify_chain
from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.models import AuditChainAnchor, AuditEvent

__all__ = ["as_chain_event", "latest_chain_anchor", "verify_tenant_chain"]

_HASHED_COLUMNS = (
    "tenant_id",
    "occurred_at",
    "actor_kind",
    "actor_ref",
    "session_id",
    "trace_id",
    "event_type",
    "subject_type",
    "subject_id",
    "payload",
)


async def latest_chain_anchor(session: AsyncSession, tenant_id: uuid.UUID) -> ChainAnchor | None:
    """The tenant's current head: the anchor with the HIGHEST `audit_seq`.

    By seq, never by `anchored_at` or by insertion order. The two orderings
    agree on every row this module writes and disagree on a row planted around
    it, and the wrong one verifies a truncated log against a head BELOW the
    truncation, which reads as perfectly intact. Pinned by
    `test_audit_anchor.py::test_latest_chain_anchor_reads_the_highest_seq_not_the_newest_row`.
    """
    result = await session.execute(
        select(AuditChainAnchor)
        .where(AuditChainAnchor.tenant_id == tenant_id)
        .order_by(AuditChainAnchor.audit_seq.desc())
        .limit(1)
    )
    row = result.scalars().first()
    if row is None:
        return None
    return ChainAnchor(audit_seq=row.audit_seq, audit_hash=row.audit_hash)


def as_chain_event(row: AuditEvent) -> Mapping[str, Any]:
    """One stored row in the shape `chain.verify_chain` walks.

    These are the writer's hashed fields read back: the two lists must stay
    identical, and a drift either way surfaces as a false break rather than
    a silent pass.
    """
    event: dict[str, Any] = {name: getattr(row, name) for name in _HASHED_COLUMNS}
    event["seq"] = row.seq
    event["prev_hash"] = row.prev_hash
    event["hash"] = row.hash
    return event


async def verify_tenant_chain(session: AsyncSession, tenant_id: uuid.UUID) -> VerifyResult:
    """Walk `tenant_id`'s events in `seq` order and hold them to its anchor.

    A tenant that has never been anchored gets exactly the verdict it got
    before an earlier change, with `anchored_seq` None to say so.
    """
    anchor = await latest_chain_anchor(session, tenant_id)
    result = await session.execute(
        select(AuditEvent).where(AuditEvent.tenant_id == tenant_id).order_by(AuditEvent.seq)
    )
    return verify_chain([as_chain_event(row) for row in result.scalars().all()], anchor=anchor)
