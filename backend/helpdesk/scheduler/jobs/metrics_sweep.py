"""The gauge readings the metrics sweep takes for each tenant.

`helpdesk_audit_chain_ok` has no tenant label, so this process keeps each
tenant's last verdict and reports the AND of them. The map lives in memory:
a restarted scheduler reports `1` until it finds a broken tenant again.
"""

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.audit.reader import verify_tenant_chain
from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.core.observability.metrics import (
    record_audit_chain_ok,
    record_chats_waiting,
    record_grants_active,
)
from helpdesk.shared.crud.grants import count_live_grants
from helpdesk.shared.crud.sessions import count_waiting_chats


async def refresh_active_grants_gauge(session: AsyncSession, *, now: datetime) -> None:
    """Set `helpdesk_grants_active` for the bound tenant.

    The label comes from the session, never an argument, so a caller cannot
    publish a number under another tenant's name. The value is set, not added to.
    """
    count = await count_live_grants(session, now)
    record_grants_active(tenant=str(bound_tenant_id(session)), count=count)


async def refresh_waiting_chats_gauge(session: AsyncSession, *, now: datetime) -> None:
    """Set `helpdesk_chats_waiting` for the bound tenant: chats left on a question
    for 30 minutes or more. The label comes from the session, as above."""
    count = await count_waiting_chats(session, now=now)
    record_chats_waiting(tenant=str(bound_tenant_id(session)), count=count)


_CHAIN_VERDICTS: dict[uuid.UUID, bool] = {}
"""What this process's last verify said about each tenant's chain."""


def reset_chain_verdicts() -> None:
    """Forget every verdict. For tests only.

    Without it, one broken tenant in an earlier test would hold the gauge at `0`
    for every test after it.
    """
    _CHAIN_VERDICTS.clear()


def _every_chain_verified() -> bool:
    """The AND over every tenant this process has verified.

    An empty map gives `True`, but the gauge is not set before the first sweep,
    so no scrape sees that value.
    """
    return all(_CHAIN_VERDICTS.values())


async def refresh_chain_gauge(session: AsyncSession) -> None:
    """Verify the bound tenant's chain and set `helpdesk_audit_chain_ok`.

    The good answer is recorded too: a gauge starts at `0`, so recording only
    failures would make a healthy system look broken. A verify that raises
    records `0` and re-raises, so the runner drops the lock and retries on the
    next tick instead of waiting out the full cadence.
    """
    tenant_id = bound_tenant_id(session)
    try:
        result = await verify_tenant_chain(session, tenant_id)
    except Exception:
        _CHAIN_VERDICTS[tenant_id] = False
        record_audit_chain_ok(_every_chain_verified())
        raise
    _CHAIN_VERDICTS[tenant_id] = result.ok
    record_audit_chain_ok(_every_chain_verified())


__all__ = [
    "refresh_active_grants_gauge",
    "refresh_chain_gauge",
    "refresh_waiting_chats_gauge",
    "reset_chain_verdicts",
]
