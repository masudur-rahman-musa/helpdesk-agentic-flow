"""Remembering one tenant's verified chain head.

The thin I/O half of the anchor, beside `writer` and `verify`, so every
statement against the audit tables stays in one package.

The order is the whole guarantee: verify against the head already stored,
and anchor only on a verdict of `ok`. Written the other way round, a
truncated log would be re-anchored at its new, lower head and the deletion
would become permanently invisible; that ordering is pinned by
`test_audit_anchor.py::test_a_tampered_chain_writes_no_anchor` and
`::test_a_truncated_chain_writes_no_anchor`.

The row is only half the record. Decision B's other half is one log line per
write, carrying tenant, seq and hash and no user identity: logs already leave
the host, so a line shipped off it is the part of the head an attacker with
the database cannot reach. Pinned by
`::test_the_anchor_write_logs_one_line_naming_the_tenant_seq_and_hash`.
"""

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.models import AuditChainAnchor, AuditEvent
from helpdesk.shared.services.audit.chain import ChainAnchor

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
    """An anchor that would move a tenant's head BACKWARDS.

    `VALIDATION`, the the design code for an input that has to be fixed rather
    than retried. A lower seq breaks no constraint — different seq, same
    tenant — so without this the table would quietly hold two contradictory
    heads, and a reader taking the newest row would verify a truncated log
    against a head below the truncation. Pinned by
    `test_audit_anchor.py::test_an_anchor_behind_the_stored_head_is_refused_and_leaves_it_in_place`.
    """

    code = ErrorCode.VALIDATION


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


async def write_chain_anchor(
    session: AsyncSession, tenant_id: uuid.UUID, anchor: ChainAnchor
) -> None:
    """Store `anchor` as the tenant's head, or refuse to move the head back.

    The comparison is strict: an EQUAL seq is not a regression, it is the same
    head, and the unique index is what refuses that one. `<=` here would
    answer the wrong error for the honest re-anchor of a tenant that has
    emitted nothing since the last sweep.

    The check reads the stored head and then inserts, which is a
    read-modify-write; the unique index on (`tenant_id`, `audit_seq`) is the
    backstop two concurrent writers meet, and the runner's per-job Redis lock
    is the first one.
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

    The tenant comes off the session, never off an argument, so
    the chain walked and the head stored answer for the same tenant — the one
    the runner bound.

    Four cases end with no write: a chain that does not verify (which, since
    `verify_tenant_chain` loads the stored anchor, covers a truncated log as
    well as a tampered one), a tenant with no events at all, and a head
    already anchored at the current seq. The last one is what keeps a quiet
    tenant from meeting the unique index on every tick; the anchor that stands
    is returned, because it IS the current head.

    Takes no `now`: an anchor is a statement about the rows that exist, and an
    instant handed in would be a second, disagreeing record of when the head
    was taken.
    """
    # Imported here rather than at the top, and this is the only place it
    # happens: `verify.py` imports `latest_chain_anchor` from this module, so
    # the layering runs verify -> anchor and a top-level import back would be
    # a cycle. This module is the higher half — it decides whether to write —
    # and the deferred import is what keeps that direction true.
    from helpdesk.shared.services.audit.verify import verify_tenant_chain

    tenant_id = bound_tenant_id(session)
    verdict = await verify_tenant_chain(session, tenant_id)
    if not verdict.ok:
        return None
    head = await session.execute(
        select(AuditEvent.seq, AuditEvent.hash)
        .where(AuditEvent.tenant_id == tenant_id)
        .order_by(AuditEvent.seq.desc())
        .limit(1)
    )
    row = head.first()
    if row is None:
        return None
    anchor = ChainAnchor(audit_seq=row.seq, audit_hash=row.hash)
    if verdict.anchored_seq == anchor.audit_seq:
        return anchor
    await write_chain_anchor(session, tenant_id, anchor)
    return anchor
