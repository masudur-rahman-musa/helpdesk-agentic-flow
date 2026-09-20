"""Letting an old chain anchor go, and never the head.

Beside `anchor.py`, so every statement against the audit tables stays in one
package. An earlier change left the growth open: a head taken on every sweep is a row
a day per tenant that nothing ever removes, and an anchor's whole value is
being old, so the window is a decision rather than an oversight. a design note
records it as 90 days.

Two guards keep the detection standing, and they are independent on purpose.
This module excludes the tenant's highest `audit_seq` from the statement, and
the database refuses the same row anyway: the trigger of this plan's revision
passes a DELETE only for a row past the window with a higher seq above it. A
tenant quiet for years therefore keeps its one ancient head under either
guard, pinned by
`test_audit_anchor_retention.py::test_a_tenants_only_anchor_is_kept_however_old`
and `::test_a_delete_of_the_head_as_the_restricted_role_is_refused_by_the_trigger`.

Decision E: a purge that removed rows ships one log line and nothing else. An
audit event would make the audit log grow to record the upkeep of its own
anchors, and this module would need the writer, which is the dependency the
anchor path was built without.
"""

import logging
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.core.db import bound_tenant_id
from helpdesk.models import AuditChainAnchor

logger = logging.getLogger(__name__)

ANCHOR_RETENTION_DAYS = 90
"""a design note's window, in days.

The same number is written as a SQL `INTERVAL` in this plan's Alembic
revision, and the two are held together by
`test_audit_anchor_retention.py::test_the_migrations_sql_interval_equals_the_library_constant`.
An applied migration is frozen, so that test is the only link between them.
"""

PURGE_LOG_MESSAGE = "audit.chain.anchors_purged"
"""The message an operator greps the shipped logs for, beside
`audit.chain.anchored`: one prefix answers for both halves of the anchor."""

__all__ = [
    "ANCHOR_RETENTION_DAYS",
    "PURGE_LOG_MESSAGE",
    "anchor_retention_cutoff",
    "purge_chain_anchors",
]


def anchor_retention_cutoff(now: datetime) -> datetime:
    """The instant an anchor must be strictly BELOW to be purgeable.

    Strict, so a row exactly at the cutoff is KEPT: the boundary is stated
    once here rather than at the call site, and both sides of it are pinned by
    `test_audit_anchor_retention.py::test_an_anchor_exactly_at_the_cutoff_is_kept_and_nothing_is_deleted`
    and `::test_an_anchor_one_microsecond_past_the_cutoff_is_deleted`.
    """
    return now - timedelta(days=ANCHOR_RETENTION_DAYS)


async def purge_chain_anchors(session: AsyncSession, now: datetime) -> int:
    """Delete the bound tenant's anchors past the window, and return how many.

    The tenant comes off the session, never off an argument, so
    the rows walked and the rows deleted answer for the tenant the runner
    bound. `test_audit_anchor_retention.py::test_purging_one_tenant_deletes_no_row_of_another`
    is the other half of that: both tenants hold rows past the cutoff, and only
    the bound one loses any.

    The head is excluded by seq and not by age: the two orderings agree on
    every row `anchor.py` writes and disagree on a row planted around them, and
    the wrong one deletes the head of a tenant whose newest anchor is not its
    highest. `::test_the_highest_seq_survives_even_when_it_is_the_oldest_row`
    is the case that tells them apart.

    No batching. At an earlier change's cadence a tenant takes about 96 anchors a day,
    so the window holds on the order of 8,600 rows and a first run over a
    backlog is one small statement.
    """
    tenant_id = bound_tenant_id(session)
    head_seq = (
        select(func.max(AuditChainAnchor.audit_seq))
        .where(AuditChainAnchor.tenant_id == tenant_id)
        .scalar_subquery()
    )
    # `AsyncSession.execute` is typed as returning a plain `Result`, which has
    # no `rowcount`; a DELETE actually returns a `CursorResult`, so the
    # narrowing is safe. `core/repository.py::_delete_count` does the same for
    # the repository's own deletes; it is private to that module, and this
    # package holds its own statements by design.
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            delete(AuditChainAnchor).where(
                AuditChainAnchor.tenant_id == tenant_id,
                AuditChainAnchor.anchored_at < anchor_retention_cutoff(now),
                AuditChainAnchor.audit_seq < head_seq,
            )
        ),
    )
    deleted = result.rowcount
    if deleted:
        logger.info(PURGE_LOG_MESSAGE, extra={"tenant_id": str(tenant_id), "deleted": deleted})
    return deleted
