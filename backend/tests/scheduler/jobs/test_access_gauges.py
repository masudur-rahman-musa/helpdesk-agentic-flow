"""An earlier change §6: `refresh_active_grants_gauge`, called with a fixed `now`.

Written before the implementation exists. Expected surface (the implementer
must provide exactly this):

``helpdesk.obs.metrics``
    ``GRANTS_ACTIVE = "helpdesk_grants_active"`` — a Gauge with exactly the
    label ``tenant`` (the design), registered by ``install_sweep_gauges`` on the
    registry it is handed and reached through the module-level
    ``SWEEP_GAUGES``, read at CALL time so a test can install its own.
    ``def record_grants_active(*, tenant: str, count: int) -> None`` — SETS
    the series, never increments it.

``helpdesk.core.repository``
    ``async def count_live_grants(session, now) -> int`` — the bound tenant's
    live grants: ``status = 'active'``, ``revoked_at IS NULL``, and
    ``expires_at IS NULL OR expires_at > now``. The exact complement of
    ``access/expiry.py``'s ``expires_at <= now`` (21a decision D), so no
    instant exists at which a grant is neither counted nor expirable.

``helpdesk.access.gauges``
    ``async def refresh_active_grants_gauge(session, *, now) -> None`` — reads
    the count on the tenant-bound session and records it under that tenant.

The tenant label value is the bound tenant's uuid, read off the session and
never off an argument, which is also why every case below asserts
the series under ``str(tenant.tenant_id)``.

a project rule: ONE session-scoped database, schema created once. Per-test
isolation is a namespace, not a rebuild — the gauge is a whole-tenant count, so
every case seeds a tenant of its OWN with `_seed.seed_tenant` and no case can
see another's rows. The second namespace is the fresh `CollectorRegistry`
installed over `metrics.SWEEP_GAUGES`, which is what makes every assertion
below an ABSOLUTE value: against the process-wide pair the same case could only
assert a delta, which passes just as happily when the wrong site did the
recording (plan §5's fourth risk).

`seed_tenant` plants ONE grant per tenant, `status = 'active'` with a NULL
`expires_at` — a LIVE grant by decision E. Every expected count below is
written as that seeded row plus what the case plants, and the arithmetic is
stated in each docstring rather than left to be recomputed.

No pytest-asyncio: each case is a sync function driving one `asyncio.run`.
"""

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from prometheus_client import CollectorRegistry

from helpdesk.scheduler.jobs.metrics_sweep import refresh_active_grants_gauge
from helpdesk.shared.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.shared.core.repository import ACTIVE_GRANT_STATUS, REVOKED_GRANT_STATUS, Repository
from helpdesk.shared.models import Base, Grant
from helpdesk.shared.obs import metrics
from tests.conftest import TempDatabaseFactory
from tests.support.seed import SeededTenant, seed_tenant

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
"""The one instant every case counts at. Passed in, never read from a clock."""

GRANTED_AT = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
LAPSED_AT = NOW - timedelta(days=1)
"""Comfortably past `NOW`: a row the expiry sweep has not reached yet."""
AHEAD = NOW + timedelta(days=30)
"""Comfortably ahead of `NOW`: an ordinary live grant."""
JUST_AHEAD = NOW + timedelta(microseconds=1)
"""The narrowest value that must be COUNTED — the open side of the boundary."""

EXPIRED_GRANT_STATUS = "expired"
"""What `grants.status` reads once the expiry sweep has been through.

A literal rather than an import: expiry is not revocation, and the only thing
that makes this string right is that an earlier change §1 fixed it."""

GRANTS_ACTIVE = "helpdesk_grants_active"
"""The exposed name a dashboard queries, written out."""

SEEDED_LIVE_GRANTS = 1
"""How many live grants `_seed.seed_tenant` leaves behind, per tenant.

Read off `_seed.py` rather than assumed: its one `Grant` row is `active` with
`expires_at=None` and `revoked_at=None`, which decision E counts."""


@pytest.fixture(scope="session")
def gauges_db_url(pg: TempDatabaseFactory) -> Iterator[str]:
    """One database, schema created exactly once."""
    url = pg.create()

    async def prepare() -> None:
        engine = create_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        finally:
            await engine.dispose()

    asyncio.run(prepare())
    yield url
    pg.drop(url)


@pytest.fixture
def sweep_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[CollectorRegistry]:
    """A registry of this test's own, with the sweep gauges installed on it.

    `test_obs_metrics.py::fresh_registry`'s shape for the SEPARATE factory of
    an earlier change decision F: the recorder reads `metrics.SWEEP_GAUGES` at call
    time, so replacing that name here is what makes an absolute assertion
    possible. Nothing expensive is rebuilt; the isolation is a namespace.
    """
    registry = CollectorRegistry()
    monkeypatch.setattr(metrics, "SWEEP_GAUGES", metrics.install_sweep_gauges(registry))
    yield registry


def _tenant(url: str) -> SeededTenant:
    """A tenant of this case's own: the per-test namespace."""

    async def prepare() -> SeededTenant:
        engine = create_engine(url)
        try:
            return await seed_tenant(
                create_session_factory(engine), f"gauges-{uuid.uuid4().hex[:12]}"
            )
        finally:
            await engine.dispose()

    return asyncio.run(prepare())


def _plant_grant(
    url: str,
    tenant: SeededTenant,
    *,
    expires_at: datetime | None = AHEAD,
    status: str = ACTIVE_GRANT_STATUS,
    revoked_at: datetime | None = None,
    revoke_reason: str | None = None,
) -> uuid.UUID:
    """One committed `grants` row for the tenant's seeded user."""

    async def scenario() -> uuid.UUID:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant.tenant_id) as session:
                row = await Repository(session, Grant).create(
                    user_id=tenant.user_id,
                    role_id=tenant.role_b_id,
                    request_id=None,
                    granted_by_user_id=None,
                    granted_at=GRANTED_AT,
                    expires_at=expires_at,
                    revoked_at=revoked_at,
                    revoke_reason=revoke_reason,
                    status=status,
                )
                return row.id
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _refresh(url: str, tenant_id: uuid.UUID, *, now: datetime = NOW, times: int = 1) -> None:
    """`refresh_active_grants_gauge` on a session bound to `tenant_id`."""

    async def scenario() -> None:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            for _ in range(times):
                async with tenant_session(factory, tenant_id) as session:
                    await refresh_active_grants_gauge(session, now=now)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def _gauge(registry: CollectorRegistry, tenant_id: uuid.UUID) -> float | None:
    """One tenant's `helpdesk_grants_active` sample, or None when absent."""
    return registry.get_sample_value(GRANTS_ACTIVE, {"tenant": str(tenant_id)})


# --- the count itself (criterion 2, decision E) -------------------------------


def test_the_gauge_is_set_to_the_bound_tenants_live_grant_count(
    gauges_db_url: str,
    sweep_registry: CollectorRegistry,
) -> None:
    """Catches a count that is not decision E's LIVE set, one wrong row at a
    time. Three excluded rows are planted, and each is excluded by a different
    conjunct:

    a `revoked` row (drop `revoked_at IS NULL` or `status = 'active'` and the
    gauge reports access that was taken away); an `expired` row (drop
    `status = 'active'` and every grant the sweep has ever expired is still
    counted, so the number only ever rises); and a `status = 'active'` row
    whose `expires_at` is already past — decision E's own case, the one a
    "count the rows whose status is active" implementation gets wrong, because
    the expiry sweep runs every 300 seconds and a lapsed row can sit there for
    a whole cadence. That third row is why 3.0 rather than 4.0 is the answer,
    and no other case in this file separates those two implementations.

    3.0 = the seeded live grant + the two live grants planted here.
    """
    tenant = _tenant(gauges_db_url)
    _plant_grant(gauges_db_url, tenant, expires_at=AHEAD)
    _plant_grant(gauges_db_url, tenant, expires_at=None)
    _plant_grant(
        gauges_db_url,
        tenant,
        expires_at=AHEAD,
        status=REVOKED_GRANT_STATUS,
        revoked_at=GRANTED_AT,
        revoke_reason="fake revocation, must not be counted",
    )
    _plant_grant(gauges_db_url, tenant, expires_at=LAPSED_AT, status=EXPIRED_GRANT_STATUS)
    _plant_grant(gauges_db_url, tenant, expires_at=LAPSED_AT, status=ACTIVE_GRANT_STATUS)

    _refresh(gauges_db_url, tenant.tenant_id)

    assert _gauge(sweep_registry, tenant.tenant_id) == SEEDED_LIVE_GRANTS + 2
    assert _gauge(sweep_registry, tenant.tenant_id) == 3.0


def test_two_sweeps_over_an_unchanged_tenant_leave_the_same_value(
    gauges_db_url: str,
    sweep_registry: CollectorRegistry,
) -> None:
    """Criterion 2's "SET, never incremented". Catches a recorder built on
    `.inc(count)` — the shape every other recorder in `obs/metrics.py` uses,
    so it is the natural mistake — which reads 6.0 after the second sweep and
    doubles again every 300 seconds for ever. A gauge is read as "how many
    right now", so nothing downstream would flag it; the dashboard would just
    show access nobody holds.

    Also catches a `.set()` that is skipped when the count has not changed,
    which the third case cannot see.

    3.0 = the seeded live grant + the two planted here, unchanged between the
    two sweeps because nothing is written in between.
    """
    tenant = _tenant(gauges_db_url)
    _plant_grant(gauges_db_url, tenant, expires_at=AHEAD)
    _plant_grant(gauges_db_url, tenant, expires_at=AHEAD)

    _refresh(gauges_db_url, tenant.tenant_id, times=2)

    assert _gauge(sweep_registry, tenant.tenant_id) == SEEDED_LIVE_GRANTS + 2
    assert _gauge(sweep_registry, tenant.tenant_id) == 3.0


# --- the boundary (criterion 3, the complement of 21a decision D) -------------


def test_a_grant_expiring_at_exactly_now_is_not_counted(
    gauges_db_url: str,
    sweep_registry: CollectorRegistry,
) -> None:
    """Criterion 3's closed side. Catches `expires_at >= now`, which counts a
    grant `expire_grants` expires on the very same tick (21a decision D's
    `expires_at <= now`): the two jobs of one tick would then disagree about
    one row, and whichever claimed its cadence gate first would decide what
    the dashboard shows for the next five minutes.

    1.0 = the seeded live grant alone. The planted row is the only difference
    between this case and its microsecond twin below, so only the comparison
    can move the number — and the seeded grant means a gauge that was never
    set at all (None) fails too, rather than passing as "0 is not 2".
    """
    tenant = _tenant(gauges_db_url)
    _plant_grant(gauges_db_url, tenant, expires_at=NOW)

    _refresh(gauges_db_url, tenant.tenant_id)

    assert _gauge(sweep_registry, tenant.tenant_id) == SEEDED_LIVE_GRANTS
    assert _gauge(sweep_registry, tenant.tenant_id) == 1.0


def test_a_grant_expiring_one_microsecond_after_now_is_counted(
    gauges_db_url: str,
    sweep_registry: CollectorRegistry,
) -> None:
    """Criterion 3's open side, and the twin of the case above: the only
    difference between them is one microsecond on one planted row.

    Catches a strict comparison widened the wrong way — `expires_at > now`
    turned into a check that the grant has a day left, or a truncation of
    `now` to the second — which under-reports live access, and it under-reports
    it exactly where the expiry sweep also refuses to act, leaving a grant that
    is held but counted nowhere.

    2.0 = the seeded live grant + this one.
    """
    tenant = _tenant(gauges_db_url)
    _plant_grant(gauges_db_url, tenant, expires_at=JUST_AHEAD)

    _refresh(gauges_db_url, tenant.tenant_id)

    assert _gauge(sweep_registry, tenant.tenant_id) == SEEDED_LIVE_GRANTS + 1
    assert _gauge(sweep_registry, tenant.tenant_id) == 2.0


def test_a_grant_with_no_expiry_is_counted(
    gauges_db_url: str,
    sweep_registry: CollectorRegistry,
) -> None:
    """The null arm of the predicate. Catches a count written as a plain
    `expires_at > now`: Postgres compares NULL to an instant as unknown, so a
    permanent grant would silently drop out of the number — and permanent
    grants are exactly the ones a standing-access dashboard exists to show.

    2.0 = the seeded live grant + the permanent one planted here. Two, not
    one, so an implementation that counted only the FIRST null-expiry row (a
    `LIMIT`, an `exists`) fails as well.
    """
    tenant = _tenant(gauges_db_url)
    _plant_grant(gauges_db_url, tenant, expires_at=None)

    _refresh(gauges_db_url, tenant.tenant_id)

    assert _gauge(sweep_registry, tenant.tenant_id) == SEEDED_LIVE_GRANTS + 1
    assert _gauge(sweep_registry, tenant.tenant_id) == 2.0


# --- the tenant binding (criterion 4, a project rule) ----------------------------


def test_another_tenants_live_grants_do_not_move_this_tenants_gauge(
    gauges_db_url: str,
    sweep_registry: CollectorRegistry,
) -> None:
    """Criterion 4. Catches a count built as its own statement over `grants`
    instead of going through the tenant-bound session: the runner calls this
    job once per tenant, so a query that ignores the binding gives every
    tenant the whole deployment's number and a customer's dashboard shows a
    neighbour's access.

    The neighbour's rows are live by the same `now`, so only the scoping can
    keep them out — 6.0 is what an unscoped count reads here, against the 2.0
    the binding produces.

    The neighbour's SERIES is asserted ABSENT rather than zero, which is the
    second half: a sweep that helpfully set every tenant's gauge from one
    tenant's tick would publish numbers for tenants it never bound, and
    decision H's "a tenant keeps its last value" would then be meaningless.

    2.0 = tenant A's seeded live grant + one planted. Tenant B holds
    4 (its seeded one + three planted) and is never refreshed.
    """
    tenant = _tenant(gauges_db_url)
    neighbour = _tenant(gauges_db_url)
    _plant_grant(gauges_db_url, tenant, expires_at=AHEAD)
    for _ in range(3):
        _plant_grant(gauges_db_url, neighbour, expires_at=AHEAD)

    _refresh(gauges_db_url, tenant.tenant_id)

    assert _gauge(sweep_registry, tenant.tenant_id) == SEEDED_LIVE_GRANTS + 1
    assert _gauge(sweep_registry, tenant.tenant_id) == 2.0
    assert _gauge(sweep_registry, neighbour.tenant_id) is None
