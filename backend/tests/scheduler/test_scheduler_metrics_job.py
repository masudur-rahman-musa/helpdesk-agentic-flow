"""The metrics sweep job, its registration and its wrapper.

Written before the implementation exists. Expected surface (the implementer
must provide exactly this):

``scheduler.jobs``
    ``METRICS_SWEEP_JOB_NAME = "metrics_sweep"``
    ``async def run_metrics_sweep(session, now) -> None`` — calls
    ``helpdesk.access.gauges.refresh_active_grants_gauge(session, now=now)``
    and ``helpdesk.audit.health.refresh_chain_gauge(session)``, and drops both
    return values. It also installs the sweep gauges at module level, which is
    what makes both metrics appear on the scheduler's exposition and on no
    other service's.

``scheduler.runner``
    ``METRICS_SWEEP_CADENCE_SECONDS = 300`` — the cadence and,
    by design, the `PX` of the lock key.
    ``METRICS_SWEEP_JOB`` and one more ``REGISTRY`` row, under a name that
    passes the existing ``check_job_name``.

What this file deliberately does NOT re-test: `check_job_name`'s inclusive
boundary (exactly 64 accepted, 65 refused) and its `re.fullmatch` rejection
table. Both are pinned in `test_scheduler_runner.py`, and this change adds one
new value that shapes a key — the job name — which is asserted to PASS that
same gate below (the input-validation criterion, recorded rather than
silently skipped). Copying the boundary cases here would put them red and
green for exactly the same reasons as the originals: the neighbouring-guard
trap.

The database and the dedicated Redis index are
`test_scheduler_runner.py`'s session-scoped fixtures, imported rather than
rebuilt. Per-test isolation stays what that file established: the only key
this file writes is the job's own, and the case that touches it
deletes exactly that key first.

`test_scheduler_runner.py`, `test_scheduler_daily_jobs.py`,
`test_scheduler_review_jobs.py` and `test_scheduler_metrics_job.py` are four
distinct basenames, and none collides with anything in the tree.

No pytest-asyncio: each case is a sync function driving one `asyncio.run`.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.scheduler import jobs
from helpdesk.scheduler.runner import (
    METRICS_SWEEP_CADENCE_SECONDS,
    REGISTRY,
    check_job_name,
    job_lock_key,
    tenant_lock_key,
)

# Imported, not redefined: one throwaway database with two
# seeded tenants and one dedicated Redis index for the whole run.
from tests.scheduler.test_scheduler_runner import (  # noqa: F401
    NOW,
    _delete_key,
    _get_key_ttl,
    _run_tick,
    scheduler_db,
    scheduler_redis_url,
)
from tests.support.seed import SeededTenant

METRICS_SWEEP_KEY = "helpdesk:joblock:metrics_sweep"
"""The literal key an operator greps for with `KEYS helpdesk:joblock:*`.

Written out rather than composed, so this file fails if either the prefix or
the job name moves."""

ALL_JOB_NAMES = frozenset(
    {
        "expire_grants",
        "expiry_warnings",
        "leaver_sweep",
        "review_generation",
        "review_reminders",
        "metrics_sweep",
        "chain_anchor",
        "anchor_retention",
    }
)
"""Every job this service runs, counted off the code: the
five-minute sweep, the daily pair, the review pair, this one's, the
`chain_anchor`, and the `anchor_retention`."""

_FAKE_SESSION = object()
"""The session double the wrapper case hands through.

A bare object, never a database handle: the claim under test is "the wrapper
passes ITS arguments straight to the engines", and a real session would let a
wrapper that opened its own connection pass anyway."""


def _grants_recorder(
    calls: list[tuple[object, datetime]],
) -> Callable[..., Awaitable[None]]:
    """A double for `refresh_active_grants_gauge` or `refresh_waiting_chats_gauge`:
    records session and `now`."""

    async def engine(session: AsyncSession, *, now: datetime) -> None:
        calls.append((session, now))

    return engine


def _chain_recorder(calls: list[object]) -> Callable[..., Awaitable[None]]:
    """A double for `refresh_chain_gauge`: records the session it was given.

    It takes no `now` on purpose. The chain verdict is about the rows that
    exist, not about an instant, and a wrapper that handed a `now` down would
    fail here with a `TypeError` rather than silently growing an argument the
    engine has to accept for ever.
    """

    async def engine(session: AsyncSession) -> None:
        calls.append(session)

    return engine


# --- the constants -----------------------------------------


def test_the_metrics_job_constants_are_exactly_the_expected_values() -> None:
    """Catches this change's two new literals drifting.

    `METRICS_SWEEP_CADENCE_SECONDS` is the lock TTL by construction,
    and 300 is chosen over reusing `DAILY_CADENCE_SECONDS`
    for this reason: a gauge is read as "now", so a daily refresh means
    `helpdesk_grants_active` is a WRONG number for 23 hours rather than a
    missing one, and `helpdesk_audit_chain_ok` would report a chain nobody has
    walked since yesterday as verified. 86400 here is exactly that regression,
    and nothing else in the suite would show it.

    The name is asserted with the key it produces, because the name is the only
    client-shaped part of a Redis key here: a job renamed without its key
    moving orphans the lock a running instance holds while the new one takes a
    fresh key with a fresh window.
    """
    assert jobs.METRICS_SWEEP_JOB_NAME == "metrics_sweep"
    assert METRICS_SWEEP_CADENCE_SECONDS == 300
    assert job_lock_key(jobs.METRICS_SWEEP_JOB_NAME) == METRICS_SWEEP_KEY


def test_the_metrics_job_is_registered_at_its_cadence_under_a_valid_name() -> None:
    """The registration criterion.

    Catches a job written and never registered — the whole change lands, every
    other case in this file passes, and nothing ever runs it, so both gauges
    sit at their registration defaults for ever: `helpdesk_grants_active` has
    no series at all and `helpdesk_audit_chain_ok` reads `0`, which an alert
    reads as a tampered chain on a healthy deployment.

    Catches a job registered with its own cadence literal instead of the
    constant: the key's TTL IS the gate, so a job declaring 300 while its key
    is set for 86400 refreshes both gauges once a day.

    And it applies `check_job_name` to the REGISTERED name, which is where
    the input validation for the one new key-shaping
    value — a name with a hyphen, a capital or a trailing newline would then
    fail at import rather than inside a `SET` at three in the morning.
    """
    assert "metrics_sweep" in REGISTRY
    job = REGISTRY["metrics_sweep"]
    assert job.name == "metrics_sweep"
    assert job.cadence_seconds == METRICS_SWEEP_CADENCE_SECONDS
    assert job.cadence_seconds == 300
    assert check_job_name(job.name) == "metrics_sweep"
    assert job_lock_key(job.name) == METRICS_SWEEP_KEY


def test_the_registry_holds_all_eight_jobs_under_eight_distinct_names() -> None:
    """Catches a registration that REPLACES the registry rather than extending
    it — a `REGISTRY = {...}` rebuilt from this change's job, which silently
    stops the expiry sweep, the daily pair and the review pair and would
    show up nowhere else in this file.

    Also catches the new job being registered under a name another job already
    holds, which the per-job case cannot see: a dict keyed by name loses one of
    the two without complaining, and the survivor would run at the wrong
    cadence.

    Eight is counted off the code, not carried over from this file's previous
    row: `expire_grants`, `expiry_warnings`, `leaver_sweep`,
    `review_generation`, `review_reminders`, this one's `metrics_sweep`, the
    `chain_anchor`, and the `anchor_retention`. This is the ONE
    case in the tree that names the whole set, so the job is registered
    beside the other seven here and nowhere else.
    """
    assert set(REGISTRY) == set(ALL_JOB_NAMES)
    assert {job.name for job in REGISTRY.values()} == set(ALL_JOB_NAMES)
    assert len({job.name for job in REGISTRY.values()}) == len(REGISTRY)
    assert len(REGISTRY) == 8


# --- the wrapper (it decides nothing) -------------------------------


def test_the_metrics_sweep_wrapper_hands_its_session_and_now_to_both_engines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a wrapper that decides anything at all, in three ways.

    A dropped engine: a wrapper calling just the grant count leaves
    `helpdesk_audit_chain_ok` at its registration default for ever, so the
    metric whose whole purpose is to notice a doctored audit log would report
    `0` on a sound chain and nobody would be able to tell the difference. The
    waiting-chats count is read on the same tick. All three call lists are
    asserted, so dropping any one fails.

    A clock read here instead of the tick's instant: `now` is the boundary that
    decides which grants count (`expires_at > now`), and it is the exact
    complement of the expiry sweep's `<= now`. Two jobs of one tick reading two
    clocks would disagree about a grant lapsing between them, and the whole
    file's boundary pair would become untestable at a fixed instant.

    Its own session, or a re-bound tenant: the double is not a database handle,
    so a wrapper doing either could not reach a table at all — it can only pass
    this by handing on exactly what it was given, which is also what pins that
    the count and the verdict answer for the SAME tenant.
    """
    grants_calls: list[tuple[object, datetime]] = []
    waiting_calls: list[tuple[object, datetime]] = []
    chain_calls: list[object] = []
    monkeypatch.setattr(jobs, "refresh_active_grants_gauge", _grants_recorder(grants_calls))
    monkeypatch.setattr(jobs, "refresh_waiting_chats_gauge", _grants_recorder(waiting_calls))
    monkeypatch.setattr(jobs, "refresh_chain_gauge", _chain_recorder(chain_calls))
    session = cast(AsyncSession, _FAKE_SESSION)

    asyncio.run(jobs.run_metrics_sweep(session, NOW))

    assert grants_calls == [(_FAKE_SESSION, NOW)]
    assert waiting_calls == [(_FAKE_SESSION, NOW)]
    assert chain_calls == [_FAKE_SESSION]


# --- the cadence gate on the real registry ------------------


def test_a_tick_leaves_the_metrics_key_with_a_three_hundred_second_ttl(
    scheduler_db: tuple[str, list[SeededTenant]],  # noqa: F811
    scheduler_redis_url: str,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The registration criterion end to end, on the REAL registry against real
    Postgres and Redis, so the literal per-tenant key names and the literal TTL
    are pinned together.

    Catches a lock taken with no expiry (the job would never run again after
    one tick, for ever), one taken on a key an operator would not find, and a
    lock RELEASED at the end of the run — the key itself is
    the cadence gate, so a released key means this job re-walks every audit
    event every 60 seconds instead of every 300. Per-tenant keys are asserted
    PRESENT for both tenants, so a runner that deletes all keys after a run
    fails here.
    """
    database_url, tenants = scheduler_db
    for tenant in tenants:
        _delete_key(
            scheduler_redis_url,
            tenant_lock_key(jobs.METRICS_SWEEP_JOB_NAME, tenant.tenant_id),
        )

    _run_tick(database_url, scheduler_redis_url, dict(REGISTRY), monkeypatch=monkeypatch)

    for tenant in tenants:
        key = tenant_lock_key(jobs.METRICS_SWEEP_JOB_NAME, tenant.tenant_id)
        ttl = _get_key_ttl(scheduler_redis_url, key)
        assert ttl == METRICS_SWEEP_CADENCE_SECONDS, f"expected TTL 300 for {key}, got {ttl}"
        assert ttl == 300
