"""Reading the audit tables: the tenant's chain head and a full chain check.

Kept in a leaf module so `anchor.py` can import it without a cycle.
"""

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.audit.chain import ChainAnchor, VerifyResult, verify_chain
from helpdesk.shared.crud.audit import fetch_events_ordered, fetch_latest_chain_anchor
from helpdesk.shared.models import AuditEvent

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

    Never by `anchored_at` or insert order: a planted row could then make a
    cut-short log check out against a head below the cut.
    """
    row = await fetch_latest_chain_anchor(session, tenant_id)
    if row is None:
        return None
    return ChainAnchor(audit_seq=row.audit_seq, audit_hash=row.audit_hash)


def as_chain_event(row: AuditEvent) -> Mapping[str, Any]:
    """One stored row in the shape `chain.verify_chain` walks.

    The columns must match the writer's hashed fields; a drift shows up as a
    false break, never a silent pass.
    """
    event: dict[str, Any] = {name: getattr(row, name) for name in _HASHED_COLUMNS}
    event["seq"] = row.seq
    event["prev_hash"] = row.prev_hash
    event["hash"] = row.hash
    return event


async def verify_tenant_chain(session: AsyncSession, tenant_id: uuid.UUID) -> VerifyResult:
    """Walk `tenant_id`'s events in `seq` order and hold them to its anchor.

    A tenant never anchored gets `anchored_seq` None.
    """
    anchor = await latest_chain_anchor(session, tenant_id)
    rows = await fetch_events_ordered(session, tenant_id)
    return verify_chain([as_chain_event(row) for row in rows], anchor=anchor)
