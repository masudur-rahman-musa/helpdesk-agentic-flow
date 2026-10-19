"""The next readable number for the bound tenant, from one statement."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.models.numbers import NumberKind

# One upsert, so two requests filed at once never get the same number: the
# second waits on the first's row lock and then reads its `last`. Picked over
# `max(number) + 1` under an advisory lock because those keys are already
# taken by audit and idempotency.
_NEXT = text(
    """
    INSERT INTO reference_counters (id, tenant_id, kind, last)
    VALUES (gen_random_uuid(), :tenant_id, :kind, 1)
    ON CONFLICT (tenant_id, kind)
    DO UPDATE SET last = reference_counters.last + 1, updated_at = now()
    RETURNING last
    """
)


async def next_number(session: AsyncSession, kind: NumberKind) -> int:
    result = await session.execute(
        _NEXT, {"tenant_id": bound_tenant_id(session), "kind": kind.value}
    )
    return int(result.scalar_one())
