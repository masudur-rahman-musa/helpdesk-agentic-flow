"""An earlier change section 6: the chain-anchor job, its registration and its wrapper.

Written before the implementation exists. Expected surface (the implementer
must provide exactly this):

``scheduler.jobs``
    ``CHAIN_ANCHOR_JOB_NAME = "chain_anchor"``
    ``async def run_chain_anchor(session, now) -> None`` — calls
    ``helpdesk.audit.anchor.anchor_tenant_chain(session)`` and drops the
    return value. The engine is imported as a module-level name in
    ``scheduler.jobs``, exactly as ``refresh_chain_gauge`` is, so this file
    can replace it.

``scheduler.runner``
    ``CHAIN_ANCHOR_CADENCE_SECONDS = 900`` (plan §1) — the cadence and, by
    21a decision B, the ``PX`` of the lock key.
    ``CHAIN_ANCHOR_JOB`` and a seventh ``REGISTRY`` row, under a name that
    passes the existing ``check_job_name``.

What this file deliberately does NOT re-test:

- ``check_job_name``'s boundary and its rejection table, pinned in
  ``test_scheduler_runner.py``. The one new key-shaping value here is the job
  name, which is asserted to PASS that same gate below rather than copied
  through the neighbouring guard's cases.
- "the registry holds SEVEN jobs under seven distinct names" (plan §1 and
  §6). That pin already exists and plan §8 moves it in place:
  ``test_scheduler_metrics_job.py::test_the_registry_holds_all_eight_jobs_
  under_eight_distinct_names``, which an earlier change moved on to eight when it
  registered the retention sweep. Writing it twice would make one of the two
  green for the other's reason.
- "the wrapper verifies before it anchors, asserted on the call order" (§6).
  The wrapper makes ONE call: ``anchor_tenant_chain`` verifies inside itself,
  and a wrapper that verified as well would be a guard the handler runs twice
  and therefore unkillable. The ordering is pinned where
  it is decided, by
  ``packages/helpdesk/tests/test_audit_anchor.py::test_a_tampered_chain_
  writes_no_anchor``, which stores no anchor for a log that does not verify.

a project rule: the database and the dedicated Redis index are
``test_scheduler_runner.py``'s session-scoped fixtures, imported rather than
rebuilt. The only key this file writes is the one the plan names, and the case
that touches it deletes exactly that key first.

a project rule: ``test_scheduler_chain_anchor_job.py`` carries the service in
its name and collides with no basename in the tree.

No pytest-asyncio: each case is a sync function driving one ``asyncio.run``.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.scheduler import jobs
from helpdesk.scheduler.runner import (
    CHAIN_ANCHOR_CADENCE_SECONDS,
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

CHAIN_ANCHOR_KEY = "helpdesk:joblock:chain_anchor"
"""The literal key an operator greps for with `KEYS helpdesk:joblock:*`.

Written out rather than composed, so this file fails if either the prefix or
the job name moves."""

_FAKE_SESSION = object()
"""The session double the wrapper case hands through.

A bare object, never a database handle: the claim under test is "the wrapper
passes ITS session straight to the engine", and a real session would let a
wrapper that opened its own connection pass anyway."""


def _anchor_recorder(calls: list[object]) -> Callable[..., Awaitable[None]]:
    """A double for `anchor_tenant_chain`: records the session it was given.

    It takes no `now` on purpose, and no tenant. The anchored head is about
    the rows that exist, not about an instant, and the tenant comes off the
    session — so a wrapper that handed either down fails here
    with a `TypeError` rather than silently growing an argument the engine
    would have to accept for ever.
    """

    async def engine(session: AsyncSession) -> None:
        calls.append(session)

    return engine


# --- the constants -----------------------------------------


def test_the_chain_anchor_job_constants_are_exactly_the_planned_values() -> None:
    """Catches this plan's two new literals drifting.

    `CHAIN_ANCHOR_CADENCE_SECONDS` is the lock TTL by construction (21a
    decision B), so it is also the window in which a truncation is invisible:
    at 86400 an attacker has a whole day between the last anchored head and
    the next one, and everything they delete inside it is never covered by any
    anchor. 900 is the number plan §1 fixes, and nothing else in the suite
    would show it moving.

    The name is asserted with the key it produces, because the name is the
    only client-shaped part of a Redis key here: a job renamed without its key
    moving orphans the lock a running instance holds while the new one takes a
    fresh key with a fresh window.
    """
    assert jobs.CHAIN_ANCHOR_JOB_NAME == "chain_anchor"
    assert CHAIN_ANCHOR_CADENCE_SECONDS == 900
    assert job_lock_key(jobs.CHAIN_ANCHOR_JOB_NAME) == CHAIN_ANCHOR_KEY


def test_the_chain_anchor_job_is_registered_at_its_cadence_under_a_valid_name() -> None:
    """Plan §1's registration criterion.

    Catches a job written and never registered — the whole plan lands, every
    other case in this file passes, and no head is ever anchored, so
    `verify_tenant_chain` keeps answering `ok` on a truncated log exactly as
    it did before the plan. That is the one failure mode a green suite would
    otherwise hide completely.

    Catches a job registered with its own cadence literal instead of the
    constant: the key's TTL IS the gate, so a job declaring 900 while its key
    is set for 86400 anchors once a day.

    And it applies `check_job_name` to the REGISTERED name, which is where
    plan §1's input validation for this plan's one new key-shaping value
    belongs — a name with a hyphen, a capital or a trailing newline would then
    fail at import rather than inside a `SET` at three in the morning.
    """
    assert "chain_anchor" in REGISTRY
    job = REGISTRY["chain_anchor"]
    assert job.name == "chain_anchor"
    assert job.cadence_seconds == CHAIN_ANCHOR_CADENCE_SECONDS
    assert job.cadence_seconds == 900
    assert check_job_name(job.name) == "chain_anchor"
    assert job_lock_key(job.name) == CHAIN_ANCHOR_KEY


def test_the_chain_anchor_job_is_not_folded_into_the_metrics_sweep() -> None:
    """Plan decision C, which two docstrings in the source depend on.

    Catches the alternative that was rejected: anchoring from inside
    `metrics_sweep`. `jobs.py` says that sweep "changes nothing at all" and
    `audit/health.py` says it "writes no event"; a reading job that starts
    writing rows falsifies both, and ties the anchor's write rate to a gauge's
    scrape freshness. Asserted as two distinct names at two distinct cadences,
    which is what folding would collapse.
    """
    assert jobs.CHAIN_ANCHOR_JOB_NAME != jobs.METRICS_SWEEP_JOB_NAME
    assert REGISTRY["chain_anchor"] is not REGISTRY["metrics_sweep"]
    assert REGISTRY["chain_anchor"].cadence_seconds != REGISTRY["metrics_sweep"].cadence_seconds


# --- the wrapper (plan §1: it decides nothing) -------------------------------


def test_the_chain_anchor_wrapper_hands_its_session_to_the_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a wrapper that decides anything at all.

    Its own session, or a re-bound tenant: the double is not a database
    handle, so a wrapper doing either could not reach a table — it can only
    pass this by handing on exactly what it was given, which is what pins that
    the chain walked and the head stored answer for the SAME tenant, the one
    the runner bound.

    A clock read, or the tick's `now` handed down: the double takes no `now`,
    so either fails with a `TypeError`. An anchor is a statement about the
    rows that exist, and an instant in it would be a second, disagreeing
    record of when the head was taken.

    The call list is asserted as exactly one entry, so a wrapper that anchors
    twice — once before verifying, once after — fails here.
    """
    calls: list[object] = []
    monkeypatch.setattr(jobs, "anchor_tenant_chain", _anchor_recorder(calls))
    session = cast(AsyncSession, _FAKE_SESSION)

    asyncio.run(jobs.run_chain_anchor(session, NOW))

    assert calls == [_FAKE_SESSION]


# --- the cadence gate on the real registry (21a decision B) ------------------


def test_a_tick_leaves_the_chain_anchor_key_with_a_nine_hundred_second_ttl(
    scheduler_db: tuple[str, list[SeededTenant]],  # noqa: F811
    scheduler_redis_url: str,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The registration criterion end to end, on the REAL registry against real
    Postgres and Redis, so the literal per-tenant key names and the literal
    TTL are pinned together.

    Catches a lock taken with no expiry (the job would never run again after
    one tick, for ever), one taken on a key an operator would not find, and a
    lock RELEASED at the end of the run — 21a decision B makes the key itself
    the cadence gate, so a released key means this job re-walks and re-anchors
    every tenant every 60 seconds instead of every 900. Per-tenant keys are
    asserted PRESENT for both tenants, so a runner that deletes all keys after
    a run fails here.
    """
    database_url, tenants = scheduler_db
    for tenant in tenants:
        _delete_key(
            scheduler_redis_url,
            tenant_lock_key("chain_anchor", tenant.tenant_id),
        )

    _run_tick(database_url, scheduler_redis_url, dict(REGISTRY), monkeypatch=monkeypatch)

    for tenant in tenants:
        key = tenant_lock_key("chain_anchor", tenant.tenant_id)
        ttl = _get_key_ttl(scheduler_redis_url, key)
        assert ttl == CHAIN_ANCHOR_CADENCE_SECONDS, f"expected TTL 900 for {key}, got {ttl}"
        assert ttl == 900
