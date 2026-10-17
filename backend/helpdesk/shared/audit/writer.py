"""Appending one event to a tenant's chain.

Two appends that read the same last hash would fork the chain, so a
per-tenant advisory lock, held until the caller's transaction ends, puts
them in line. This only works under READ COMMITTED (the default): under
REPEATABLE READ the snapshot would predate the lock and appends could fork.

The insert joins the caller's transaction, so an action and its event
commit or roll back together.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import AwareDatetime, BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.models import AuditEvent
from helpdesk.shared.audit.chain import GENESIS_HASH, event_hash
from helpdesk.shared.crud.audit import acquire_audit_lock, fetch_last_event_hash


class AuditEventInput(BaseModel):
    """One event as its caller describes it.

    No chain fields and no tenant: the tenant comes from the caller's
    verified identity, never from the event body.
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


class TenantMismatchError(RuntimeError):
    """The requested tenant is not the one bound to the session."""


async def append_event(
    session: AsyncSession, tenant_id: uuid.UUID, event: AuditEventInput
) -> AuditEvent:
    """Link one event onto `tenant_id`'s chain and flush it; the caller commits.

    A tenant that differs from the session's bound one is refused. The ORM
    guard does not cover INSERTs, so without this check a wrong tenant would
    splice a row into another tenant's chain and make it read as tampered.
    """
    bound = bound_tenant_id(session)
    if bound != tenant_id:
        raise TenantMismatchError(
            f"session is bound to tenant {bound}, refusing to append for {tenant_id}"
        )

    await acquire_audit_lock(session, bound)
    prev_hash = await fetch_last_event_hash(session, bound)

    fields: dict[str, Any] = {
        # The tenant is inside the digest: without it a whole chain could be
        # relabelled onto an empty tenant and still verify as intact.
        "tenant_id": bound,
        # UTC before hashing, so the value read back from Postgres hashes
        # the same.
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
    row = AuditEvent(prev_hash=prev_hash, hash=event_hash(prev_hash, fields), **fields)
    session.add(row)
    await session.flush()
    return row
