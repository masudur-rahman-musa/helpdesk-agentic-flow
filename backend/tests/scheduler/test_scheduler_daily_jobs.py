"""The two DAILY jobs, their registrations and their wrappers.

Written before the implementation exists. Expected surface (the implementer
must provide exactly this):

``scheduler.jobs``
    ``EXPIRY_WARNINGS_JOB_NAME = "expiry_warnings"``
    ``LEAVER_SWEEP_JOB_NAME = "leaver_sweep"``
    ``async def run_expiry_warnings(session, now) -> None`` — calls
    ``helpdesk.access.expiry.warn_expiring_grants(session, now=now)`` and drops
    the return value.
    ``async def run_leaver_sweep(session, now) -> None`` — calls
    ``helpdesk.access.leavers.sweep_leavers(session, now=now)`` and drops the
    return value.

``scheduler.runner``
    ``DAILY_CADENCE_SECONDS = 86400`` — the product spec "daily", and the `PX` of
    both lock keys.
    ``REGISTRY`` gains both jobs, each at ``DAILY_CADENCE_SECONDS``, under
    names that pass the existing ``check_job_name``.

What this file deliberately does NOT re-test: `check_job_name`'s inclusive
boundary (exactly 64 accepted, 65 refused) and its `re.fullmatch` rejection
table. Both are already pinned in `test_scheduler_runner.py`, and this change
adds no new value that shapes a key — only two constants of its own, which are
asserted to PASS that same gate below (a decision
recorded rather than silently skipped). A copy of those cases here would be
red and green for exactly the same reasons as the originals.

The database and the dedicated Redis index are
`test_scheduler_runner.py`'s session-scoped fixtures, imported rather than
rebuilt — seeding two tenants is the slow part of this suite. Per-test
isolation stays what that file established: the only keys this file writes are
the jobs' own two, and the one case that touches them deletes exactly
those two first.

This file carries the service in its name, and
`test_scheduler_runner.py` and `test_scheduler_daily_jobs.py` are distinct
basenames that collide with nothing in the tree.

No pytest-asyncio: each case is a sync function driving one `asyncio.run`.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.scheduler import jobs
from helpdesk.scheduler.runner import (
    DAILY_CADENCE_SECONDS,
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

EXPIRY_WARNINGS_KEY = "helpdesk:joblock:expiry_warnings"
LEAVER_SWEEP_KEY = "helpdesk:joblock:leaver_sweep"
"""The literal keys an operator greps for with `KEYS helpdesk:joblock:*`.

Written out rather than composed, so this file fails if either the prefix or
a job name moves."""

_FAKE_SESSION = object()
"""The session double the wrapper cases hand through.

A bare object, never a database handle: the claim under test is "the wrapper
passes ITS arguments straight to the engine", and a real session would let a
wrapper that opened its own connection pass anyway."""


def _recorder(
    calls: list[tuple[object, datetime]],
) -> Callable[..., Awaitable[list[Any]]]:
    """An engine double that records how it got called and returns a list.

    It returns a non-empty-typed value on purpose: both engines answer with a
    list, and a wrapper that returned it instead of dropping it would no
    longer satisfy the `-> None` the registry's `JobRun` type requires.
    """

    async def engine(session: AsyncSession, *, now: datetime) -> list[Any]:
        calls.append((session, now))
        return []

    return engine


# --- the constants -----------------------------------------


def test_the_daily_job_constants_are_exactly_the_expected_values() -> None:
    """Catches every one of this change's new literals drifting.

    The cadence IS the lock TTL by construction, so a
    `DAILY_CADENCE_SECONDS` of 3600 would run both jobs 24 times a day — 24
    expiry warnings per grant, were the dedupe ever dropped — while a value of
    a week would let a leaver keep every account for six days after their last
    one. The two names are asserted with the keys they produce, because the
    name is the only client-shaped part of a Redis key here: a job renamed
    without its key moving orphans the lock a running instance holds, and the
    new one takes a fresh key with a fresh window.
    """
    assert DAILY_CADENCE_SECONDS == 86400
    assert jobs.EXPIRY_WARNINGS_JOB_NAME == "expiry_warnings"
    assert jobs.LEAVER_SWEEP_JOB_NAME == "leaver_sweep"
    assert job_lock_key(jobs.EXPIRY_WARNINGS_JOB_NAME) == EXPIRY_WARNINGS_KEY
    assert job_lock_key(jobs.LEAVER_SWEEP_JOB_NAME) == LEAVER_SWEEP_KEY


@pytest.mark.parametrize(
    ("job_name", "lock_key"),
    [
        pytest.param("expiry_warnings", EXPIRY_WARNINGS_KEY, id="expiry-warnings"),
        pytest.param("leaver_sweep", LEAVER_SWEEP_KEY, id="leaver-sweep"),
    ],
)
def test_each_new_job_is_registered_at_the_daily_cadence_under_a_valid_name(
    job_name: str, lock_key: str
) -> None:
    """Per job.

    Catches a job written and never registered — the whole change lands, the
    tests of the library pass, and nothing ever runs it — and one registered
    with its own cadence literal instead of the constant: the key's TTL IS the
    gate, so a job declaring 86400 while its key is set for 300 runs 288 times
    a day. `check_job_name` is applied to the registered name (the
    two new names are the only values added here that shape a key, and
    their gate is the existing one), so a name with an underscore-leading
    typo or a hyphen fails here rather than at three in the morning inside a
    `SET`.
    """
    assert job_name in REGISTRY
    job = REGISTRY[job_name]
    assert job.name == job_name
    assert job.cadence_seconds == DAILY_CADENCE_SECONDS
    assert job.cadence_seconds == 86400
    assert check_job_name(job.name) == job_name
    assert job_lock_key(job.name) == lock_key


def test_the_registry_still_holds_the_five_minute_sweep_beside_the_daily_pair() -> None:
    """Catches a registration that REPLACES the registry rather than extending
    it — a `REGISTRY = {...}` rebuilt from this change's two jobs, which silently
    stops the expiry sweep and would show up nowhere else in this
    file. Also catches both daily jobs being registered under one name, which
    a per-job case cannot see: a dict keyed by name loses one of them without
    complaining."""
    assert {"expire_grants", "expiry_warnings", "leaver_sweep"} <= set(REGISTRY)
    assert len({job.name for job in REGISTRY.values()}) == len(REGISTRY)


# --- the cadence gate on the real registry ----


def test_a_tick_leaves_both_daily_keys_with_a_one_day_ttl(
    scheduler_db: tuple[str, list[SeededTenant]],  # noqa: F811
    scheduler_redis_url: str,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """To end, on the REAL registry against real Postgres and
    Redis, so the literal per-tenant key names and the literal TTL are pinned
    together.

    Catches a lock taken with no expiry (the job would never run again after
    one tick, for ever), one taken on a key an operator would not find, and a
    lock RELEASED at the end of the run — the key itself is
    the cadence gate, so a released key means these jobs run every 60 seconds
    instead of every 24 hours. Both per-tenant keys are asserted PRESENT for
    each job, so a runner that deletes them all after the run fails here.
    """
    database_url, tenants = scheduler_db
    for job_name in (jobs.EXPIRY_WARNINGS_JOB_NAME, jobs.LEAVER_SWEEP_JOB_NAME):
        for tenant in tenants:
            _delete_key(scheduler_redis_url, tenant_lock_key(job_name, tenant.tenant_id))

    _run_tick(database_url, scheduler_redis_url, dict(REGISTRY), monkeypatch=monkeypatch)

    for job_name in (jobs.EXPIRY_WARNINGS_JOB_NAME, jobs.LEAVER_SWEEP_JOB_NAME):
        for tenant in tenants:
            key = tenant_lock_key(job_name, tenant.tenant_id)
            ttl = _get_key_ttl(scheduler_redis_url, key)
            assert ttl == DAILY_CADENCE_SECONDS, f"expected TTL 86400 for {key}, got {ttl}"
            assert ttl == 86400


# --- the wrappers (two lines each, deciding nothing) ----------------


def test_the_expiry_warnings_wrapper_hands_its_session_and_now_to_the_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a wrapper that decides anything at all.

    Two specific wrong implementations: one that reads its own clock
    (`datetime.now(UTC)`) instead of passing the tick's instant, which breaks
    the guarantee that the read selecting the band and the row it writes
    answer for ONE moment and makes the job untestable at a fixed `now`; and
    one that opens its own session or re-binds a tenant, which would sweep
    outside the tenant the runner bound. The double is not a database handle,
    so a wrapper doing either of those cannot even reach a table — it can only
    pass this by handing on exactly what it was given.
    """
    calls: list[tuple[object, datetime]] = []
    monkeypatch.setattr(jobs, "warn_expiring_grants", _recorder(calls))
    session = cast(AsyncSession, _FAKE_SESSION)

    asyncio.run(jobs.run_expiry_warnings(session, NOW))

    assert calls == [(_FAKE_SESSION, NOW)]


def test_the_leaver_sweep_wrapper_hands_its_session_and_now_to_the_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same claim for the second job, which has its own wrapper and its own
    way of getting it wrong.

    One fix at one call site is not the fix. A leaver sweep
    reading its own clock would decide "who is due" against a different day
    from the one the tick answers for — the boundary case
    (`end_date < now.date()`) sits exactly there, so a person's last working
    day is what a clock read a second past midnight would cost.
    """
    calls: list[tuple[object, datetime]] = []
    monkeypatch.setattr(jobs, "sweep_leavers", _recorder(calls))
    session = cast(AsyncSession, _FAKE_SESSION)

    asyncio.run(jobs.run_leaver_sweep(session, NOW))

    assert calls == [(_FAKE_SESSION, NOW)]
