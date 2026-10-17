"""the design's two gauge refreshers, in the one scheduler job that takes them.

Merged here from the old `access.gauges` and `audit.health`: both
are readings the metrics sweep takes once per tenant per tick, and only the
scheduler reads them, so they leave `shared` for the app that uses them. The
two functions keep their own shapes and decide nothing; `run_metrics_sweep`
in `helpdesk.scheduler.jobs` calls both on one lock key so a scrape's grant
count and chain verdict are always the same age.

`refresh_active_grants_gauge` sets `helpdesk_grants_active`: it asks
`count_live_grants` how many grants the bound tenant holds (`expires_at > now`,
the exact complement of the expiry sweep's `<= now`) and hands the number to
`obs.metrics`. The engine lives here rather than in `obs` because `obs` reaches
no store; `now` is passed in by the runner, never read
from a clock here, so every job of one tick answers for one instant.

`refresh_chain_gauge` sets the unlabelled `helpdesk_audit_chain_ok` to the AND
over every tenant this process has verified: `1` while all verified, `0` from
the moment one did not. The per-process verdict map is what makes a no-label
gauge answerable when the runner calls every job once per tenant; it is a
cache of what this sweep has seen, not a store, and a restarted scheduler
reports `1` until it has verified a broken tenant again.
"""

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.core.observability.metrics import record_audit_chain_ok, record_grants_active
from helpdesk.shared.crud.grants import count_live_grants
from helpdesk.shared.audit.reader import verify_tenant_chain


async def refresh_active_grants_gauge(session: AsyncSession, *, now: datetime) -> None:
    """Set `helpdesk_grants_active` for the bound tenant (the design).

    The label is the bound tenant's uuid, read off the session and never off
    an argument, so a caller cannot publish a number under
    somebody else's name.

    SET, never incremented: a gauge answers "how many right now", and
    `record_grants_active` is the recorder that enforces it. Pinned by
    `test_access_gauges.py::test_two_sweeps_over_an_unchanged_tenant_leave_the_same_value`.
    """
    count = await count_live_grants(session, now)
    record_grants_active(tenant=str(bound_tenant_id(session)), count=count)


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


__all__ = ["refresh_active_grants_gauge", "refresh_chain_gauge", "reset_chain_verdicts"]
