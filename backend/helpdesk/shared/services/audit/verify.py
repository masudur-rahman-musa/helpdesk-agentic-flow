"""Reading one tenant's chain back and checking it.

The thin I/O half of `chain.verify_chain`: it loads rows and hands them over
as mappings. Writer and verifier hash through the same `chain` functions, so
a drift between them would surface as a false break rather than a silent
pass.

It also loads the tenant's anchored head and hands it down, which is what
makes every caller that never learned about anchors — the verify route, the
chain gauge, the MCP read path — report a truncated or re-chained log without
changing a line of its own. Pinned by
`test_audit_anchor.py::test_verify_reports_truncation_against_the_stored_anchor`
and `::test_a_truncated_tenant_drives_the_chain_gauge_to_zero`.
"""

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.audit.anchor import latest_chain_anchor
from helpdesk.shared.models import AuditEvent
from helpdesk.shared.services.audit.chain import VerifyResult, verify_chain

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
