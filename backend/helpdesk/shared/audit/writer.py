"""Appending one event to a tenant's chain.

Read-last-hash then insert is a read-modify-write: two concurrent appends
that both read the same last hash would produce two events sharing a
`prev_hash`, which is a fork, not a chain. A transaction-scoped advisory
lock keyed on the tenant serialises appends per tenant
and is released by Postgres when the caller's transaction ends, whether it
commits or rolls back.

The insert joins the caller's transaction deliberately: a request that fails
after appending takes its audit event down with it, so there is never a
committed action without its event or an event without its action.

The lock only serialises correctly because the read that follows it sees
rows committed after the lock was granted, which is true under READ
COMMITTED (the engine default). Under REPEATABLE READ the snapshot would
predate the lock and two appends could still fork; do not set a stricter
isolation level on this session without revisiting the read below.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import AwareDatetime, BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.audit.chain import GENESIS_HASH, event_hash
from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.models import AuditEvent

_ADVISORY_LOCK = text("SELECT pg_advisory_xact_lock(:key)")

_SIGNED_64_BIT_OFFSET = 1 << 63


class AuditEventInput(BaseModel):
    """One event as its caller describes it.

    Carries no `tenant_id`, `seq`, `prev_hash` or `hash`: the chain fields
    belong to this module and the tenant comes from the caller's verified
    identity, never from the event body.
    """

    occurred_at: AwareDatetime | None = None
    actor_kind: str  # user | agent | system
    actor_ref: str | None = None
    session_id: uuid.UUID | None = None
    trace_id: str | None = None
    event_type: str
    subject_type: str
    subject_id: str | None = None
    payload: dict[str, Any] = {}


def _lock_key(tenant_id: uuid.UUID) -> int:
    """The tenant's low 64 bits as a signed bigint, which is what the lock takes.

    A collision between two tenants would only serialise their appends
    against each other; it cannot mix or corrupt either chain.
    """
    return (tenant_id.int & ((1 << 64) - 1)) - _SIGNED_64_BIT_OFFSET


async def _last_hash(session: AsyncSession, tenant_id: uuid.UUID) -> str:
    result = await session.execute(
        select(AuditEvent.hash)
        .where(AuditEvent.tenant_id == tenant_id)
        .order_by(AuditEvent.seq.desc())
        .limit(1)
    )
    return result.scalar_one_or_none() or GENESIS_HASH


class TenantMismatchError(RuntimeError):
    """The requested tenant is not the one bound to the session."""


async def append_event(
    session: AsyncSession, tenant_id: uuid.UUID, event: AuditEventInput
) -> AuditEvent:
    """Link one event onto `tenant_id`'s chain and flush it; the caller commits.

    The tenant actually written is the session's bound one, and a mismatched
    argument is refused rather than honoured. The ORM guard
    cannot catch this: it does not cover INSERTs, so a caller passing a
    tenant from a tool argument would otherwise splice a genesis-linked row
    into a different tenant's chain and make that chain read as tampered.
    """
    bound = bound_tenant_id(session)
    if bound != tenant_id:
        raise TenantMismatchError(
            f"session is bound to tenant {bound}, refusing to append for {tenant_id}"
        )

    await session.execute(_ADVISORY_LOCK, {"key": _lock_key(bound)})
    prev_hash = await _last_hash(session, bound)

    fields: dict[str, Any] = {
        # The tenant is inside the digest: without it a whole chain could be
        # relabelled onto an empty tenant and still verify as intact.
        "tenant_id": bound,
        # Normalised to UTC before hashing so the same instant always
        # renders one way; Postgres returns it in UTC on the way back, and
        # an offset difference would read as permanent tampering.
        "occurred_at": (event.occurred_at or datetime.now(UTC)).astimezone(UTC),
        "actor_kind": event.actor_kind,
        "actor_ref": event.actor_ref,
        "session_id": event.session_id,
        "trace_id": event.trace_id,
        "event_type": event.event_type,
        "subject_type": event.subject_type,
        "subject_id": event.subject_id,
        "payload": event.payload,
    }
    # `fields` carries tenant_id, so the stored row and the digest cannot
    # disagree about which chain this event belongs to.
    row = AuditEvent(prev_hash=prev_hash, hash=event_hash(prev_hash, fields), **fields)
    session.add(row)
    await session.flush()
    return row
