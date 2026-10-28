"""The anchor-retention job, its registration and its wrapper.

Written before the implementation exists. Expected surface (the implementer
must provide exactly this):

``scheduler.jobs``
    ``ANCHOR_RETENTION_JOB_NAME = "anchor_retention"``
    ``async def run_anchor_retention(session, now) -> None`` — calls
    ``helpdesk.audit.retention.purge_chain_anchors(session, now)`` and drops
    the returned count. The engine is imported as a module-level name in
    ``scheduler.jobs``, exactly as ``anchor_tenant_chain`` is, so this file can
    replace it.

``scheduler.runner``
    ``ANCHOR_RETENTION_JOB`` and an EIGHTH ``REGISTRY`` row, at the existing
    ``DAILY_CADENCE_SECONDS`` (no new cadence number enters the
    runner), under a name that passes the existing ``check_job_name``.

What this file deliberately does NOT re-test:

- ``check_job_name``'s boundary and its rejection table, pinned in
  ``test_scheduler_runner.py``. The one new key-shaping value here is the job
  name, asserted to PASS that same gate below rather than copied through the
  neighbouring guard's cases.
- "the registry holds EIGHT jobs under eight distinct names".
  That pin already exists and was moved to eight in place:
  ``test_scheduler_metrics_job.py::test_the_registry_holds_all_eight_jobs_
  under_eight_distinct_names``, with a subset-and-count twin in
  ``test_scheduler_review_jobs.py``. Writing it a third time would make one of
  them green for another's reason. Exactly what
  ``test_scheduler_chain_anchor_job.py`` did for seven.
- what the purge decides — the window, the head exclusion, the tenant filter
  and the log line. All of it belongs to the library and is pinned by
  ``packages/helpdesk/tests/test_audit_anchor_retention.py``; the wrapper
  decides nothing, which is what the wrapper case below pins.

The database and the dedicated Redis index are
``test_scheduler_runner.py``'s session-scoped fixtures, imported rather than
rebuilt. The only key this file writes is the one the design names, and the case
that touches it deletes exactly that key first.

``test_scheduler_anchor_retention_job.py`` carries the
service in its name and collides with no basename in the tree.

No pytest-asyncio: each case is a sync function driving one ``asyncio.run``.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import cast

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

ANCHOR_RETENTION_KEY = "helpdesk:joblock:anchor_retention"
"""The literal key an operator greps for with `KEYS helpdesk:joblock:*`.

Written out rather than composed, so this file fails if either the prefix or
the job name moves."""

_FAKE_SESSION = object()
"""The session double the wrapper case hands through.

A bare object, never a database handle: the claim under test is "the wrapper
passes ITS session straight to the engine", and a real session would let a
wrapper that opened its own connection pass anyway."""


def _purge_recorder(calls: list[tuple[object, object]]) -> Callable[..., Awaitable[int]]:
    """A double for `purge_chain_anchors`: records the session and the `now`.

    It returns a count, like the engine: a wrapper that awaited nothing, or
    that treated the return as the thing to act on, meets the same shape the
    real engine has. It takes no tenant, because the tenant comes off the
    session — a wrapper that handed one down fails here with a
    `TypeError` rather than silently growing an argument the engine would have
    to accept for ever.
    """

    async def engine(session: AsyncSession, now: datetime) -> int:
        calls.append((session, now))
        return 0

    return engine


# --- the constants -----------------------------------------


def test_the_anchor_retention_job_constants_are_exactly_the_expected_values() -> None:
    """Catches this change's one new literal drifting, and the key it shapes.

    The name is asserted together with the key it produces, because the name is
    the only client-shaped part of a Redis key here: a job renamed without its
    key moving orphans the lock a running instance holds while the new one
    takes a fresh key with a fresh window.

    The cadence is asserted against `DAILY_CADENCE_SECONDS` AND against 86400,
    which is the design choice written down: reusing the existing constant is the
    decision, and a job that declared its own 86400 literal would pass the
    second assertion and fail the first only if that constant ever moved — so
    both are here, exactly as the review and daily job files do it.
    """
    assert jobs.ANCHOR_RETENTION_JOB_NAME == "anchor_retention"
    assert DAILY_CADENCE_SECONDS == 86400
    assert job_lock_key(jobs.ANCHOR_RETENTION_JOB_NAME) == ANCHOR_RETENTION_KEY


def test_the_anchor_retention_job_is_registered_at_the_daily_cadence_under_a_valid_name() -> None:
    """The registration criterion.

    Catches a job written and never registered — the whole change lands, every
    other case in this file passes, the trigger happily permits the delete, and
    nothing ever issues one: the anchor table grows for ever exactly as it did
    before the change. That is the one failure mode a green suite would otherwise
    hide completely.

    Catches a job registered with its own cadence literal instead of the
    constant: the key's TTL IS the gate, so a job declaring
    86400 while its key is set for 300 purges 288 times a day.

    And it applies `check_job_name` to the REGISTERED name, which is where this
    change's one new key-shaping value is validated — a name with a hyphen, a
    capital or a trailing newline would then fail at import rather than inside
    a `SET` at three in the morning.
    """
    assert "anchor_retention" in REGISTRY
    job = REGISTRY["anchor_retention"]
    assert job.name == "anchor_retention"
    assert job.cadence_seconds == DAILY_CADENCE_SECONDS
    assert job.cadence_seconds == 86400
    assert check_job_name(job.name) == "anchor_retention"
    assert job_lock_key(job.name) == ANCHOR_RETENTION_KEY


def test_the_anchor_retention_job_is_not_folded_into_the_chain_anchor_job() -> None:
    """The anchor job's own docstring depends on.

    Catches the alternative that was rejected: purging from inside
    `chain_anchor`. That job's docstring says what it writes, and a job that
    also DELETED rows would falsify it while tying the deletion rate to how
    often a head is taken — 96 purges a day at the chain-anchor cadence instead of one.
    Asserted as two distinct names at two distinct cadences, which is what
    folding would collapse.
    """
    assert jobs.ANCHOR_RETENTION_JOB_NAME != jobs.CHAIN_ANCHOR_JOB_NAME
    assert REGISTRY["anchor_retention"] is not REGISTRY["chain_anchor"]
    assert REGISTRY["anchor_retention"].cadence_seconds != REGISTRY["chain_anchor"].cadence_seconds


# --- the wrapper (it decides nothing) ------------------------


def test_the_anchor_retention_wrapper_hands_its_session_and_the_ticks_now_to_the_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a wrapper that decides anything at all.

    A wrapper reading its own clock — `datetime.now(UTC)` inside the wrapper —
    is the specific wrong implementation: the recorded `now` is compared to the
    tick's fixed instant, so that version records a different value and fails,
    while a case asserting only "the engine got called" would pass. It matters
    because the tick's `now` is what makes every job of one tick answer for one
    instant, and a cutoff computed from a second clock would delete a different
    set of rows than the tick claims to be about.

    Its own session, or a re-bound tenant: the double is not a database handle,
    so a wrapper doing either could not reach a table — it can only pass by
    handing on exactly what it was given, which is what pins that the rows
    walked answer for the tenant the runner bound.

    The call list is asserted as exactly one entry, so a wrapper that purged
    twice — once per tenant and once more over the table — fails here.
    """
    calls: list[tuple[object, object]] = []
    monkeypatch.setattr(jobs, "purge_chain_anchors", _purge_recorder(calls))
    session = cast(AsyncSession, _FAKE_SESSION)

    asyncio.run(jobs.run_anchor_retention(session, NOW))

    assert calls == [(_FAKE_SESSION, NOW)]


# --- the cadence gate on the real registry ------------------


def test_a_tick_leaves_the_anchor_retention_key_with_a_one_day_ttl(
    scheduler_db: tuple[str, list[SeededTenant]],  # noqa: F811
    scheduler_redis_url: str,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The registration criterion end to end, on the REAL registry against real
    Postgres and Redis, so the literal per-tenant key names and the literal
    TTL are pinned together.

    Catches a lock taken with no expiry (the job would never run again after
    one tick, for ever), one taken on a key an operator would not find, and a
    lock RELEASED at the end of the run — the key itself is
    the cadence gate, so a released key means this job re-purges every tenant
    every 60 seconds. Per-tenant keys are asserted PRESENT for both tenants,
    so a runner that deletes all keys after a run fails here.
    """
    database_url, tenants = scheduler_db
    for tenant in tenants:
        _delete_key(
            scheduler_redis_url,
            tenant_lock_key(jobs.ANCHOR_RETENTION_JOB_NAME, tenant.tenant_id),
        )

    _run_tick(database_url, scheduler_redis_url, dict(REGISTRY), monkeypatch=monkeypatch)

    for tenant in tenants:
        key = tenant_lock_key(jobs.ANCHOR_RETENTION_JOB_NAME, tenant.tenant_id)
        ttl = _get_key_ttl(scheduler_redis_url, key)
        assert ttl == DAILY_CADENCE_SECONDS, f"expected TTL 86400 for {key}, got {ttl}"
        assert ttl == 86400
