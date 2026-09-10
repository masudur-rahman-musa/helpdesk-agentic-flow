"""The audit-chain gauge of the design, and the AND that makes it answerable.

`helpdesk_audit_chain_ok` is declared with NO labels (the design, an earlier change
decision D), and the scheduler's runner calls every job once per TENANT. Those
two facts do not fit together on their own: a plain `.set()` per tenant would
leave the gauge carrying whichever tenant the tick happened to visit last, and
`list_tenant_ids` orders by slug — so whether a doctored audit log was visible
at all would depend on the alphabetical position of the tenant it belonged to.

The map below is what closes that gap. Each tenant's verdict is remembered as
this process saw it, and the gauge is set to the AND: `1` while every tenant
this process has verified verified, `0` from the moment one of them does not.
Pinned by
`test_audit_health.py::test_a_healthy_tenant_verified_after_a_broken_one_leaves_the_gauge_at_zero`,
which verifies the healthy tenant SECOND on purpose — that is the order under
which a per-tenant `.set()` passes every other case in the file.

The map is process-local, and deliberately so: it is a cache of what this
sweep has seen, not a store. A restarted scheduler reports `1` until it has
verified a broken tenant again, and a tenant deleted from the database keeps
its entry until the process ends. Both are bounded by the tenant count, and
the alternative — a table — would be a second audit record to keep honest.

This module reads the audit log and nothing else; it writes no event. A verify
is a read, and an event saying "the chain was checked" would be a row inside
the very chain whose integrity is in question.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.audit.verify import verify_tenant_chain
from helpdesk.core.db import bound_tenant_id
from helpdesk.obs.metrics import record_audit_chain_ok

_CHAIN_VERDICTS: dict[uuid.UUID, bool] = {}
"""What this process's last verify said about each tenant's chain."""


def reset_chain_verdicts() -> None:
    """Forget every verdict. For tests, and for nothing else.

    The map is process-local, so without this a file's own order would decide
    its results: one broken tenant would pin the gauge at `0` for every case
    after it, and a single case re-run would disagree with the full file. Used
    by `test_audit_health.py::clean_verdicts`.
    """
    _CHAIN_VERDICTS.clear()


def _every_chain_verified() -> bool:
    """The AND over every tenant this process has verified.

    An empty map is `True`, which is only ever read before the first sweep of
    a tenant — and the gauge is not set at all until then, so no scrape can
    see that value.
    """
    return all(_CHAIN_VERDICTS.values())


async def refresh_chain_gauge(session: AsyncSession) -> None:
    """Verify the bound tenant's chain and set `helpdesk_audit_chain_ok`.

    The tenant comes off the session, never off an argument, so
    the verdict is filed under the tenant whose rows were actually walked.

    The GOOD answer is recorded as well as the bad one. A prometheus gauge
    carries `0` from the moment it is registered, so a recorder wired only
    into the failure branch would leave a healthy deployment indistinguishable
    from a permanently broken one — pinned by
    `test_audit_health.py::test_a_sound_chain_sets_the_gauge_to_one`, and its
    opposite, a gauge hard-wired to "I ran", by
    `::test_a_tampered_row_sets_the_gauge_to_zero`.

    A verify that RAISES records `0` and re-raises (decision G, fail closed).
    "Not verified" is the one reading that covers both a broken chain and a
    chain nobody could read, and the exception still has to reach the runner:
    the runner deletes the job's lock key on a failed run, so the sweep retries
    within one tick instead of waiting out the full 300-second cadence. Both
    halves are pinned by
    `::test_a_verify_that_raises_sets_the_gauge_to_zero_and_re_raises`, whose
    gauge is driven to `1.0` first so that leaving the last value fails.
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


__all__ = ["refresh_chain_gauge", "reset_chain_verdicts"]
