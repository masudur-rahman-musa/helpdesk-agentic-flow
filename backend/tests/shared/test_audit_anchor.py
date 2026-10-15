"""An earlier change section 6: the anchored chain head, and what it makes detectable.

Written before the implementation exists. Expected surface (the implementer
must provide exactly this; the names below are this file's, and a different
spelling in the implementation is a rename here, not a redesign):

``helpdesk.audit.chain`` (pure: no I/O, no clock, no models, no services)
    ``ANCHOR_HASH_PATTERN = r"[0-9a-f]{64}"`` · ``MIN_ANCHOR_SEQ = 1``
    ``class InvalidChainAnchorError(ToolError)`` — ``ErrorCode.VALIDATION``
    ``class ChainAnchor(BaseModel)`` — ``audit_seq: int`` and
    ``audit_hash: str``, both checked in a ``field_validator`` with
    ``re.fullmatch`` and refused with the typed error
    BEFORE any INSERT is built.

``helpdesk.audit.anchor`` (the thin I/O half, beside `writer` and `verify`)
    ``class AnchorRegressionError(ToolError)`` — ``ErrorCode.VALIDATION``
    ``async def latest_chain_anchor(session, tenant_id) -> ChainAnchor | None``
        — the tenant's anchor with the HIGHEST ``audit_seq``.
    ``async def write_chain_anchor(session, tenant_id, anchor) -> None``
        — the no-regress check, the INSERT, and one log line.
    ``async def anchor_tenant_chain(session) -> ChainAnchor | None``
        — verify the chain the session's bound tenant carries and, only when
        the verdict is ``ok`` and the tenant has at least one event, build the
        anchor from its last event and write it. The tenant comes off the
        session, never off an argument, exactly as
        ``audit.health.refresh_chain_gauge`` takes it.

``helpdesk.models`` — ``AuditChainAnchor`` on ``audit_chain_anchors``.

Why the write is TWO functions rather than the one §6 names: §1 requires a
write behind the stored head to be refused with a typed error, and a writer
that derives its own ``audit_seq`` from the last event can never present one.
The derivation lives in ``anchor_tenant_chain`` (where "a tampered chain
writes no anchor" is the kill) and the refusal in ``write_chain_anchor``
(where the regression and the unique constraint are reachable at all).

a project rule: the session-scoped `migrated_db_url` from `conftest.py`, with
an earlier change's `restricted_db_url` over that SAME database for the privilege
cases. Per-test isolation is a fresh tenant per case, never a fresh database;
only the Alembic round-trip takes `fresh_db_url`, because it downgrades.

a project rule: `test_audit_anchor.py` is a basename no other test file in
the tree carries.

No pytest-asyncio: each case is a sync function driving one `asyncio.run`.
"""

import asyncio
import json
import logging
import os
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from prometheus_client import CollectorRegistry
from sqlalchemy import BigInteger, DateTime, String, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from helpdesk.scheduler.jobs.metrics_sweep import refresh_chain_gauge, reset_chain_verdicts
from helpdesk.shared.audit.anchor import (
    AnchorRegressionError,
    anchor_tenant_chain,
    latest_chain_anchor,
    write_chain_anchor,
)
from helpdesk.shared.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.shared.core.errors import ErrorCode
from helpdesk.shared.core.observability import metrics
from helpdesk.shared.core.observability.logging import FORBIDDEN_KEYS
from helpdesk.shared.models import Base
from helpdesk.shared.services.audit import AuditEventInput
from helpdesk.shared.services.audit.chain import (
    ANCHOR_HASH_PATTERN,
    MIN_ANCHOR_SEQ,
    ChainAnchor,
    InvalidChainAnchorError,
    VerifyResult,
)
from helpdesk.shared.services.audit.verify import verify_tenant_chain
from helpdesk.shared.services.audit.writer import append_event
from tests.support.seed import TENANT_SCOPED_TABLES

REPO_ROOT = Path(__file__).resolve().parents[3]

ANCHOR_TABLE = "audit_chain_anchors"
"""The table name, written out: it is half of every grant and every trigger."""

ANCHOR_LOG_MESSAGE = "audit.chain.anchored"
"""Decision B's external witness. The message an operator greps the shipped
logs for, so it is pinned as a literal."""

ANCHOR_LOGGER = "helpdesk.shared.audit.anchor"
"""Where that line is emitted from: the module that owns the INSERT."""

ROLE_NAME = "helpdesk_app"
"""An earlier change decision C, repeated here for the privilege cases below."""

ANCHOR_DOWNGRADE_TARGET = "c7f4a1d2e8b3"
"""An earlier change's revision: the one immediately below this plan's own.

Named as a literal because the round trip below must undo THIS revision, and
`-1` counts from whatever the head happens to be."""

INSUFFICIENT_PRIVILEGE = "42501"
TRIGGER_RAISED = "P0001"
"""42501 is `insufficient_privilege`, P0001 what an append-only trigger
raises. Which one arrives is the whole point of the privilege cases, exactly
as in `test_audit_db_role.py`."""

SAMPLE_HASH = "0123456789abcdef" * 4
"""A 64-character lowercase hex string using every hex digit. Obviously fake,
and never the digest of anything."""

AUDIT_CHAIN_OK = "helpdesk_audit_chain_ok"
"""The exposed gauge name an alert queries."""


# --- helpers ------------------------------------------------------------------


@asynccontextmanager
async def _engine(url: str) -> AsyncIterator[AsyncEngine]:
    """An engine that lives and dies inside one `asyncio.run` loop."""
    engine = create_engine(url)
    try:
        yield engine
    finally:
        await engine.dispose()


def _sqlstate(exc: DBAPIError) -> str:
    """The Postgres SQLSTATE off the driver exception, never the message."""
    orig = exc.orig
    sqlstate: object = getattr(orig, "sqlstate", None)
    assert isinstance(sqlstate, str), f"no SQLSTATE on the driver exception {orig!r}"
    return sqlstate


def _event(index: int) -> AuditEventInput:
    """One obviously fake event; `index` is what makes each row differ."""
    return AuditEventInput(
        occurred_at=None,
        actor_kind="system",
        actor_ref=str(uuid.UUID(int=index)),
        session_id=None,
        trace_id=f"trace-fake-30bb-{index}",
        event_type="ticket.created",
        subject_type="ticket",
        subject_id=str(uuid.UUID(int=3000 + index)),
        payload={"fake": True, "index": index},
    )


def _tenant_with_events(owner_url: str, events: int) -> uuid.UUID:
    """A fresh tenant of this case's own, carrying `events` chained events.

    The per-case namespace: every case plants its own tenant
    in the ONE session-scoped migrated database, so no case can see another's
    anchors and nothing is rebuilt per test.
    """

    async def scenario() -> uuid.UUID:
        tenant_id = uuid.uuid4()
        slug = f"anchor-{uuid.uuid4().hex[:10]}"
        async with _engine(owner_url) as engine:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO tenants (id, slug, name, residency, settings) "
                        "VALUES (:id, :slug, :name, 'eu', CAST(:settings AS jsonb))"
                    ),
                    {
                        "id": tenant_id,
                        "slug": slug,
                        "name": f"Fake Tenant {slug}",
                        "settings": "{}",
                    },
                )
            factory = create_session_factory(engine)
            for index in range(1, events + 1):
                async with tenant_session(factory, tenant_id) as session:
                    await append_event(session, tenant_id, _event(index))
        return tenant_id

    return asyncio.run(scenario())


def _last_event(owner_url: str, tenant_id: uuid.UUID) -> tuple[int, str]:
    """The tenant's highest-`seq` event as (seq, hash), read outside the code."""

    async def scenario() -> tuple[int, str]:
        async with _engine(owner_url) as engine, engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT seq, hash FROM audit_events WHERE tenant_id = :t "
                        "ORDER BY seq DESC LIMIT 1"
                    ),
                    {"t": tenant_id},
                )
            ).one()
        return int(row.seq), str(row.hash)

    return asyncio.run(scenario())


def _anchor_chain(owner_url: str, tenant_id: uuid.UUID) -> ChainAnchor | None:
    """`anchor_tenant_chain` on a session bound to `tenant_id`, committed."""

    async def scenario() -> ChainAnchor | None:
        async with _engine(owner_url) as engine:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant_id) as session:
                written = await anchor_tenant_chain(session)
                await session.commit()
        return written

    return asyncio.run(scenario())


def _write_anchor(owner_url: str, tenant_id: uuid.UUID, anchor: ChainAnchor) -> None:
    """`write_chain_anchor` with an anchor the CALLER chose, committed."""

    async def scenario() -> None:
        async with _engine(owner_url) as engine:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant_id) as session:
                await write_chain_anchor(session, tenant_id, anchor)
                await session.commit()

    asyncio.run(scenario())


def _stored_anchors(owner_url: str, tenant_id: uuid.UUID) -> list[tuple[int, str]]:
    """Every anchor row the tenant carries, by `audit_seq`, read as the owner."""

    async def scenario() -> list[tuple[int, str]]:
        async with _engine(owner_url) as engine, engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        f"SELECT audit_seq, audit_hash FROM {ANCHOR_TABLE} "
                        "WHERE tenant_id = :t ORDER BY audit_seq"
                    ),
                    {"t": tenant_id},
                )
            ).all()
        return [(int(row.audit_seq), str(row.audit_hash)) for row in rows]

    return asyncio.run(scenario())


def _plant_anchor_row(
    owner_url: str, tenant_id: uuid.UUID, audit_seq: int, audit_hash: str, anchored_at: datetime
) -> None:
    """One anchor row planted as the OWNER, around the writer under test.

    The reader case needs two rows whose `audit_seq` and `anchored_at` order
    differently, which `write_chain_anchor` refuses to produce by design.
    """

    async def scenario() -> None:
        async with _engine(owner_url) as engine, engine.begin() as conn:
            await conn.execute(
                text(
                    f"INSERT INTO {ANCHOR_TABLE} "
                    "(id, tenant_id, audit_seq, audit_hash, anchored_at) "
                    "VALUES (:id, :t, :seq, :hash, :at)"
                ),
                {
                    "id": uuid.uuid4(),
                    "t": tenant_id,
                    "seq": audit_seq,
                    "hash": audit_hash,
                    "at": anchored_at,
                },
            )

    asyncio.run(scenario())


def _read_latest(owner_url: str, tenant_id: uuid.UUID) -> ChainAnchor | None:
    """`latest_chain_anchor` on a session bound to `tenant_id`."""

    async def scenario() -> ChainAnchor | None:
        async with _engine(owner_url) as engine:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant_id) as session:
                return await latest_chain_anchor(session, tenant_id)

    return asyncio.run(scenario())


def _tamper(owner_url: str, tenant_id: uuid.UUID, ordinal: int) -> None:
    """Rewrite the `ordinal`th event OF THIS TENANT: owner, trigger disabled.

    The row is located by `OFFSET`, the way `_truncate_tail` locates its cut.
    `seq` is a GLOBAL identity (`models/audit.py`), so it is not a per-tenant
    counter: passing a literal `2` rewrote whichever tenant happened to own
    seq 2 and usually matched no row at all, leaving the chain intact and the
    premise assertion below the only thing that said so.
    """

    async def scenario() -> None:
        async with _engine(owner_url) as engine, engine.begin() as conn:
            await conn.execute(text("ALTER TABLE audit_events DISABLE TRIGGER USER"))
            updated = await conn.execute(
                text(
                    "UPDATE audit_events SET payload = CAST(:payload AS jsonb) "
                    "WHERE tenant_id = :t AND seq = ("
                    "SELECT seq FROM audit_events WHERE tenant_id = :t "
                    "ORDER BY seq OFFSET :offset LIMIT 1)"
                ),
                {
                    "payload": json.dumps({"fake": "tampered"}),
                    "t": tenant_id,
                    "offset": ordinal - 1,
                },
            )
            assert updated.rowcount == 1, f"no event {ordinal} to tamper with"
            await conn.execute(text("ALTER TABLE audit_events ENABLE TRIGGER USER"))

    asyncio.run(scenario())


def _truncate_tail(owner_url: str, tenant_id: uuid.UUID, keep: int) -> None:
    """Delete every event past the `keep`th, as the owner with triggers off.

    The insider this plan is about: the surviving rows still link perfectly,
    which is what `test_audit_chain.py::test_truncating_the_tail_is_not_
    detected_by_the_walk_alone` records about the walk on its own.
    """

    async def scenario() -> None:
        async with _engine(owner_url) as engine, engine.begin() as conn:
            await conn.execute(text("ALTER TABLE audit_events DISABLE TRIGGER USER"))
            await conn.execute(
                text(
                    "DELETE FROM audit_events WHERE tenant_id = :t AND seq > ("
                    "SELECT seq FROM audit_events WHERE tenant_id = :t "
                    "ORDER BY seq OFFSET :offset LIMIT 1)"
                ),
                {"t": tenant_id, "offset": keep - 1},
            )
            await conn.execute(text("ALTER TABLE audit_events ENABLE TRIGGER USER"))

    asyncio.run(scenario())


def _verify(owner_url: str, tenant_id: uuid.UUID) -> VerifyResult:
    """`verify_tenant_chain` on a session bound to `tenant_id`."""

    async def scenario() -> VerifyResult:
        async with _engine(owner_url) as engine:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant_id) as session:
                return await verify_tenant_chain(session, tenant_id)

    return asyncio.run(scenario())


def _refresh_gauge(owner_url: str, tenant_id: uuid.UUID) -> None:
    """`refresh_chain_gauge` on a session bound to `tenant_id`."""

    async def scenario() -> None:
        async with _engine(owner_url) as engine:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant_id) as session:
                await refresh_chain_gauge(session)

    asyncio.run(scenario())


async def _refused_sqlstate(
    restricted_url: str, statement: str, parameters: Mapping[str, object]
) -> str:
    """Run one statement as the role; return the SQLSTATE it was refused with.

    `connect` rather than `begin`: the statement leaves the transaction
    aborted, and closing the connection rolls it back rather than sending a
    COMMIT over the failure.
    """
    async with _engine(restricted_url) as engine, engine.connect() as conn:
        with pytest.raises(DBAPIError) as caught:
            await conn.execute(text(statement), dict(parameters))
    return _sqlstate(caught.value)


async def _insert_anchor_as_role(restricted_url: str, tenant_id: uuid.UUID, audit_seq: int) -> None:
    """One INSERT into the anchor table over a connection authenticated as the role."""
    async with _engine(restricted_url) as engine, engine.begin() as conn:
        await conn.execute(
            text(
                f"INSERT INTO {ANCHOR_TABLE} "
                "(id, tenant_id, audit_seq, audit_hash, anchored_at) "
                "VALUES (:id, :t, :seq, :hash, now())"
            ),
            {"id": uuid.uuid4(), "t": tenant_id, "seq": audit_seq, "hash": SAMPLE_HASH},
        )


async def _role_table_privileges(owner_url: str) -> dict[str, frozenset[str]]:
    """Every privilege `helpdesk_app` holds on a public table, from the catalogue."""
    granted: dict[str, set[str]] = {}
    async with _engine(owner_url) as engine, engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT table_name, privilege_type FROM "
                    "information_schema.role_table_grants "
                    "WHERE grantee = :role AND table_schema = 'public'"
                ),
                {"role": ROLE_NAME},
            )
        ).all()
    for table_name, privilege_type in rows:
        granted.setdefault(str(table_name), set()).add(str(privilege_type))
    return {name: frozenset(privileges) for name, privileges in granted.items()}


def _alembic(*arguments: str, database_url: str) -> None:
    result = subprocess.run(
        ["uv", "run", "alembic", *arguments],
        cwd=REPO_ROOT / "backend",  # alembic.ini lives in backend/
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"alembic {' '.join(arguments)} failed:\n{result.stderr}"


@pytest.fixture
def sweep_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[CollectorRegistry]:
    """A registry of this case's own, with the sweep gauges installed on it.

    `record_audit_chain_ok` reads `metrics.SWEEP_GAUGES` at call time, so
    replacing that name is what makes an absolute assertion possible and what
    keeps this file off the scheduler's process-wide instance. Same shape as
    `test_audit_health.py::sweep_registry`.
    """
    registry = CollectorRegistry()
    monkeypatch.setattr(metrics, "SWEEP_GAUGES", metrics.install_sweep_gauges(registry))
    yield registry


# --- the table and the allowlist ---------------------------


def test_the_anchor_table_is_in_the_tenant_scoped_allowlist() -> None:
    """a project rule: the hand-kept allowlist this plan extends.

    Catches a table added to `Base.metadata` and to no allowlist, which is
    what makes `test_models.py` walk it for a `tenant_id`, a NOT NULL, a FK to
    `tenants` and a composite index led by `tenant_id` — none of which any
    case in this file asserts, and all of which decide whether one tenant's
    anchors can be read while another's are bound.
    """
    assert ANCHOR_TABLE in TENANT_SCOPED_TABLES


def test_the_anchor_table_carries_the_planned_columns() -> None:
    """Plan §1's column criterion, asserted on the mapped table.

    Catches `audit_hash` declared as unbounded text, which would let a
    64-character gate be widened later in one place and not the other, and
    `anchored_at` declared WITHOUT a timezone: a naive timestamp makes two
    anchors written either side of a clock change unorderable, and the
    ordering is the only thing that says which head is current.

    `audit_seq` is BigInteger because it mirrors `audit_events.seq`, which is
    a BIGINT identity; a 32-bit column here would start refusing anchors at
    the two-billionth event while the log itself kept going.
    """
    table = Base.metadata.tables[ANCHOR_TABLE]

    assert isinstance(table.c.audit_seq.type, BigInteger)
    assert isinstance(table.c.audit_hash.type, String)
    assert table.c.audit_hash.type.length == 64
    assert isinstance(table.c.anchored_at.type, DateTime)
    assert table.c.anchored_at.type.timezone is True
    for name in ("id", "created_at", "updated_at", "tenant_id"):
        assert name in table.c, f"{ANCHOR_TABLE} must keep TenantScoped's {name}"


# --- the input model (plan §1's input-validation criterion) -------------------


def test_the_anchor_validation_constants_are_exactly_the_planned_values() -> None:
    """a project rule on the two values that decide what may be stored.

    A pattern widened to `[0-9a-fA-F]{64}` or a floor dropped to 0 would let
    a value through that no case below spells out, and the boundary cases
    would then be asserting the code against itself.
    """
    assert ANCHOR_HASH_PATTERN == r"[0-9a-f]{64}"
    assert MIN_ANCHOR_SEQ == 1
    assert InvalidChainAnchorError.code is ErrorCode.VALIDATION
    assert AnchorRegressionError.code is ErrorCode.VALIDATION


def test_an_anchor_hash_of_exactly_sixty_four_lowercase_hex_characters_is_accepted() -> None:
    """The inclusive boundary from the accepting side.

    Catches a gate written one character tight (`{65}`, or a `>` where a `>=`
    belongs), which would refuse every real digest `event_hash` produces and
    leave the anchor job raising on every tick with the log looking verified.
    """
    anchor = ChainAnchor(audit_seq=1, audit_hash=SAMPLE_HASH)

    assert anchor.audit_hash == SAMPLE_HASH
    assert len(SAMPLE_HASH) == 64
    assert anchor.audit_seq == 1


@pytest.mark.parametrize(
    ("label", "candidate"),
    [
        ("63 characters", "a" * 63),
        ("65 characters", "a" * 65),
        ("an uppercase hex digit", "A" + "a" * 63),
        ("a non-hex letter", "g" + "a" * 63),
        ("a trailing newline", "a" * 64 + "\n"),
        ("a trailing CRLF", "a" * 64 + "\r\n"),
        ("a trailing space", "a" * 64 + " "),
        ("a leading space", " " + "a" * 63),
        ("empty", ""),
    ],
)
def test_an_anchor_hash_outside_the_pattern_is_refused_with_the_typed_error(
    label: str, candidate: str
) -> None:
    """The rejection table of a project rule, and the boundary from the other
    side.

    The three trailing rows are the ones that only `re.fullmatch` refuses: a
    Pydantic `Field(pattern=...)` and `re.match` both anchor at the START
    only, so a 64-character digest with a newline glued on is ACCEPTED by
    either — and a hash carrying a newline is a value no operator reading a
    shipped log line can tell from the real one, which is the whole of
    decision B's witness.

    The uppercase row is the one a case-insensitive comparison would let
    through; `event_hash` emits lowercase, so an uppercase anchor could never
    equal a stored hash and every verify against it would read as a rewritten
    log.
    """
    with pytest.raises(InvalidChainAnchorError):
        ChainAnchor(audit_seq=1, audit_hash=candidate)


@pytest.mark.parametrize("audit_seq", [1, 2, 9_000_000_000])
def test_an_anchor_seq_of_one_or_more_is_accepted(audit_seq: int) -> None:
    """The inclusive boundary of `audit_seq` from the accepting side.

    1 is the first `seq` Postgres's identity hands out, so a floor written as
    `> 1` would refuse the anchor of every tenant whose log holds exactly one
    event — the tenant most likely to have been truncated to that one event.
    """
    assert ChainAnchor(audit_seq=audit_seq, audit_hash=SAMPLE_HASH).audit_seq == audit_seq


@pytest.mark.parametrize("audit_seq", [0, -1])
def test_an_anchor_seq_below_one_is_refused_with_the_typed_error(audit_seq: int) -> None:
    """The refusing side of the same boundary.

    Catches an anchor built for a tenant with no events at all, where a
    derived "last seq" of 0 is the natural wrong answer: stored once, it names
    a row that can never exist, so every later verify of that tenant reads as
    truncated for ever and the gauge never recovers.
    """
    with pytest.raises(InvalidChainAnchorError):
        ChainAnchor(audit_seq=audit_seq, audit_hash=SAMPLE_HASH)


# --- writing an anchor --------------------------------------------------------


def test_anchoring_a_verified_chain_stores_the_tenants_last_event(migrated_db_url: str) -> None:
    """Plan §1's happy path, and the only case that says WHICH event is the head.

    Catches an implementation that anchors the FIRST event, or genesis: both
    store a row, both make `latest_chain_anchor` answer, and both leave every
    event after the anchored one deletable with no verify ever noticing — the
    exact hole this plan closes.

    The expected pair is read out of `audit_events` rather than written as a
    literal, because the digest depends on a timestamp the
    writer chose.
    """
    tenant_id = _tenant_with_events(migrated_db_url, 3)
    last_seq, last_hash = _last_event(migrated_db_url, tenant_id)

    written = _anchor_chain(migrated_db_url, tenant_id)

    assert written == ChainAnchor(audit_seq=last_seq, audit_hash=last_hash)
    assert _stored_anchors(migrated_db_url, tenant_id) == [(last_seq, last_hash)]
    assert _read_latest(migrated_db_url, tenant_id) == ChainAnchor(
        audit_seq=last_seq, audit_hash=last_hash
    )


def test_a_tampered_chain_writes_no_anchor(migrated_db_url: str) -> None:
    """Plan §5's first risk: a doctored log laundering itself into the record.

    Catches the ordering written the other way round — anchor first, verify
    after. That implementation passes the happy-path case above, passes every
    reader case, and quietly stores a head for a chain that does not verify;
    the next sweep then has a stored anchor agreeing with the rewritten log,
    and the tampering becomes permanently invisible.

    The premise is asserted: the chain really is broken, so a green here
    cannot mean the tamper helper failed.
    """
    tenant_id = _tenant_with_events(migrated_db_url, 4)
    seq_before, _ = _last_event(migrated_db_url, tenant_id)
    _tamper(migrated_db_url, tenant_id, 2)
    assert _verify(migrated_db_url, tenant_id).ok is False, "premise: the chain must be broken"

    written = _anchor_chain(migrated_db_url, tenant_id)

    assert written is None
    assert _stored_anchors(migrated_db_url, tenant_id) == []
    assert seq_before >= 1


def test_a_truncated_chain_writes_no_anchor(migrated_db_url: str) -> None:
    """The same refusal for the failure this plan exists to detect, which the
    tampered case cannot reach: after a truncation the surviving rows all
    recompute, so only the stored anchor says anything is wrong.

    Catches an implementation that verifies WITHOUT the stored anchor before
    writing — `verify_chain(events)` with no second argument. It answers `ok`
    on a truncated log, so a second anchor lands at the new, lower head, and
    the tenant reads as intact from then on. The stored set is asserted to be
    exactly the original head, which is what a laundering write changes.
    """
    tenant_id = _tenant_with_events(migrated_db_url, 5)
    head_seq, head_hash = _last_event(migrated_db_url, tenant_id)
    assert _anchor_chain(migrated_db_url, tenant_id) is not None, "premise: the first anchor lands"
    _truncate_tail(migrated_db_url, tenant_id, keep=3)

    written = _anchor_chain(migrated_db_url, tenant_id)

    assert written is None
    assert _stored_anchors(migrated_db_url, tenant_id) == [(head_seq, head_hash)]


def test_a_tenant_with_no_events_writes_no_anchor(migrated_db_url: str) -> None:
    """Plan §1: a tenant with no events writes no anchor at all.

    Catches an implementation that anchors `(0, GENESIS_HASH)` for an empty
    tenant — the natural thing to fall out of "the last event's seq and hash"
    when there is no last event. That row names a seq no event can ever carry,
    so the tenant's very first event would make the chain read as truncated
    for ever, and every new tenant in the system would arrive broken.
    """
    tenant_id = _tenant_with_events(migrated_db_url, 0)

    written = _anchor_chain(migrated_db_url, tenant_id)

    assert written is None
    assert _stored_anchors(migrated_db_url, tenant_id) == []
    assert _verify(migrated_db_url, tenant_id).ok is True
    assert _verify(migrated_db_url, tenant_id).anchored_seq is None


def test_a_second_anchor_at_the_same_seq_is_refused_by_the_unique_constraint(
    migrated_db_url: str,
) -> None:
    """Plan §1's unhappy path, and §5's concurrency backstop.

    Catches a table with no unique constraint on (`tenant_id`, `audit_seq`):
    two runners whose Redis lock both expired would each store a head at the
    same seq, and two rows at one seq make "the current head" a question the
    table cannot answer — a reader picking either could verify against a hash
    an attacker chose.

    An EQUAL seq is deliberately not a regression (§1 says "lower than"), so
    this case also pins the no-regress comparison as strict: an implementation
    using `<=` answers `AnchorRegressionError` here and fails, which is the
    right signal, because it would then refuse the honest re-anchor of a
    tenant that has emitted no new event since the last sweep.
    """
    tenant_id = _tenant_with_events(migrated_db_url, 2)
    anchor = ChainAnchor(audit_seq=7, audit_hash=SAMPLE_HASH)
    _write_anchor(migrated_db_url, tenant_id, anchor)

    with pytest.raises(IntegrityError):
        _write_anchor(migrated_db_url, tenant_id, anchor)

    assert _stored_anchors(migrated_db_url, tenant_id) == [(7, SAMPLE_HASH)]


def test_an_anchor_behind_the_stored_head_is_refused_and_leaves_it_in_place(
    migrated_db_url: str,
) -> None:
    """Plan §1: an anchor never regresses.

    Catches a writer with no comparison at all. The INSERT of a LOWER seq
    breaks no constraint — different seq, same tenant — so it lands quietly,
    and from then on the table holds two contradictory heads. Any reader that
    took the newest row, rather than the highest seq, would then verify
    against a head below the truncation and report the log as intact.

    The stored set is asserted afterwards as well as the exception: an
    implementation that raises AFTER the INSERT, inside the same committed
    transaction, would pass a `pytest.raises` on its own.
    """
    tenant_id = _tenant_with_events(migrated_db_url, 2)
    _write_anchor(migrated_db_url, tenant_id, ChainAnchor(audit_seq=9, audit_hash=SAMPLE_HASH))

    with pytest.raises(AnchorRegressionError):
        _write_anchor(migrated_db_url, tenant_id, ChainAnchor(audit_seq=4, audit_hash="b" * 64))

    assert _stored_anchors(migrated_db_url, tenant_id) == [(9, SAMPLE_HASH)]
    assert _read_latest(migrated_db_url, tenant_id) == ChainAnchor(
        audit_seq=9, audit_hash=SAMPLE_HASH
    )


def test_latest_chain_anchor_reads_the_highest_seq_not_the_newest_row(
    migrated_db_url: str,
) -> None:
    """Plan §6: proved by inserting the two rows out of order.

    Catches a reader ordering by `anchored_at` (or by `created_at`, or by
    nothing at all with a `LIMIT 1`). Every other case in this file has one
    anchor per tenant, or two whose seq and write order agree, so this is the
    only arrangement where the two orderings disagree — and the wrong one
    verifies a truncated log against a head BELOW the truncation, which reads
    as perfectly intact.

    The rows are planted as the owner: `write_chain_anchor` refuses to create
    this arrangement, which is exactly what the case above pins.
    """
    tenant_id = _tenant_with_events(migrated_db_url, 1)
    _plant_anchor_row(migrated_db_url, tenant_id, 9, SAMPLE_HASH, datetime(2026, 1, 1, tzinfo=UTC))
    _plant_anchor_row(migrated_db_url, tenant_id, 4, "b" * 64, datetime(2026, 6, 1, tzinfo=UTC))

    assert _read_latest(migrated_db_url, tenant_id) == ChainAnchor(
        audit_seq=9, audit_hash=SAMPLE_HASH
    )


def test_an_anchor_is_never_read_for_another_tenant(migrated_db_url: str) -> None:
    """a project rule's neighbour: an anchor belongs to exactly one tenant.

    Catches a query written without the `tenant_id` filter — one `SELECT ...
    ORDER BY audit_seq DESC LIMIT 1` over the whole table. Every single-tenant
    case in this file passes with that bug; here tenant B has no anchor of its
    own, so an unfiltered read hands it A's head, and B's honest chain is
    reported as rewritten (its row at A's seq carries a different hash) from
    the first sweep onwards.
    """
    tenant_a = _tenant_with_events(migrated_db_url, 3)
    tenant_b = _tenant_with_events(migrated_db_url, 3)
    assert _anchor_chain(migrated_db_url, tenant_a) is not None, "premise: A is anchored"

    assert _read_latest(migrated_db_url, tenant_b) is None
    verdict = _verify(migrated_db_url, tenant_b)
    assert verdict.ok is True
    assert verdict.anchored_seq is None


# --- the external witness (decision B) ----------------------------------------


def test_the_anchor_write_logs_one_line_naming_the_tenant_seq_and_hash(
    migrated_db_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Decision B: the only half of the record a superuser cannot reach.

    Catches an implementation that writes the row and ships nothing — the
    attacker who can drop audit rows can drop anchor rows too, and what
    survives them is a line already shipped off the host. Catches a line
    emitted per retry or per tenant visited rather than per WRITE, by counting
    exactly one.

    The three fields are read off the record's `__dict__`, before
    `JsonFormatter`
    renders it: the formatter redacts a forbidden key in place rather than
    dropping it, so a producer that attached identity here would still emit a
    line with that key on it. the design's telemetry rule is checked against the
    repo's own `FORBIDDEN_KEYS` rather than a list retyped here, so a sixth
    spelling added there is covered on the day it is added.
    """
    tenant_id = _tenant_with_events(migrated_db_url, 2)
    last_seq, last_hash = _last_event(migrated_db_url, tenant_id)

    with caplog.at_level(logging.INFO, logger=ANCHOR_LOGGER):
        _anchor_chain(migrated_db_url, tenant_id)

    written = [record for record in caplog.records if record.getMessage() == ANCHOR_LOG_MESSAGE]
    assert len(written) == 1, [record.getMessage() for record in caplog.records]
    payload = written[0].__dict__
    assert str(payload["tenant_id"]) == str(tenant_id)
    assert payload["audit_seq"] == last_seq
    assert payload["audit_hash"] == last_hash
    for forbidden in FORBIDDEN_KEYS:
        assert forbidden not in payload, f"{forbidden} must never reach a log line"


# --- the restricted role on the anchor table (plan §1) ------------------------


def test_restricted_role_insert_into_the_anchor_table_succeeds(
    migrated_db_url: str, restricted_db_url: str
) -> None:
    """Catches a migration that creates the table and grants the role nothing:
    the scheduler's anchor job runs as `helpdesk_app`, so it would meet 42501
    on every tick and no head would ever be recorded — with the chain gauge
    still reading 1, because an unanchored chain verifies."""

    tenant_id = _tenant_with_events(migrated_db_url, 1)

    async def scenario() -> None:
        await _insert_anchor_as_role(restricted_db_url, tenant_id, 1)
        async with _engine(restricted_db_url) as engine, engine.connect() as conn:
            row = (
                await conn.execute(
                    text(f"SELECT audit_seq, audit_hash FROM {ANCHOR_TABLE} WHERE tenant_id = :t"),
                    {"t": tenant_id},
                )
            ).one()
        assert int(row.audit_seq) == 1
        assert str(row.audit_hash) == SAMPLE_HASH

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("label", "statement"),
    [
        ("UPDATE", f"UPDATE {ANCHOR_TABLE} SET audit_seq = 2 WHERE tenant_id = :t"),
    ],
)
def test_a_row_matching_write_on_the_anchor_table_is_refused_by_privilege(
    migrated_db_url: str, restricted_db_url: str, label: str, statement: str
) -> None:
    """Plan §1: the refusal must be the PRIVILEGE check, not the trigger.

    The row is planted first and the WHERE clause really matches it, so this
    is the neighbouring-guard trap written out: with the grant still held, the
    append-only trigger stops the statement anyway and a case asserting only
    "this raises" would be green either way. Asserting `42501` exactly makes
    the case flip on the grant alone — which is the guarantee that survives a
    role that can disable a trigger.

    An UPDATE that lands is how an attacker moves the head DOWN to the
    truncation they just made.

    An earlier change §8 took the DELETE row out of this table. Decision A of that plan
    grants `helpdesk_app` DELETE so the retention job can run, so a DELETE is
    no longer refused by the grants at all: the head at the time of the
    statement, and every anchor inside the window, are refused one SQLSTATE
    later, by the trigger, for every role including the owner. That guarantee is pinned by
    `test_audit_anchor_retention.py::test_a_delete_of_the_head_as_the_restricted_role_is_refused_by_the_trigger`,
    `::test_a_delete_of_a_recent_non_head_anchor_as_the_restricted_role_is_refused`
    and `::test_a_delete_of_the_head_as_the_table_owner_is_refused_by_the_trigger`.
    """

    tenant_id = _tenant_with_events(migrated_db_url, 1)

    async def scenario() -> None:
        await _insert_anchor_as_role(restricted_db_url, tenant_id, 1)
        sqlstate = await _refused_sqlstate(restricted_db_url, statement, {"t": tenant_id})
        assert sqlstate == INSUFFICIENT_PRIVILEGE, (
            f"expected {INSUFFICIENT_PRIVILEGE} for {label} as {ROLE_NAME}, got {sqlstate}"
            + (
                f" ({TRIGGER_RAISED} means the append-only trigger caught what the "
                "grants were supposed to stop)"
                if sqlstate == TRIGGER_RAISED
                else ""
            )
        )

    asyncio.run(scenario())


def test_truncate_on_the_anchor_table_is_refused_by_privilege(
    migrated_db_url: str, restricted_db_url: str
) -> None:
    """TRUNCATE empties every tenant's anchors at once, and a log with no
    anchor verifies clean, so the whole detection this plan adds is one
    statement away from being switched off.

    Catches a migration that hands the role TRUNCATE, or grants ALL: the
    statement must be stopped by the privilege check before any
    statement-level trigger is reached."""

    tenant_id = _tenant_with_events(migrated_db_url, 1)

    async def scenario() -> None:
        await _insert_anchor_as_role(restricted_db_url, tenant_id, 1)
        sqlstate = await _refused_sqlstate(restricted_db_url, f"TRUNCATE {ANCHOR_TABLE}", {})
        assert sqlstate == INSUFFICIENT_PRIVILEGE, (
            f"expected {INSUFFICIENT_PRIVILEGE} for TRUNCATE as {ROLE_NAME}, got {sqlstate}"
        )

    asyncio.run(scenario())


# --- end to end ---------------------------------------------------------------


def test_verify_reports_truncation_against_the_stored_anchor(migrated_db_url: str) -> None:
    """Plan §1's headline, through the seam every caller already uses.

    Catches `verify.py` never loading the anchor: `chain.py` can answer the
    four-branch verdict perfectly and every existing caller — the route, the
    gauge, the MCP read path — still reports `ok` on a truncated log, because
    nothing hands the head down. `test_audit_chain.py` cannot see that, since
    it passes the anchor itself.
    """
    tenant_id = _tenant_with_events(migrated_db_url, 5)
    head_seq, _head_hash = _last_event(migrated_db_url, tenant_id)
    assert _anchor_chain(migrated_db_url, tenant_id) is not None, "premise: the anchor lands"

    _truncate_tail(migrated_db_url, tenant_id, keep=3)
    verdict = _verify(migrated_db_url, tenant_id)

    assert verdict.ok is False
    assert verdict.truncated_at == head_seq
    assert verdict.broken_at is None
    assert verdict.anchored_seq == head_seq


def test_a_truncated_tenant_drives_the_chain_gauge_to_zero(
    migrated_db_url: str, sweep_registry: CollectorRegistry
) -> None:
    """Plan §1: `helpdesk_audit_chain_ok` reads 0 for a truncated tenant
    THROUGH the existing AND in `audit/health.py`, with no change to it.

    Catches a verdict shape that reports truncation somewhere other than `ok`
    — a fourth field the gauge does not read. `refresh_chain_gauge` records
    `result.ok` and nothing else, so an implementation that set only
    `truncated_at` would leave the alert reading 1 on a log missing its last
    hour, which is the one number an operator is actually watching.

    The gauge is driven to 1 first, so leaving the previous value fails.
    """
    reset_chain_verdicts()
    tenant_id = _tenant_with_events(migrated_db_url, 5)
    assert _anchor_chain(migrated_db_url, tenant_id) is not None, "premise: the anchor lands"
    _refresh_gauge(migrated_db_url, tenant_id)
    assert sweep_registry.get_sample_value(AUDIT_CHAIN_OK) == 1.0, "premise: a sound chain reads 1"

    _truncate_tail(migrated_db_url, tenant_id, keep=2)
    _refresh_gauge(migrated_db_url, tenant_id)

    assert sweep_registry.get_sample_value(AUDIT_CHAIN_OK) == 0.0
    reset_chain_verdicts()


# --- the migration ------------------------------------------------------------


def test_the_anchor_migration_round_trips_and_its_downgrade_takes_the_grants(
    fresh_db_url: str,
) -> None:
    """Catches a downgrade that leaves the table (the second upgrade's CREATE
    TABLE then fails, which is the whole kill of the round trip) and one that
    leaves the grants behind on a table it dropped and recreated.

    The downgrade names an earlier change's revision rather than counting `-1` steps
    back: this revision is no longer the head, and
    a relative step would leave the anchor table standing and say nothing about
    this migration at all. If the target is wrong, the grant check below reads
    a database where `audit_events` lost its grants instead, and says so.
    """
    _alembic("upgrade", "head", database_url=fresh_db_url)

    async def after_upgrade() -> None:
        granted = await _role_table_privileges(fresh_db_url)
        # An earlier change §8: this reads the live catalogue at HEAD, not an earlier change's
        # revision alone, so it sees that plan's `DELETE` too. Which revision
        # granted which privilege is the union map's business
        # (`test_audit_db_role.py`); what this case still pins is that the
        # downgrade to `c7f4a1d2e8b3` takes every one of them away.
        assert granted.get(ANCHOR_TABLE, frozenset()) == frozenset({"SELECT", "INSERT", "DELETE"})

    asyncio.run(after_upgrade())

    _alembic("downgrade", ANCHOR_DOWNGRADE_TARGET, database_url=fresh_db_url)

    async def after_downgrade() -> None:
        async with _engine(fresh_db_url) as engine, engine.connect() as conn:
            anchors = (
                await conn.execute(text(f"SELECT to_regclass('public.{ANCHOR_TABLE}')"))
            ).scalar_one()
            events = (
                await conn.execute(text("SELECT to_regclass('public.audit_events')"))
            ).scalar_one()
        assert events is not None, (
            "one step down must be THIS plan's revision only; audit_events is gone, "
            "so the new revision does not revise c7f4a1d2e8b3"
        )
        assert anchors is None, "the downgrade must drop the anchor table"
        granted = await _role_table_privileges(fresh_db_url)
        assert ANCHOR_TABLE not in granted

    asyncio.run(after_downgrade())

    _alembic("upgrade", "head", database_url=fresh_db_url)
    asyncio.run(after_upgrade())
