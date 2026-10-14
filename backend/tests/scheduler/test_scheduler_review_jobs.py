"""An earlier change §6: the two review jobs, their registrations and their wrappers.

Written before the implementation exists. Expected surface (the implementer
must provide exactly this):

``scheduler.jobs``
    ``REVIEW_GENERATION_JOB_NAME = "review_generation"``
    ``REVIEW_REMINDERS_JOB_NAME = "review_reminders"``
    ``async def run_review_generation(session, now) -> None`` — calls
    ``helpdesk.access.review_schedule.generate_reviews(session,
    SimulatedDirectory(session), now=now)`` and drops the return value.
    ``async def run_review_reminders(session, now) -> None`` — calls
    ``helpdesk.access.review_schedule.send_review_reminders(session, now=now)``
    and drops the return value.

    The two WRAPPER names are not fixed by plan §6; they follow the convention
    `jobs.py` already carries (`run_expire_grants`, `run_expiry_warnings`,
    `run_leaver_sweep`), which is why they are written out here rather than
    discovered.

``scheduler.runner``
    ``REGISTRY`` gains both jobs, each at the existing
    ``DAILY_CADENCE_SECONDS`` (86400), under names that pass the existing
    ``check_job_name``.

What this file deliberately does NOT re-test: `check_job_name`'s inclusive
boundary (exactly 64 accepted, 65 refused) and its `re.fullmatch` rejection
table. Both are already pinned in `test_scheduler_runner.py`, and this plan
adds no new value that shapes a key — only two constants of its own, which are
asserted to PASS that same gate below (plan §1's input-validation criterion,
recorded rather than silently skipped). A copy of those cases here would be
red and green for exactly the same reasons as the originals — the
neighbouring-guard trap.

a project rule: the database and the dedicated Redis index are
`test_scheduler_runner.py`'s session-scoped fixtures, imported rather than
rebuilt. Per-test isolation stays what that file established: the only keys
this file writes are the two the plan names, and the one case that touches
them deletes exactly those two first.

a project rule: `test_scheduler_runner.py`, `test_scheduler_daily_jobs.py`
and `test_scheduler_review_jobs.py` are three distinct basenames.

No pytest-asyncio: each case is a sync function driving one `asyncio.run`.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

# Imported, not redefined: one throwaway database with two
# seeded tenants and one dedicated Redis index for the whole run.
from test_scheduler_runner import (  # noqa: F401
    NOW,
    _delete_key,
    _run_tick,
    scheduler_db,
    scheduler_redis_url,
)

from helpdesk.scheduler import jobs
from helpdesk.scheduler.runner import (
    DAILY_CADENCE_SECONDS,
    REGISTRY,
    check_job_name,
    job_lock_key,
)
from tests.support.seed import SeededTenant

REVIEW_GENERATION_KEY = "helpdesk:joblock:review_generation"
REVIEW_REMINDERS_KEY = "helpdesk:joblock:review_reminders"
"""The literal keys an operator greps for with `KEYS helpdesk:joblock:*`.

Written out rather than composed, so this file fails if either the prefix or a
job name moves."""

_FAKE_SESSION = object()
"""The session double the wrapper cases hand through.

A bare object, never a database handle: the claim under test is "the wrapper
passes ITS arguments straight to the engine", and a real session would let a
wrapper that opened its own connection pass anyway."""


def _recorder(
    calls: list[tuple[object, datetime]],
) -> Callable[..., Awaitable[list[Any]]]:
    """An engine double that records the session and the `now` it was given.

    Takes `*args` so it stands in for both engines: `generate_reviews` is
    called with a directory beside the session and `send_review_reminders`
    without one, and neither the directory nor the return value is this
    file's claim.
    """

    async def engine(session: AsyncSession, *args: Any, now: datetime) -> list[Any]:
        calls.append((session, now))
        return []

    return engine


# --- the constants -----------------------------------------


def test_the_review_job_constants_are_exactly_the_planned_values() -> None:
    """Catches every one of this plan's new key-shaping literals drifting.

    The two names are asserted with the keys they produce, because the name is
    the only client-shaped part of a Redis key here: a job renamed without its
    key moving orphans the lock a running instance holds while the new one
    takes a fresh key with a fresh window, so a quarter's reviews could be
    generated twice on the day of a deploy. `DAILY_CADENCE_SECONDS` is
    re-asserted because this plan registers against it: the cadence IS the
    lock TTL (21a decision B), so a value of 3600 would open a quarter's
    reviews 24 times a day, each run skipped only by the already-open check,
    and would nag every reviewer 24 times about the same overdue review —
    decision E having deliberately removed the dedupe that would have hidden
    it.
    """
    assert jobs.REVIEW_GENERATION_JOB_NAME == "review_generation"
    assert jobs.REVIEW_REMINDERS_JOB_NAME == "review_reminders"
    assert job_lock_key(jobs.REVIEW_GENERATION_JOB_NAME) == REVIEW_GENERATION_KEY
    assert job_lock_key(jobs.REVIEW_REMINDERS_JOB_NAME) == REVIEW_REMINDERS_KEY
    assert DAILY_CADENCE_SECONDS == 86400


@pytest.mark.parametrize(
    ("job_name", "lock_key"),
    [
        pytest.param("review_generation", REVIEW_GENERATION_KEY, id="review-generation"),
        pytest.param("review_reminders", REVIEW_REMINDERS_KEY, id="review-reminders"),
    ],
)
def test_each_review_job_is_registered_at_the_daily_cadence_under_a_valid_name(
    job_name: str, lock_key: str
) -> None:
    """Plan §1's registration criterion, per job.

    Catches a job written and never registered — the whole plan lands, the
    library's tests pass, and nothing ever runs it, so no review is ever
    generated and no reviewer ever reminded. Catches one registered with its
    own cadence literal instead of the constant: the key's TTL IS the gate, so
    a job declaring 86400 while its key is set for 300 runs 288 times a day.
    And it applies `check_job_name` to the REGISTERED name, which is where
    plan §1 puts the input validation for this plan's only two new
    key-shaping values — a name with a hyphen or a capital would then fail
    here rather than inside a `SET` at three in the morning.
    """
    assert job_name in REGISTRY
    job = REGISTRY[job_name]
    assert job.name == job_name
    assert job.cadence_seconds == DAILY_CADENCE_SECONDS
    assert job.cadence_seconds == 86400
    assert check_job_name(job.name) == job_name
    assert job_lock_key(job.name) == lock_key


def test_the_registry_holds_all_eight_jobs_under_eight_distinct_names() -> None:
    """Catches a registration that REPLACES the registry rather than extending
    it — a `REGISTRY = {...}` rebuilt from this plan's two jobs, which
    silently stops 21a's expiry sweep and 21b's daily pair and would show up
    nowhere else in this file. Also catches both new jobs being registered
    under one name, which a per-job case cannot see: a dict keyed by name
    loses one of them without complaining.

    Eight is counted off the code, not carried over from this file's previous
    row: `expire_grants`, `expiry_warnings`, `leaver_sweep`,
    this plan's two, an earlier change's `metrics_sweep`, an earlier change's `chain_anchor`,
    and an earlier change's `anchor_retention`.
    """
    assert {
        "expire_grants",
        "expiry_warnings",
        "leaver_sweep",
        "review_generation",
        "review_reminders",
        "metrics_sweep",
        "chain_anchor",
        "anchor_retention",
    } <= set(REGISTRY)
    assert len({job.name for job in REGISTRY.values()}) == len(REGISTRY)
    assert len(REGISTRY) == 8


# --- the cadence gate on the real registry (21a decision B) ------------------


def test_a_tick_leaves_both_review_keys_with_a_one_day_ttl(
    scheduler_db: tuple[str, list[SeededTenant]],  # noqa: F811
    scheduler_redis_url: str,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The registration criterion end to end, on the REAL registry against real
    Postgres and Redis, so the literal key names and the literal TTL are
    pinned together.

    Catches a lock taken with no expiry (the job would never run again after
    one tick, for ever), one taken on a key an operator would not find, and a
    lock RELEASED at the end of the run — 21a decision B makes the key itself
    the cadence gate, so a released key means these jobs run every 60 seconds
    instead of every 24 hours, re-reading every scope of every tenant and
    re-reminding every reviewer on each tick. Both keys are asserted PRESENT
    afterwards, which is also what a job RAISING would fail (21a decision C
    deletes the key of a job that raised), so this case doubles as the proof
    that both engines survive one real run over two seeded tenants.
    """
    database_url, _tenants = scheduler_db
    for key in (REVIEW_GENERATION_KEY, REVIEW_REMINDERS_KEY):
        _delete_key(scheduler_redis_url, key)

    ttls = _run_tick(database_url, scheduler_redis_url, dict(REGISTRY), monkeypatch=monkeypatch)

    assert ttls["review_generation"] == DAILY_CADENCE_SECONDS
    assert ttls["review_generation"] == 86400
    assert ttls["review_reminders"] == DAILY_CADENCE_SECONDS
    assert ttls["review_reminders"] == 86400


# --- the wrappers (plan §2: two lines each, deciding nothing) ----------------


def test_the_review_generation_wrapper_hands_its_session_and_now_to_the_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a wrapper that decides anything at all.

    Two specific wrong implementations. One that reads its own clock
    (`datetime.now(UTC)`) instead of passing the tick's instant: `now` decides
    the PERIOD every review of the run is filed under and the `due_at` every
    reviewer is held to, so a clock read here makes the job untestable at a
    fixed instant and lets one tick's scopes straddle a quarter boundary in
    two directions instead of one (plan §5). And one that opens its own
    session or re-binds a tenant, which would generate outside the tenant the
    runner bound. The double is not a database handle, so a wrapper doing
    either of those cannot even reach a table — it can only pass this by
    handing on exactly what it was given.
    """
    calls: list[tuple[object, datetime]] = []
    monkeypatch.setattr(jobs, "generate_reviews", _recorder(calls))
    session = cast(AsyncSession, _FAKE_SESSION)

    asyncio.run(jobs.run_review_generation(session, NOW))

    assert calls == [(_FAKE_SESSION, NOW)]


def test_the_review_reminders_wrapper_hands_its_session_and_now_to_the_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same claim for the second job, which has its own wrapper and its own
    way of getting it wrong.

    a project rule: one fix at one call site is not the fix. A reminder
    sweep reading its own clock would measure the overdue band and the
    fourteen-day escalation from a different instant than the tick answers
    for — and both of this plan's boundary pairs are written to the
    microsecond, so the two jobs of one tick could disagree about whether the
    review generated this morning is already overdue.
    """
    calls: list[tuple[object, datetime]] = []
    monkeypatch.setattr(jobs, "send_review_reminders", _recorder(calls))
    session = cast(AsyncSession, _FAKE_SESSION)

    asyncio.run(jobs.run_review_reminders(session, NOW))

    assert calls == [(_FAKE_SESSION, NOW)]
