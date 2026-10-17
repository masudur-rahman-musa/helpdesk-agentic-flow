"""Removing old chain anchors, but never the head.

An anchor is taken every sweep, so without this the table grows for ever.
Old anchors go after 90 days. Two separate guards keep the newest one: this
module skips the tenant's highest `audit_seq`, and a database trigger
refuses that delete too. A purge logs one line instead of writing an audit
event, so the log does not grow to record its own upkeep.
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.crud.audit import delete_old_chain_anchors

logger = logging.getLogger(__name__)

ANCHOR_RETENTION_DAYS = 90
"""The window, in days. The migration repeats it as a SQL `INTERVAL`, and a
test keeps the two equal."""

PURGE_LOG_MESSAGE = "audit.chain.anchors_purged"
"""The message an operator greps the shipped logs for."""

__all__ = [
    "ANCHOR_RETENTION_DAYS",
    "PURGE_LOG_MESSAGE",
    "anchor_retention_cutoff",
    "purge_chain_anchors",
]


def anchor_retention_cutoff(now: datetime) -> datetime:
    """An anchor older than this instant may be removed; one exactly at it is kept."""
    return now - timedelta(days=ANCHOR_RETENTION_DAYS)


async def purge_chain_anchors(session: AsyncSession, now: datetime) -> int:
    """Delete the bound tenant's anchors past the window, and return how many.

    The tenant comes from the session. The head is kept by highest seq, not
    by age, so a planted row cannot get it deleted.
    """
    tenant_id = bound_tenant_id(session)
    deleted = await delete_old_chain_anchors(session, tenant_id, anchor_retention_cutoff(now))
    if deleted:
        logger.info(PURGE_LOG_MESSAGE, extra={"tenant_id": str(tenant_id), "deleted": deleted})
    return deleted
