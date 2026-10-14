"""An earlier change §6: `refresh_chain_gauge`, the process-wide AND behind it.

Written before the implementation exists. Expected surface (the implementer
must provide exactly this):

``helpdesk.obs.metrics``
    ``AUDIT_CHAIN_OK = "helpdesk_audit_chain_ok"`` — a Gauge with NO labels
    (the design, an earlier change decision D), registered by ``install_sweep_gauges``
    and reached through the module-level ``SWEEP_GAUGES``, read at CALL time.
    ``def record_audit_chain_ok(ok: bool) -> None``.

``helpdesk.audit.health``
    ``async def refresh_chain_gauge(session) -> None`` — verifies the BOUND
    tenant's chain through ``audit.verify.verify_tenant_chain``, folds the
    answer into a process-local map keyed by tenant id, and records the AND
    over every tenant this process has verified. A verify that RAISES records
    ``0`` and re-raises (decision G).
    ``def reset_chain_verdicts() -> None`` — the reset seam this file needs.
    The map is process-local by design, so without a seam the second case in
    this file would inherit the first case's verdict and every case after a
    broken one would read ``0`` whatever the code did. The plan names the seam
    but not its spelling; this file fixes it as ``reset_chain_verdicts``, and
    a different name in the implementation is a rename here, not a redesign.

Decision D is what this file mostly exists for. The runner calls the job once
per tenant, so a plain ``.set()`` per tenant would let a healthy tenant's tick
erase a broken tenant's verdict; the map is what makes one unlabelled gauge
answer for the whole process.

a project rule: ONE session-scoped database, schema created once. Per-test
isolation is a namespace — a tenant of the case's own from `_seed.seed_tenant`
— plus the fresh `CollectorRegistry` installed over `metrics.SWEEP_GAUGES` and
the reset of the process-local map. Nothing is rebuilt per test.

Every gauge assertion below is preceded by a state in which the gauge holds the
OTHER value. A prometheus gauge reads `0.0` from the moment it is registered,
so a case that only ever asserts `0.0` on a fresh registry would pass against
an implementation that records nothing at all; each case here either drives it
to `1.0` first or asserts the `1.0` a later tick must not produce.

No pytest-asyncio: each case is a sync function driving one `asyncio.run`.
"""

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from _seed import SeededTenant, seed_tenant
from conftest import TempDatabaseFactory
from prometheus_client import CollectorRegistry
from sqlalchemy import text

from helpdesk.shared.audit import AuditEventInput
from helpdesk.shared.audit import health as audit_health
from helpdesk.shared.audit.health import refresh_chain_gauge, reset_chain_verdicts
from helpdesk.shared.audit.verify import verify_tenant_chain
from helpdesk.shared.audit.writer import append_event
from helpdesk.shared.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.shared.models import Base
from helpdesk.shared.obs import metrics

AUDIT_CHAIN_OK = "helpdesk_audit_chain_ok"
"""The exposed name an alert queries, written out."""

TAMPERED_EVENT_TYPE = "ticket.tampered"
"""What the insider rewrites one stored row's `event_type` to.

`event_type` is one of `audit.verify._HASHED_COLUMNS`, so changing it makes
that row stop recomputing — the ordinary shape of a doctored audit log."""


@pytest.fixture(scope="session")
def health_db_url(pg: TempDatabaseFactory) -> Iterator[str]:
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

    `test_obs_metrics.py::fresh_registry`'s shape for an earlier change decision F's
    separate factory: `record_audit_chain_ok` reads `metrics.SWEEP_GAUGES` at
    call time, so replacing that name here is what makes an absolute assertion
    possible and what stops these cases reading the scheduler's process-wide
    instance (plan §5's fourth risk).
    """
    registry = CollectorRegistry()
    monkeypatch.setattr(metrics, "SWEEP_GAUGES", metrics.install_sweep_gauges(registry))
    yield registry


@pytest.fixture(autouse=True)
def clean_verdicts() -> Iterator[None]:
    """Empty the process-local map before and after every case.

    The map is deliberately process-local (decision D), so without this the
    file's own order would decide its results: one broken tenant would pin the
    gauge at `0` for every case after it, and a re-run of a single case would
    disagree with the full file.
    """
    reset_chain_verdicts()
    yield
    reset_chain_verdicts()


def _tenant(url: str, *, events: int = 3) -> SeededTenant:
    """A tenant of this case's own, with a real chain of `events` links."""

    async def prepare() -> SeededTenant:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            tenant = await seed_tenant(factory, f"health-{uuid.uuid4().hex[:12]}")
            for index in range(1, events + 1):
                async with tenant_session(factory, tenant.tenant_id) as session:
                    await append_event(
                        session,
                        tenant.tenant_id,
                        AuditEventInput(
                            occurred_at=None,
                            actor_kind="system",
                            actor_ref=str(uuid.UUID(int=index)),
                            session_id=None,
                            trace_id=f"trace-fake-{index}",
                            event_type="ticket.created",
                            subject_type="ticket",
                            subject_id=str(uuid.UUID(int=1000 + index)),
                            payload={"fake": True, "index": index},
                        ),
                    )
            return tenant
        finally:
            await engine.dispose()

    return asyncio.run(prepare())


def _tamper(url: str, tenant_id: uuid.UUID) -> None:
    """Rewrite one hashed column of this tenant's FIRST event.

    Written straight against the table rather than through the writer: the
    whole point of the chain is that a change nobody was allowed to make is
    still detectable, and no code path in the repository can produce one.
    """

    async def scenario() -> None:
        engine = create_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE audit_events SET event_type = :event_type "
                        "WHERE tenant_id = :tenant AND seq = ("
                        "SELECT min(seq) FROM audit_events WHERE tenant_id = :tenant)"
                    ),
                    {"event_type": TAMPERED_EVENT_TYPE, "tenant": tenant_id},
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def _chain_verifies(url: str, tenant_id: uuid.UUID) -> bool:
    """What `verify_tenant_chain` itself says: the premise, read off the data."""

    async def scenario() -> bool:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant_id) as session:
                return (await verify_tenant_chain(session, tenant_id)).ok
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _refresh(url: str, tenant_id: uuid.UUID) -> None:
    """`refresh_chain_gauge` on a session bound to `tenant_id`."""

    async def scenario() -> None:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant_id) as session:
                await refresh_chain_gauge(session)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def _gauge(registry: CollectorRegistry) -> float | None:
    """The unlabelled `helpdesk_audit_chain_ok` sample."""
    return registry.get_sample_value(AUDIT_CHAIN_OK)


# --- the two verdicts (criterion 5) ------------------------------------------


def test_a_sound_chain_sets_the_gauge_to_one(
    health_db_url: str,
    sweep_registry: CollectorRegistry,
) -> None:
    """Catches a sweep that verifies and never records the GOOD answer — a
    recorder wired only into the failure branch, which is easy to write and
    impossible to see, because an unlabelled prometheus gauge reads `0.0` from
    the moment it is registered. The exposition would then look identical to a
    permanently broken chain, and the alert on it would fire from the first
    scrape of a healthy deployment until somebody muted it for good.

    The premise is read off the data rather than assumed:
    `verify_tenant_chain` must itself say this chain is sound, so a case that
    went red because `seed_tenant` or `append_event` broke would say so.
    """
    tenant = _tenant(health_db_url)
    assert _chain_verifies(health_db_url, tenant.tenant_id) is True

    _refresh(health_db_url, tenant.tenant_id)

    assert _gauge(sweep_registry) == 1.0


def test_a_tampered_row_sets_the_gauge_to_zero(
    health_db_url: str,
    sweep_registry: CollectorRegistry,
) -> None:
    """Catches a sweep that records `1` unconditionally — a gauge hard-wired
    to "I ran", which is what a health check written from the sweep's point of
    view rather than the chain's becomes. That is the one failure this whole
    plan exists to surface: a doctored audit log is invisible by construction,
    so a gauge that cannot go to zero means nobody ever learns.

    The tenant is driven to `1.0` FIRST and then tampered with, so the `0.0`
    below is a value this sweep wrote and not the value a fresh registry
    already held — and it also catches a map that never revises an existing
    tenant's verdict, which the third case cannot see because that one uses two
    different tenants.
    """
    tenant = _tenant(health_db_url)
    _refresh(health_db_url, tenant.tenant_id)
    assert _gauge(sweep_registry) == 1.0

    _tamper(health_db_url, tenant.tenant_id)
    assert _chain_verifies(health_db_url, tenant.tenant_id) is False

    _refresh(health_db_url, tenant.tenant_id)

    assert _gauge(sweep_registry) == 0.0


# --- the accumulator (criterion 5's second half, decision D) ------------------


def test_a_healthy_tenant_verified_after_a_broken_one_leaves_the_gauge_at_zero(
    health_db_url: str,
    sweep_registry: CollectorRegistry,
) -> None:
    """Decision D's kill: delete the per-tenant map, leave a plain `.set()`,
    and this case reads `1.0`.

    The runner visits every tenant on one tick, so with a `.set()` per tenant
    the gauge would carry only the LAST tenant's verdict — and `list_tenant_ids`
    orders by slug, so whether a broken chain is visible at all would depend on
    the alphabetical position of the tenant it belongs to. The healthy tenant
    is verified SECOND on purpose: that is the order that makes a per-tenant
    `.set()` pass every other case in this file.

    Both premises are read off the data, so a case that went red because the
    tamper did not take says so rather than blaming the accumulator.
    """
    broken = _tenant(health_db_url)
    healthy = _tenant(health_db_url)
    _tamper(health_db_url, broken.tenant_id)
    assert _chain_verifies(health_db_url, broken.tenant_id) is False
    assert _chain_verifies(health_db_url, healthy.tenant_id) is True

    _refresh(health_db_url, broken.tenant_id)
    assert _gauge(sweep_registry) == 0.0

    _refresh(health_db_url, healthy.tenant_id)

    assert _gauge(sweep_registry) == 0.0


# --- the unhappy path (criterion 6, decision G) -------------------------------


def test_a_verify_that_raises_sets_the_gauge_to_zero_and_re_raises(
    health_db_url: str,
    sweep_registry: CollectorRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision G, fail closed. Catches the two ways this goes wrong.

    One: a sweep that lets the exception out untouched, leaving the gauge at
    the `1.0` the previous tick wrote — the runner logs the failure, deletes
    the lock key and moves on, so an unreadable database or an unwalkable row
    would be reported as a chain somebody verified, by a sweep that never read
    it. This case drives the gauge to `1.0` first precisely so that wrong
    implementation is the one that fails here.

    Two: a sweep that swallows the exception after recording `0`. The runner
    treats a raise as "this run failed", deletes the key and retries within one
    tick (60 seconds); a swallowed exception leaves the key in place for the
    full 300-second cadence and the tick reports success.

    The failure is injected at the module seam rather than by breaking the
    database, because "the verify raised" is the contract — an unreachable
    store and a row the walk cannot read are the same event to this function,
    and neither can be produced reliably from a test.
    """
    tenant = _tenant(health_db_url)
    _refresh(health_db_url, tenant.tenant_id)
    assert _gauge(sweep_registry) == 1.0

    async def unreachable(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("fake: the audit store could not be read")

    monkeypatch.setattr(audit_health, "verify_tenant_chain", unreachable)

    with pytest.raises(RuntimeError):
        _refresh(health_db_url, tenant.tenant_id)

    assert _gauge(sweep_registry) == 0.0
