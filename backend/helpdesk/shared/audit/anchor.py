"""Remembering one tenant's checked chain head.

Order matters: check against the stored head first, and anchor only when it
passes. The other way round, a cut-short log would be re-anchored at its new
lower head and the loss would never show. Each write also logs one line with
tenant, seq and hash; logs leave the host, so someone with the database
alone cannot rewrite that copy.
"""

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.audit.chain import ChainAnchor
from helpdesk.shared.audit.reader import latest_chain_anchor, verify_tenant_chain
from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.crud.audit import fetch_head_event
from helpdesk.shared.models import AuditChainAnchor

logger = logging.getLogger(__name__)

ANCHOR_LOG_MESSAGE = "audit.chain.anchored"
"""The message an operator greps the shipped logs for."""

__all__ = [
    "ANCHOR_LOG_MESSAGE",
    "AnchorRegressionError",
    "anchor_tenant_chain",
    "latest_chain_anchor",
    "write_chain_anchor",
]


class AnchorRegressionError(ToolError):
    """An anchor that would move a tenant's head backwards.

    No constraint stops a lower seq, so without this the table could hold two
    heads that disagree.
    """

    code = ErrorCode.VALIDATION


async def write_chain_anchor(
    session: AsyncSession, tenant_id: uuid.UUID, anchor: ChainAnchor
) -> None:
    """Store `anchor` as the tenant's head, or refuse to move the head back.

    An equal seq is the same head, not a step back; the unique index refuses
    that one. The index also catches two writers racing; the job's Redis
    lock is the first guard.
    """
    stored = await latest_chain_anchor(session, tenant_id)
    if stored is not None and anchor.audit_seq < stored.audit_seq:
        raise AnchorRegressionError(
            f"an anchor at seq {anchor.audit_seq} is behind the stored head "
            f"{stored.audit_seq}: a chain head never moves backwards"
        )
    session.add(
        AuditChainAnchor(
            tenant_id=tenant_id,
            audit_seq=anchor.audit_seq,
            audit_hash=anchor.audit_hash,
            anchored_at=datetime.now(UTC),
        )
    )
    await session.flush()
    logger.info(
        ANCHOR_LOG_MESSAGE,
        extra={
            "tenant_id": str(tenant_id),
            "audit_seq": anchor.audit_seq,
            "audit_hash": anchor.audit_hash,
        },
    )


async def anchor_tenant_chain(session: AsyncSession) -> ChainAnchor | None:
    """Verify the bound tenant's chain and anchor its head, or write nothing.

    The tenant comes from the session, never an argument. Nothing is written
    when the chain fails its check, has no events, or is already anchored at
    its current seq; in that last case the standing anchor is returned.
    """
    tenant_id = bound_tenant_id(session)
    verdict = await verify_tenant_chain(session, tenant_id)
    if not verdict.ok:
        return None
    row = await fetch_head_event(session, tenant_id)
    if row is None:
        return None
    anchor = ChainAnchor(audit_seq=row[0], audit_hash=row[1])
    if verdict.anchored_seq == anchor.audit_seq:
        return anchor
    await write_chain_anchor(session, tenant_id, anchor)
    return anchor
