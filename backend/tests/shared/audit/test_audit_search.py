"""An earlier change section 6, first block: the audit search query, against Postgres.

Surface under test (written before it exists):

``helpdesk.audit.search`` (new module, re-exported from ``helpdesk.audit``)
    ``SCAN_CAP: int`` — the bound on how many rows ONE call may read. The cap
    is on the READ, not on the caller's ``limit``: an earlier change decision A filters
    by scope in the handler, AFTER the read, so a page can come back short and
    the cursor is what makes the rest reachable.
    ``AuditSearchPage`` — ``events`` (the rows, ``seq`` DESC), ``lowest_seq``
    (the lowest ``seq`` this call scanned, ``None`` when nothing matched) and
    ``has_more`` (True exactly when ``SCAN_CAP`` rows were read, so the
    handler hands back a cursor only then).
    ``InvalidTimeRangeError(ToolError)`` — ``code = ErrorCode.VALIDATION``,
    raised for ``occurred_from`` later than ``occurred_to`` BEFORE any
    statement runs.
    ``search_audit_events(session, *, event_type, subject_type, subject_id,
    actor_ref, occurred_from, occurred_to, before_seq) -> AuditSearchPage`` —
    every filter optional, ``seq`` DESC, keyset by ``before_seq``.

The session is tenant-bound (``helpdesk.core.db.tenant_session``), so the
query carries no tenant argument at all and another tenant's rows are
invisible through the ORM guard.

Style follows ``test_audit_writer.py``: the schema comes from
``Base.metadata`` via ``_seed.schema_engine`` on the session-scoped throwaway
database (a project rule — one database for the whole run, never one per
test), and each scenario runs inside a single ``asyncio.run``. Per-test
isolation inside it is a namespace: every case makes its OWN tenant and
plants events under its own ``event_type`` marker, so no two cases can read
each other's rows.

Rows are planted directly rather than through ``audit.writer.append_event``:
search reads columns and never re-hashes, so the chain fields are fixture
detail here (each row gets a distinct fake digest, which is also how the
planted rows are matched back to their ``seq``).

No pytest-asyncio: each case is a sync function driving one ``asyncio.run``.
"""

import asyncio
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import event, insert, select
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from helpdesk.shared.core.db import tenant_session
from helpdesk.shared.core.errors import ErrorCode
from helpdesk.shared.models import AuditEvent, Tenant
from helpdesk.shared.audit.search import (
    SCAN_CAP,
    AuditSearchPage,
    InvalidTimeRangeError,
    search_audit_events,
)

# The plan's own number, asserted exactly by
# `test_the_scan_cap_is_read_exactly_and_hands_back_a_cursor` (standing rule
# 2). A literal here rather than a re-import so a widened cap fails a case
# instead of silently travelling with the module.
EXPECTED_SCAN_CAP = 500

# Four fixed instants, far apart and in the past, so nothing here depends on
# when the suite runs. The time-range cases filter at the MIDPOINTS below
# rather than at an instant a row carries: an earlier change takes no decision on
# whether `occurred_from` / `occurred_to` are inclusive, and a test must not
# invent one.
T1 = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
T2 = datetime(2026, 2, 1, 9, 0, tzinfo=UTC)
T3 = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
T4 = datetime(2026, 4, 1, 9, 0, tzinfo=UTC)
BETWEEN_T2_AND_T3 = datetime(2026, 2, 15, 9, 0, tzinfo=UTC)

ACTOR_ONE = str(uuid.UUID(int=1))
ACTOR_TWO = str(uuid.UUID(int=2))
SUBJECT_ONE = str(uuid.UUID(int=101))
SUBJECT_TWO = str(uuid.UUID(int=102))


@dataclass(frozen=True)
class PlantedEvent:
    """One obviously fake audit row, in the shape the search filters read."""

    event_type: str
    subject_type: str
    subject_id: str | None
    actor_ref: str | None
    occurred_at: datetime


async def _make_tenant(factory: async_sessionmaker[AsyncSession], slug: str) -> uuid.UUID:
    async with factory() as session:
        tenant = Tenant(slug=slug, name=f"Fake Tenant {slug}", residency="eu", settings={})
        session.add(tenant)
        await session.commit()
        return tenant.id


async def _plant(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    events: Sequence[PlantedEvent],
) -> list[int]:
    """Insert the events in order and return their database-assigned `seq`s.

    The digest columns carry a distinct fake value per row, which is what the
    seqs are matched back through: `seq` is an `Identity` column, so the
    fixture must not assume it can predict the numbers it will get.
    """
    marker = uuid.uuid4().hex
    digests = [f"{index:064x}" for index in range(len(events))]
    rows: list[dict[str, Any]] = [
        {
            "tenant_id": tenant_id,
            "occurred_at": planted.occurred_at,
            "actor_kind": "user",
            "actor_ref": planted.actor_ref,
            "session_id": None,
            "trace_id": f"fake-trace-{marker}-{index}",
            "event_type": planted.event_type,
            "subject_type": planted.subject_type,
            "subject_id": planted.subject_id,
            "payload": {"fake": True, "index": index},
            "prev_hash": digests[index - 1] if index else f"{0:064x}",
            "hash": digests[index],
        }
        for index, planted in enumerate(events)
    ]
    async with factory() as session:
        await session.execute(insert(AuditEvent), rows)
        await session.commit()

    async with tenant_session(factory, tenant_id) as session:
        result = await session.execute(select(AuditEvent).order_by(AuditEvent.seq))
        by_digest = {row.hash: row.seq for row in result.scalars().all()}
    return [by_digest[digest] for digest in digests]


def _seqs(page: AuditSearchPage) -> list[int]:
    return [row.seq for row in page.events]


# --- decision D: every filter narrows, and absent means "no filter" -------------

# The planting every narrowing case reads. Row 0 and row 2 share an
# `event_type` and a `subject_id`; rows 0 and 1 share a `subject_type` and an
# `actor_ref`; the four `occurred_at`s are strictly increasing. So each filter
# below selects a DIFFERENT pair, and no two filters can be confused for each
# other.
NARROWING_PLANTING: tuple[PlantedEvent, ...] = (
    PlantedEvent("fake.alpha", "user", SUBJECT_ONE, ACTOR_ONE, T1),
    PlantedEvent("fake.beta", "user", SUBJECT_TWO, ACTOR_ONE, T2),
    PlantedEvent("fake.alpha", "grant", SUBJECT_ONE, ACTOR_TWO, T3),
    PlantedEvent("fake.beta", "grant", SUBJECT_TWO, ACTOR_TWO, T4),
)

NARROWING_CASES: tuple[tuple[str, dict[str, Any], tuple[int, ...]], ...] = (
    ("event-type", {"event_type": "fake.alpha"}, (0, 2)),
    ("subject-type", {"subject_type": "user"}, (0, 1)),
    ("subject-id", {"subject_id": SUBJECT_ONE}, (0, 2)),
    ("actor-ref", {"actor_ref": ACTOR_ONE}, (0, 1)),
    ("occurred-from", {"occurred_from": BETWEEN_T2_AND_T3}, (2, 3)),
    ("occurred-to", {"occurred_to": BETWEEN_T2_AND_T3}, (0, 1)),
    ("no-filter", {}, (0, 1, 2, 3)),
)


@pytest.mark.parametrize(
    ("filters", "expected"),
    [case[1:] for case in NARROWING_CASES],
    ids=[case[0] for case in NARROWING_CASES],
)
def test_each_decision_d_filter_narrows_and_an_absent_one_does_not(
    session_factory: async_sessionmaker[AsyncSession],
    filters: dict[str, Any],
    expected: tuple[int, ...],
) -> None:
    """Criterion 3, one filter per row, over one shared planting.

    KILL, per row: dropping THAT filter from the WHERE clause. Every filtered
    row expects two of the four planted events, so an ignored filter returns
    four and fails; and because each filter selects a different pair, a
    handler that wired `subject_id` to the `event_type` column (or any other
    swap of the six) fails the row it was wired away from. The `no-filter`
    row is the other direction: an implementation that always applies a
    filter — a `WHERE event_type = ''` when the argument is absent, say —
    returns nothing and fails only here.

    The two time rows filter at a midpoint between two planted instants, not
    at an instant a row carries: an earlier change decides nothing about inclusivity,
    so pinning `>=` over `>` here would invent a decision the plan never took.
    """

    async def scenario() -> None:
        factory = session_factory
        tenant_id = await _make_tenant(factory, f"audit-search-{uuid.uuid4().hex[:8]}")
        seqs = await _plant(factory, tenant_id, NARROWING_PLANTING)

        async with tenant_session(factory, tenant_id) as session:
            page = await search_audit_events(session, **filters)

        assert _seqs(page) == sorted((seqs[index] for index in expected), reverse=True)
        assert page.has_more is False
        assert page.lowest_seq == min(seqs[index] for index in expected)

    asyncio.run(scenario())


# --- ordering and the keyset cursor ---------------------------------------------


def test_events_come_back_newest_first_and_before_seq_resumes_without_a_gap(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Criterion 2's ordering plus decision B's keyset, in one planting.

    KILL, first half: dropping `ORDER BY seq DESC`. The five rows are planted
    oldest first, so Postgres's natural read order is the exact REVERSE of
    what this asserts; an unordered query cannot pass by accident.

    KILL, second half: a `before_seq` compiled as `>=` rather than `<`, or
    applied to `occurred_at` instead of `seq`. The cursor row is asserted
    absent from the continuation and every earlier row absent too, so a
    boundary off by one repeats a row an operator would then see twice, and a
    filter on the wrong column returns the whole planting again.

    All five rows carry the SAME `occurred_at`, so `seq` is the only column
    that can order them: a walk that ordered by timestamp would come back in
    an arbitrary order and fail the descending assertion.
    """

    async def scenario() -> None:
        factory = session_factory
        tenant_id = await _make_tenant(factory, f"audit-search-{uuid.uuid4().hex[:8]}")
        planting = tuple(
            PlantedEvent("fake.walk", "user", SUBJECT_ONE, ACTOR_ONE, T1) for _ in range(5)
        )
        seqs = await _plant(factory, tenant_id, planting)

        async with tenant_session(factory, tenant_id) as session:
            first = await search_audit_events(session, event_type="fake.walk")
            second = await search_audit_events(session, event_type="fake.walk", before_seq=seqs[2])

        assert _seqs(first) == sorted(seqs, reverse=True)
        assert first.lowest_seq == seqs[0]
        assert first.has_more is False

        assert _seqs(second) == [seqs[1], seqs[0]]
        assert seqs[2] not in _seqs(second), "the cursor row is repeated on the next page"
        assert second.lowest_seq == seqs[0]
        assert second.has_more is False

    asyncio.run(scenario())


def test_the_scan_cap_is_read_exactly_and_hands_back_a_cursor(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Decision B's bound, at exactly cap and cap+1.

    KILL: an unbounded read, or a cap applied as `SCAN_CAP + 1` / `- 1`. A
    planting of exactly `SCAN_CAP + 1` rows is the only size that separates
    the three: an unbounded query returns all `SCAN_CAP + 1`, an off-by-one
    cap returns one too many or one too few, and only the exact cap returns
    `SCAN_CAP` with `has_more` True.

    The continuation is asserted too, because `has_more` without a usable
    cursor is a page nobody can walk past: the second call returns the single
    remaining row, does not repeat the last row of the first page, and
    reports `has_more` False — which is what tells the handler to stop
    handing `next_before_seq` back.

    `SCAN_CAP` itself is asserted as an exact number here: a cap that drifts
    changes how much work one call does against the one table the design
    never prunes.
    """
    assert SCAN_CAP == EXPECTED_SCAN_CAP

    async def scenario() -> None:
        factory = session_factory
        tenant_id = await _make_tenant(factory, f"audit-search-{uuid.uuid4().hex[:8]}")
        planting = tuple(
            PlantedEvent("fake.capped", "user", SUBJECT_ONE, ACTOR_ONE, T1)
            for _ in range(SCAN_CAP + 1)
        )
        seqs = await _plant(factory, tenant_id, planting)

        async with tenant_session(factory, tenant_id) as session:
            first = await search_audit_events(session, event_type="fake.capped")
            assert first.lowest_seq is not None
            second = await search_audit_events(
                session, event_type="fake.capped", before_seq=first.lowest_seq
            )

        assert len(first.events) == SCAN_CAP
        assert _seqs(first) == sorted(seqs[1:], reverse=True)
        assert first.has_more is True
        assert first.lowest_seq == seqs[1]

        assert _seqs(second) == [seqs[0]]
        assert second.has_more is False
        assert second.lowest_seq == seqs[0]

    asyncio.run(scenario())


# --- the tenant boundary (criterion 2) ------------------------------------------


def test_another_tenants_events_are_invisible_through_the_bound_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Criterion 2's "OWN tenant only", with nothing else able to hide the row.

    KILL: reading through anything that escapes `helpdesk.core.db`'s ORM
    guard — a `text()` query, a raw connection, or a select against a
    non-tenant-scoped entity. Both tenants are planted with the SAME
    `event_type`, the same `subject_type`, the same `subject_id` and the same
    `actor_ref`, and the search is called with no filter at all, so not one
    of decision D's filters can be the thing that withholds tenant B's rows.
    The only difference between the two sets is which tenant they belong to.

    The same page is then read through a session bound to tenant B, and it
    returns B's rows and only B's: a query that answered empty for everyone
    would pass the first half and fail here.
    """

    async def scenario() -> None:
        factory = session_factory
        marker = uuid.uuid4().hex[:8]
        tenant_a = await _make_tenant(factory, f"audit-search-a-{marker}")
        tenant_b = await _make_tenant(factory, f"audit-search-b-{marker}")
        shared = (PlantedEvent("fake.shared", "user", SUBJECT_ONE, ACTOR_ONE, T1),)
        seqs_a = await _plant(factory, tenant_a, shared)
        seqs_b = await _plant(factory, tenant_b, shared)

        async with tenant_session(factory, tenant_a) as session:
            page_a = await search_audit_events(session)
        async with tenant_session(factory, tenant_b) as session:
            page_b = await search_audit_events(session)

        assert _seqs(page_a) == seqs_a
        assert _seqs(page_b) == seqs_b
        assert set(_seqs(page_a)).isdisjoint(_seqs(page_b))

    asyncio.run(scenario())


# --- the refusal, before any read (criterion 9) ---------------------------------


def test_an_inverted_time_range_is_refused_validation_before_any_statement_runs(
    session_factory: async_sessionmaker[AsyncSession],
    shared_engine: AsyncEngine,
) -> None:
    """Criterion 9's "before any read", proved by counting statements.

    KILL: a range check placed AFTER the SELECT, or none at all. Without the
    counter both would look identical from the outside — an inverted range
    matches no row, so a query that ran anyway answers the same empty page —
    which is exactly what the criterion refuses: an empty page reads as "no
    such events", and a caller who transposed two dates would be told their
    audit trail is clean.

    The counter is proved non-vacuous in the same test: the identical call
    with the two instants the right way round DOES execute a statement, so a
    listener that never fires cannot make the first half pass.

    The code is asserted as `ErrorCode.VALIDATION` (the design), not just as
    "some exception": an inverted range refused as `INTERNAL` tells the
    caller the service is broken rather than that their input is.
    """

    async def scenario() -> None:
        statements: list[str] = []
        _watch(shared_engine, statements)
        factory = session_factory
        tenant_id = await _make_tenant(factory, f"audit-search-{uuid.uuid4().hex[:8]}")
        await _plant(
            factory,
            tenant_id,
            (PlantedEvent("fake.range", "user", None, None, T2),),
        )

        async with tenant_session(factory, tenant_id) as session:
            before = len(statements)
            with pytest.raises(InvalidTimeRangeError) as refused:
                await search_audit_events(session, occurred_from=T3, occurred_to=T1)
            ran = statements[before:]
            assert not ran, f"the refusal ran {len(ran)} statement(s): {ran}"

            accepted = await search_audit_events(session, occurred_from=T1, occurred_to=T3)
            assert len(statements) > before, "the statement counter never fired"

        assert refused.value.code is ErrorCode.VALIDATION
        assert len(accepted.events) == 1

    asyncio.run(scenario())


def _watch(engine: AsyncEngine, statements: list[str]) -> None:
    """Record every statement this engine sends to Postgres."""

    def _record(
        conn: Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
