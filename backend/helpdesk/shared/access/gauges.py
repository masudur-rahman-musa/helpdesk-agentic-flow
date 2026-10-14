"""The live-grant gauge of the design, refreshed once per tenant per sweep.

One function, and it decides nothing about access: it asks
`core.repository.count_live_grants` how many grants the bound tenant holds and
hands the number to `obs.metrics`. The engine lives in `helpdesk.access`
rather than in `helpdesk.obs` because `obs` reaches no store — that promise is
what makes recording a metric an increment in memory and nothing else — and a
module running SQL there would make it false for the whole package (an earlier change
decision B).

The number is a COUNT of live grants, not of rows whose status is `active`
(decision E). The expiry sweep runs every 300 seconds, so a grant whose date
went by can sit in the table as `active` for a whole cadence; counting it
would put access on the dashboard that every read path in the system already
refuses. The two jobs therefore answer for the same instant from opposite
sides: this one counts `expires_at > now`, `expire_grants` expires
`expires_at <= now`.

`now` is passed in by the runner rather than read from a clock here, the same
shape `expire_grants` uses and for the same reason: every job of one tick
answers for one instant, and a clock read here would make the boundary
untestable at a fixed time.

Everything runs on the BOUND tenant's session. The runner opens one
`tenant_session` per tenant and calls this inside each, so the gauge carries
one series per tenant and a tenant is never given a neighbour's number —
pinned by
`test_access_gauges.py::test_another_tenants_live_grants_do_not_move_this_tenants_gauge`,
whose neighbour holds live grants of its own and whose series is asserted
absent.
"""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.core.repository import count_live_grants
from helpdesk.shared.obs.metrics import record_grants_active


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


__all__ = ["refresh_active_grants_gauge"]
