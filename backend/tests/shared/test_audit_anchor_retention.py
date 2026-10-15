"""An earlier change section 6: how an old anchor stops accumulating, and what may never go.

Written before the implementation exists. Expected surface (the implementer
must provide exactly this; the names below are this file's, and a different
spelling in the implementation is a rename here, not a redesign):

``helpdesk.audit.retention`` (a new module beside `anchor.py`, so every
statement against the audit tables stays in one package)
    ``ANCHOR_RETENTION_DAYS = 90`` — plan decision B, the window.
    ``def anchor_retention_cutoff(now: datetime) -> datetime`` — exactly
    ``now - timedelta(days=ANCHOR_RETENTION_DAYS)``, the instant a row must be
    strictly BELOW to be purgeable.
    ``async def purge_chain_anchors(session, now) -> int`` — deletes the bound
    tenant's anchors older than that cutoff, NEVER the tenant's highest
    ``audit_seq``, and returns how many rows went. The tenant comes off the
    session via ``bound_tenant_id``, never off an argument.
    One JSON log line ``audit.chain.anchors_purged`` per purge that deleted
    something, carrying ``tenant_id`` and ``deleted`` and no user identity.

one new Alembic revision, file name ending ``_audit_chain_anchor_retention.py``
    ``CREATE OR REPLACE`` of ``audit_chain_anchors_append_only`` branching on
    ``TG_OP``: a DELETE returns ``OLD`` only when the row is past the window
    AND a higher ``audit_seq`` exists for that tenant, and raises otherwise.
    Its own ``TABLE_GRANTS = {"audit_chain_anchors": ("DELETE",)}``, and a
    downgrade restoring an earlier change's function body verbatim and revoking.
    The window is written in that function as the SQL literal
    ``INTERVAL '90 days'``, which
    ``::test_the_migrations_sql_interval_equals_the_library_constant`` parses
    out of the file: an applied migration is frozen, so the test is the only
    link between the SQL number and the Python one (decision D).

What this file deliberately does NOT re-test:

- the role's grant SET on `audit_chain_anchors`. `test_audit_db_role.py`
  already asserts it per table off the UNION of every revision's own
  `TABLE_GRANTS`, and plan section 8 widens that union to SELECT, INSERT and
  DELETE there. Asserting it here too would make one of the two green for the
  other's reason.
- `check_job_name`, the lock key and the registry, which belong to the
  scheduler suite (`test_scheduler_anchor_retention_job.py`).

a project rule: the session-scoped `migrated_db_url` from `conftest.py`, with
an earlier change's `restricted_db_url` over that SAME database for the privilege and
trigger cases. Per-test isolation is a fresh tenant per case, never a fresh
database; only the Alembic round trip takes `fresh_db_url`, because it
downgrades.

a project rule: `test_audit_anchor_retention.py` is a basename no other test
file in the tree carries.

No pytest-asyncio: each case is a sync function driving one `asyncio.run`.
"""

import asyncio
import logging
import os
import re
import subprocess
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from helpdesk.shared.audit.anchor import anchor_tenant_chain
from helpdesk.shared.audit.retention import (
    ANCHOR_RETENTION_DAYS,
    anchor_retention_cutoff,
    purge_chain_anchors,
)
from helpdesk.shared.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.shared.core.observability.logging import FORBIDDEN_KEYS
from helpdesk.shared.services.audit import AuditEventInput
from helpdesk.shared.services.audit.chain import VerifyResult
from helpdesk.shared.services.audit.verify import verify_tenant_chain
from helpdesk.shared.services.audit.writer import append_event

REPO_ROOT = Path(__file__).resolve().parents[3]

ANCHOR_TABLE = "audit_chain_anchors"
"""The table name, written out: it is half of every grant and every trigger."""

PURGE_LOG_MESSAGE = "audit.chain.anchors_purged"
"""Decision E's whole record of a purge. The message an operator greps the
shipped logs for beside `audit.chain.anchored`, so it is pinned as a literal."""

RETENTION_LOGGER = "helpdesk.shared.audit.retention"
"""Where that line is emitted from: the module that owns the DELETE."""

ROLE_NAME = "helpdesk_app"
"""An earlier change decision C, repeated here for the privilege cases below."""

INSUFFICIENT_PRIVILEGE = "42501"
TRIGGER_RAISED = "P0001"
"""42501 is `insufficient_privilege`, P0001 what the append-only trigger
raises. Which one arrives is the whole point of the cases below: this plan
GRANTS the role DELETE, so from now on the head and the recent rows are
refused one SQLSTATE later, by the trigger rather than by the grants."""

SAMPLE_HASH = "0123456789abcdef" * 4
"""A 64-character lowercase hex string using every hex digit. Obviously fake,
and never the digest of anything."""

RETENTION_REVISION_GLOB = "*_audit_chain_anchor_retention.py"
"""How the revision is found: its hash cannot be written here before it
exists, and `backend/migrations/versions` is not an importable package."""

RETENTION_PARENT_REVISION = "f1a7c0d4e2b9"
"""The revision immediately below this plan's own: its `down_revision`.

Downgrading to THIS undoes exactly the retention revision. An earlier draft
named an earlier change's `d8b2f6c41a97`, which sits two steps down and so also undid
`f1a7c0d4e2b9` — harmless today, and the kind of over-wide step that fails
later for a reason that has nothing to do with retention.

Written out rather than stepped to with `-1`, because a relative step means a
different revision on the day a new head lands (undistilled entry, an earlier change)."""

NOW = datetime.now(UTC)
"""The instant every case hands to the purge, read once per run.

Not a fixed literal: the trigger enforces the same window against the
DATABASE's clock, so a row planted relative to a hard-coded date would drift
in and out of the trigger's window as the calendar moves while the case still
claimed to be about the library's cutoff. Every planted `anchored_at` below is
an offset from this one instant, which is what makes the boundary rows exact.
"""

PAST_WINDOW = NOW - timedelta(days=200)
"""Comfortably older than the window, for the cases that are not about the
boundary itself."""


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


async def _plant_tenant(owner_url: str, slug: str) -> uuid.UUID:
    """One obviously fake tenant, planted as the OWNER so the FK target exists."""
    tenant_id = uuid.uuid4()
    async with _engine(owner_url) as engine, engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO tenants (id, slug, name, residency, settings) "
                "VALUES (:id, :slug, :name, 'eu', CAST(:settings AS jsonb))"
            ),
            {"id": tenant_id, "slug": slug, "name": f"Fake Tenant {slug}", "settings": "{}"},
        )
    return tenant_id


def _tenant(owner_url: str) -> uuid.UUID:
    """A fresh tenant of this case's own: the per-case namespace."""
    return asyncio.run(_plant_tenant(owner_url, f"retention-{uuid.uuid4().hex[:10]}"))


def _tenant_with_events(owner_url: str, events: int) -> uuid.UUID:
    """A fresh tenant carrying `events` chained events, for the verify case."""

    async def scenario() -> uuid.UUID:
        tenant_id = await _plant_tenant(owner_url, f"retention-{uuid.uuid4().hex[:10]}")
        async with _engine(owner_url) as engine:
            factory = create_session_factory(engine)
            for index in range(1, events + 1):
                async with tenant_session(factory, tenant_id) as session:
                    await append_event(
                        session,
                        tenant_id,
                        AuditEventInput(
                            occurred_at=None,
                            actor_kind="system",
                            actor_ref=str(uuid.UUID(int=index)),
                            session_id=None,
                            trace_id=f"trace-fake-30cc-{index}",
                            event_type="ticket.created",
                            subject_type="ticket",
                            subject_id=str(uuid.UUID(int=3000 + index)),
                            payload={"fake": True, "index": index},
                        ),
                    )
        return tenant_id

    return asyncio.run(scenario())


async def _insert_anchor(
    owner_url: str, tenant_id: uuid.UUID, audit_seq: int, anchored_at: datetime
) -> uuid.UUID:
    """One anchor row planted as the OWNER, with the `anchored_at` the case chose.

    Planted rather than written through `write_chain_anchor`: that writer
    stamps `now()` by design, and every case here is about a row whose age the
    case fixes. No case waits on a clock.
    """
    anchor_id = uuid.uuid4()
    async with _engine(owner_url) as engine, engine.begin() as conn:
        await conn.execute(
            text(
                f"INSERT INTO {ANCHOR_TABLE} "
                "(id, tenant_id, audit_seq, audit_hash, anchored_at) "
                "VALUES (:id, :t, :seq, :hash, :at)"
            ),
            {
                "id": anchor_id,
                "t": tenant_id,
                "seq": audit_seq,
                "hash": SAMPLE_HASH,
                "at": anchored_at,
            },
        )
    return anchor_id


def _plant_anchor(
    owner_url: str, tenant_id: uuid.UUID, audit_seq: int, anchored_at: datetime
) -> uuid.UUID:
    return asyncio.run(_insert_anchor(owner_url, tenant_id, audit_seq, anchored_at))


def _purge(owner_url: str, tenant_id: uuid.UUID, now: datetime) -> int:
    """`purge_chain_anchors` on a session bound to `tenant_id`, committed.

    The tenant is bound and NOT passed: a project rule is the criterion, so the
    seam this file drives has no tenant argument to get wrong.
    """

    async def scenario() -> int:
        async with _engine(owner_url) as engine:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant_id) as session:
                deleted = await purge_chain_anchors(session, now)
                await session.commit()
        return deleted

    return asyncio.run(scenario())


def _stored_seqs(owner_url: str, tenant_id: uuid.UUID) -> list[int]:
    """Every `audit_seq` the tenant still carries, ascending, read as the owner."""

    async def scenario() -> list[int]:
        async with _engine(owner_url) as engine, engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        f"SELECT audit_seq FROM {ANCHOR_TABLE} "
                        "WHERE tenant_id = :t ORDER BY audit_seq"
                    ),
                    {"t": tenant_id},
                )
            ).all()
        return [int(row.audit_seq) for row in rows]

    return asyncio.run(scenario())


def _verify(owner_url: str, tenant_id: uuid.UUID) -> VerifyResult:
    """`verify_tenant_chain` on a session bound to `tenant_id`."""

    async def scenario() -> VerifyResult:
        async with _engine(owner_url) as engine:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant_id) as session:
                return await verify_tenant_chain(session, tenant_id)

    return asyncio.run(scenario())


def _anchor_chain(owner_url: str, tenant_id: uuid.UUID) -> None:
    """`anchor_tenant_chain` on a session bound to `tenant_id`, committed."""

    async def scenario() -> None:
        async with _engine(owner_url) as engine:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant_id) as session:
                await anchor_tenant_chain(session)
                await session.commit()

    asyncio.run(scenario())


async def _refused_sqlstate(url: str, statement: str, parameters: Mapping[str, object]) -> str:
    """Run one statement over `url`; return the SQLSTATE it was refused with.

    `connect` rather than `begin`: the statement leaves the transaction
    aborted, and closing the connection rolls it back rather than sending a
    COMMIT over the failure.
    """
    async with _engine(url) as engine, engine.connect() as conn:
        with pytest.raises(DBAPIError) as caught:
            await conn.execute(text(statement), dict(parameters))
    return _sqlstate(caught.value)


async def _delete_anchor(url: str, anchor_id: uuid.UUID) -> int:
    """DELETE one anchor by id over `url`, returning the row count."""
    async with _engine(url) as engine, engine.begin() as conn:
        result = await conn.execute(
            text(f"DELETE FROM {ANCHOR_TABLE} WHERE id = :id"), {"id": anchor_id}
        )
        return int(result.rowcount)


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


def _retention_revision_text() -> str:
    """The revision file's raw text, found by the slug this plan fixes."""
    versions = REPO_ROOT / "backend" / "migrations" / "versions"
    matches = sorted(versions.glob(RETENTION_REVISION_GLOB))
    assert len(matches) == 1, (
        f"expected exactly one alembic revision matching {RETENTION_REVISION_GLOB!r}, "
        f"found {[match.name for match in matches]}"
    )
    return matches[0].read_text(encoding="utf-8")


# --- the window ---------------------------------------------


def test_the_retention_constants_are_exactly_the_planned_values() -> None:
    """Decision B's window, and the cutoff it produces.

    Catches the number written in the wrong UNIT — `timedelta(days=90)` typed
    as `timedelta(hours=90)`, or a constant meant as days used as seconds. Both
    leave a function that purges, a log line that reads right and a suite that
    is green everywhere else, while the anchor history an auditor relies on is
    four days deep instead of three months. The cutoff is asserted as an exact
    instant rather than a band, because it is the value that decides whether a
    row survives.
    """
    assert ANCHOR_RETENTION_DAYS == 90
    assert anchor_retention_cutoff(NOW) == NOW - timedelta(days=90)
    assert anchor_retention_cutoff(NOW) == NOW - timedelta(days=ANCHOR_RETENTION_DAYS)


def test_the_migrations_sql_interval_equals_the_library_constant() -> None:
    """Decision D: the only link between the frozen SQL and the live Python.

    Catches the two cutoffs drifting — the constant lowered to 30 while the
    applied revision still says 90, after which the job asks for a delete the
    trigger refuses and every tick raises P0001 for every tenant. Nothing else
    in the tree can see it: the library's own cases never reach the trigger's
    number, and the trigger cases never reach the library's.
    """
    matched = re.search(r"INTERVAL\s+'(\d+)\s+days'", _retention_revision_text(), re.IGNORECASE)
    assert matched is not None, (
        f"the revision must state its window as the SQL literal "
        f"INTERVAL '{ANCHOR_RETENTION_DAYS} days', so this case can hold it to the library"
    )
    assert int(matched.group(1)) == ANCHOR_RETENTION_DAYS


# --- the boundary, both sides (plan section 1) --------------------------------


def test_an_anchor_exactly_at_the_cutoff_is_kept_and_nothing_is_deleted(
    migrated_db_url: str,
) -> None:
    """The inclusive boundary from the KEEPING side: the comparison is strict `<`.

    Catches `<=` against the cutoff, which is the natural thing to write and
    is one microsecond of difference from the case below — the only pair in
    this file that tells the two comparisons apart.

    The row under test is NOT the tenant's head: a higher seq is planted
    alongside it, so the head exclusion is not what saves it and deleting the
    strictness from the comparison is the only thing that flips this case.
    """
    tenant_id = _tenant(migrated_db_url)
    _plant_anchor(migrated_db_url, tenant_id, 1, NOW - timedelta(days=90))
    _plant_anchor(migrated_db_url, tenant_id, 5, NOW)

    deleted = _purge(migrated_db_url, tenant_id, NOW)

    assert deleted == 0
    assert _stored_seqs(migrated_db_url, tenant_id) == [1, 5]


def test_an_anchor_one_microsecond_past_the_cutoff_is_deleted(migrated_db_url: str) -> None:
    """The same boundary from the DELETING side.

    Catches a purge that never deletes anything at all — a statement built and
    never executed, or one whose `WHERE` can match no row — which every
    "nothing was deleted" case in this file would pass happily. It also
    catches a cutoff computed one whole day out, since the row is only just
    past it.
    """
    tenant_id = _tenant(migrated_db_url)
    _plant_anchor(
        migrated_db_url, tenant_id, 1, NOW - timedelta(days=90) - timedelta(microseconds=1)
    )
    _plant_anchor(migrated_db_url, tenant_id, 5, NOW)

    deleted = _purge(migrated_db_url, tenant_id, NOW)

    assert deleted == 1
    assert _stored_seqs(migrated_db_url, tenant_id) == [5]


# --- the head is never purged -------------------------------------------------


def test_a_tenants_only_anchor_is_kept_however_old(migrated_db_url: str) -> None:
    """Plan section 1: the quiet tenant's ancient head is the whole detection.

    Catches a purge written as age alone — `DELETE ... WHERE anchored_at <
    cutoff` — which passes both boundary cases above (they each keep a recent
    head by age, not by exclusion) and empties this tenant completely. A tenant
    with no anchor verifies as intact, so from then on its log could be
    truncated to nothing and every verify, every gauge and every route would
    report it healthy.
    """
    tenant_id = _tenant(migrated_db_url)
    _plant_anchor(migrated_db_url, tenant_id, 1, NOW - timedelta(days=730))

    deleted = _purge(migrated_db_url, tenant_id, NOW)

    assert deleted == 0
    assert _stored_seqs(migrated_db_url, tenant_id) == [1]


def test_the_highest_seq_survives_even_when_it_is_the_oldest_row(migrated_db_url: str) -> None:
    """Plan section 6's five-anchor case, arranged so `seq` and age disagree.

    Catches an exclusion written as "keep the NEWEST row" — `ORDER BY
    anchored_at DESC LIMIT 1` — which the case above cannot see, because a
    tenant with one anchor has the same row under either reading. Here the head
    (seq 5) is the OLDEST of the five, so that implementation keeps seq 4 and
    deletes the head: the count is still 4 and only the surviving seq says
    which row went.

    The head being past the window itself is the point: `whatever its own age`
    is the criterion, and every one of the five is purgeable by age alone.
    """
    tenant_id = _tenant(migrated_db_url)
    _plant_anchor(migrated_db_url, tenant_id, 5, NOW - timedelta(days=400))
    for seq in (1, 2, 3, 4):
        _plant_anchor(migrated_db_url, tenant_id, seq, PAST_WINDOW)

    deleted = _purge(migrated_db_url, tenant_id, NOW)

    assert deleted == 4
    assert _stored_seqs(migrated_db_url, tenant_id) == [5]


# --- the purge is invisible to every caller of the verify ---------------------


def test_a_purge_leaves_the_verify_verdict_and_its_anchored_seq_unchanged(
    migrated_db_url: str,
) -> None:
    """Plan section 1: the route, the gauge and the anchor job read the same answer.

    Catches a purge that takes the row `verify_tenant_chain` reads. The verdict
    is compared as a WHOLE object, `anchored_seq` included, so an
    implementation that left the tenant anchored at an older seq — which is
    what deleting by age alone, or keeping the newest row, produces here —
    changes `anchored_seq` and fails, while a case asserting only `ok` would
    stay green: a chain still verifies perfectly well against a head below the
    truncation.

    The premise is asserted on both sides: the purge really deleted rows, so a
    green cannot mean the purge was a no-op.
    """
    tenant_id = _tenant_with_events(migrated_db_url, 3)
    _anchor_chain(migrated_db_url, tenant_id)
    head_seqs = _stored_seqs(migrated_db_url, tenant_id)
    assert len(head_seqs) == 1, "premise: the tenant is anchored exactly once"
    assert head_seqs[0] > 2, "premise: the head seq leaves room for two older anchors below it"
    _plant_anchor(migrated_db_url, tenant_id, 1, PAST_WINDOW)
    _plant_anchor(migrated_db_url, tenant_id, 2, PAST_WINDOW)
    before = _verify(migrated_db_url, tenant_id)

    deleted = _purge(migrated_db_url, tenant_id, NOW)

    assert deleted == 2, "premise: the purge must have removed the two planted rows"
    assert _verify(migrated_db_url, tenant_id) == before
    assert before.anchored_seq == head_seqs[0]


def test_purging_one_tenant_deletes_no_row_of_another(migrated_db_url: str) -> None:
    """a project rule's other half: the rows walked answer for the bound tenant.

    Catches a DELETE written without the `tenant_id` filter — one statement
    over the whole table. Every single-tenant case in this file passes with
    that bug. Here BOTH tenants hold rows on either side of the cutoff, so an
    unfiltered purge empties B down to its head as well; the count is asserted
    as 2 rather than 4, and B's rows are counted afterwards, because an
    implementation that deleted everything and reported only its own tenant's
    share would pass a count check on its own.
    """
    tenant_a = _tenant(migrated_db_url)
    tenant_b = _tenant(migrated_db_url)
    for tenant_id in (tenant_a, tenant_b):
        _plant_anchor(migrated_db_url, tenant_id, 1, PAST_WINDOW)
        _plant_anchor(migrated_db_url, tenant_id, 2, PAST_WINDOW)
        _plant_anchor(migrated_db_url, tenant_id, 3, NOW)

    deleted = _purge(migrated_db_url, tenant_a, NOW)

    assert deleted == 2
    assert _stored_seqs(migrated_db_url, tenant_a) == [3]
    assert _stored_seqs(migrated_db_url, tenant_b) == [1, 2, 3]


# --- the external witness (decision E) ----------------------------------------


def test_a_purge_that_deleted_rows_logs_one_line_naming_the_tenant_and_the_count(
    migrated_db_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Decision E: the record of the upkeep, which is not an audit event.

    Catches a purge that deletes and ships nothing, leaving an operator with a
    table that shrinks and no line saying why or how much. Catches a line
    emitted per ROW rather than per purge, by counting exactly one over a purge
    that removed two rows — a per-row line would make a first run over a
    backlog print thousands.

    The fields are read off the record's `__dict__`, before `JsonFormatter`
    renders it: the formatter redacts a forbidden key in place rather than
    dropping it, so a producer that attached identity here would still emit a
    line with that key on it. SPEC section 13.5 is checked against the repo's
    own `FORBIDDEN_KEYS` rather than a list retyped here.
    """
    tenant_id = _tenant(migrated_db_url)
    _plant_anchor(migrated_db_url, tenant_id, 1, PAST_WINDOW)
    _plant_anchor(migrated_db_url, tenant_id, 2, PAST_WINDOW)
    _plant_anchor(migrated_db_url, tenant_id, 3, NOW)

    with caplog.at_level(logging.INFO, logger=RETENTION_LOGGER):
        deleted = _purge(migrated_db_url, tenant_id, NOW)

    assert deleted == 2
    lines = [record for record in caplog.records if record.getMessage() == PURGE_LOG_MESSAGE]
    assert len(lines) == 1, [record.getMessage() for record in caplog.records]
    payload = lines[0].__dict__
    assert str(payload["tenant_id"]) == str(tenant_id)
    assert payload["deleted"] == 2
    for forbidden in FORBIDDEN_KEYS:
        assert forbidden not in payload, f"{forbidden} must never reach a log line"


def test_a_purge_that_deleted_nothing_logs_no_line_at_all(
    migrated_db_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Plan section 1: silence is the record of a tick that removed nothing.

    Catches a line emitted unconditionally, which at one tenant a day for every
    tenant makes `audit.chain.anchors_purged` a message an operator stops
    reading — and the one purge that did delete a backlog then arrives in a
    stream of identical zero lines.
    """
    tenant_id = _tenant(migrated_db_url)
    _plant_anchor(migrated_db_url, tenant_id, 1, NOW)

    with caplog.at_level(logging.INFO, logger=RETENTION_LOGGER):
        deleted = _purge(migrated_db_url, tenant_id, NOW)

    assert deleted == 0
    assert [record for record in caplog.records if record.getMessage() == PURGE_LOG_MESSAGE] == []


# --- the database gate (decision A) -------------------------------------------


def test_the_restricted_role_may_delete_a_past_window_non_head_anchor(
    migrated_db_url: str, restricted_db_url: str
) -> None:
    """Decision A's permitted half, over the credentials the job actually holds.

    Catches the revision granting no DELETE (the job meets 42501 on every tick
    and the table grows for ever) and a trigger that branches on `TG_OP` but
    refuses the DELETE case too. Either one leaves every other trigger case in
    this file passing, because they all assert a REFUSAL.
    """
    tenant_id = _tenant(migrated_db_url)
    old_id = _plant_anchor(migrated_db_url, tenant_id, 1, PAST_WINDOW)
    _plant_anchor(migrated_db_url, tenant_id, 5, NOW)

    assert asyncio.run(_delete_anchor(restricted_db_url, old_id)) == 1
    assert _stored_seqs(migrated_db_url, tenant_id) == [5]


def test_a_delete_of_the_head_as_the_restricted_role_is_refused_by_the_trigger(
    migrated_db_url: str, restricted_db_url: str
) -> None:
    """Plan section 1's unhappy path: the head of the moment is refused everyone.

    "The head" here is the highest seq AT THE TIME OF THE STATEMENT, which is
    what the trigger compares against;
    `::test_a_head_demoted_by_a_fresh_insert_can_then_be_deleted` holds the
    other side of that qualifier.

    Both rows are past the window, so age cannot be what refuses this one and
    the `a higher audit_seq exists` clause is the only thing deciding it —
    delete that clause and the statement succeeds. The SQLSTATE is asserted as
    exactly P0001 rather than "it raises": the role now HOLDS DELETE, so 42501
    here would mean the grant never landed and the job cannot run at all.

    The row is read back afterwards, because a trigger that raised AFTER the
    delete inside the same transaction would satisfy `pytest.raises` alone.

    The MESSAGE is asserted here as well as the SQLSTATE, and this is the one
    case that does it: the project notes tells the operator to grep a `P0001`
    naming `audit_chain_anchors`, so a later `CREATE OR REPLACE` raising a
    bare "not permitted" would keep every SQLSTATE assertion in this file
    green and silently break the instruction the runbook publishes.
    """
    tenant_id = _tenant(migrated_db_url)
    _plant_anchor(migrated_db_url, tenant_id, 1, PAST_WINDOW)
    head_id = _plant_anchor(migrated_db_url, tenant_id, 5, PAST_WINDOW)

    async def scenario() -> None:
        async with _engine(restricted_db_url) as engine, engine.connect() as conn:
            with pytest.raises(DBAPIError) as caught:
                await conn.execute(
                    text(f"DELETE FROM {ANCHOR_TABLE} WHERE id = :id"), {"id": head_id}
                )
        message = str(caught.value)
        assert ANCHOR_TABLE in message, message
        assert "append-only" in message, message

        sqlstate = await _refused_sqlstate(
            restricted_db_url, f"DELETE FROM {ANCHOR_TABLE} WHERE id = :id", {"id": head_id}
        )
        assert sqlstate == TRIGGER_RAISED, (
            f"expected {TRIGGER_RAISED} for a DELETE of the head as {ROLE_NAME}, got {sqlstate}"
            + (
                f" ({INSUFFICIENT_PRIVILEGE} means the DELETE grant this plan adds "
                "never landed, so the job cannot run either)"
                if sqlstate == INSUFFICIENT_PRIVILEGE
                else ""
            )
        )

    asyncio.run(scenario())
    assert _stored_seqs(migrated_db_url, tenant_id) == [1, 5]


def test_a_delete_of_a_recent_non_head_anchor_as_the_restricted_role_is_refused(
    migrated_db_url: str, restricted_db_url: str
) -> None:
    """Plan section 1's other unhappy path: the window is enforced by the database.

    The row is not the head, so the `higher audit_seq exists` clause is
    satisfied and only the window clause can refuse it — delete that clause and
    the statement succeeds. Without this case the grant would let app
    credentials remove every anchor but the head, which is every anchor written
    since the last purge, and the truncation window would be unbounded rather
    than 90 days.
    """
    tenant_id = _tenant(migrated_db_url)
    recent_id = _plant_anchor(migrated_db_url, tenant_id, 1, NOW - timedelta(minutes=1))
    _plant_anchor(migrated_db_url, tenant_id, 5, NOW)

    async def scenario() -> None:
        sqlstate = await _refused_sqlstate(
            restricted_db_url, f"DELETE FROM {ANCHOR_TABLE} WHERE id = :id", {"id": recent_id}
        )
        assert sqlstate == TRIGGER_RAISED, (
            f"expected {TRIGGER_RAISED} for a DELETE inside the window as {ROLE_NAME}, "
            f"got {sqlstate}"
        )

    asyncio.run(scenario())
    assert _stored_seqs(migrated_db_url, tenant_id) == [1, 5]


def test_a_delete_of_the_head_as_the_table_owner_is_refused_by_the_trigger(
    migrated_db_url: str,
) -> None:
    """Decision A's stronger sentence: the owner is refused too.

    Catches a gate implemented with grants alone. Grants cannot express this
    half at all — the owner holds every privilege by definition — so a
    revision that simply granted DELETE and left an earlier change's blanket trigger
    replaced by nothing would pass the restricted cases above and still let the
    one role that runs migrations remove a head.
    """
    tenant_id = _tenant(migrated_db_url)
    _plant_anchor(migrated_db_url, tenant_id, 1, PAST_WINDOW)
    head_id = _plant_anchor(migrated_db_url, tenant_id, 5, PAST_WINDOW)

    async def scenario() -> None:
        sqlstate = await _refused_sqlstate(
            migrated_db_url, f"DELETE FROM {ANCHOR_TABLE} WHERE id = :id", {"id": head_id}
        )
        assert sqlstate == TRIGGER_RAISED

    asyncio.run(scenario())
    assert _stored_seqs(migrated_db_url, tenant_id) == [1, 5]


def test_a_head_demoted_by_a_fresh_insert_can_then_be_deleted(
    migrated_db_url: str, restricted_db_url: str
) -> None:
    """The residual, pinned so it is known behaviour rather than a surprise.

    a design note says the head is safe as at the time of the STATEMENT, and this
    case is why the wording is that careful. `helpdesk_app` has held INSERT on
    this table since an earlier change, and an INSERT fires no trigger, so a principal
    with those credentials can write an anchor above a tenant's head and then
    delete the row it just demoted, provided that row is past the window.

    The case exists to FAIL if the behaviour ever changes — a tightened
    trigger, or INSERT and DELETE split across two roles, which is what
    the project notes names as the close. Whoever makes that change is
    told here, and updates this case and the ADR together rather than finding
    the documents disagreeing with the database.

    It is not a claim that the path is safe. The anchor left behind does not
    match the log, so the next `verify_tenant_chain` answers a break rather
    than reporting the tenant intact.
    """
    tenant_id = _tenant(migrated_db_url)
    head_id = _plant_anchor(migrated_db_url, tenant_id, 1, NOW - timedelta(days=730))

    async def scenario() -> None:
        async with _engine(restricted_db_url) as engine, engine.begin() as conn:
            await conn.execute(
                text(
                    f"INSERT INTO {ANCHOR_TABLE} "
                    "(id, tenant_id, audit_seq, audit_hash, anchored_at) "
                    "VALUES (:id, :t, 2, :hash, now())"
                ),
                {"id": uuid.uuid4(), "t": tenant_id, "hash": SAMPLE_HASH},
            )
            result = await conn.execute(
                text(f"DELETE FROM {ANCHOR_TABLE} WHERE id = :id"), {"id": head_id}
            )
            assert result.rowcount == 1

    asyncio.run(scenario())
    assert _stored_seqs(migrated_db_url, tenant_id) == [2]


def test_a_pg_temp_shadow_of_the_anchor_table_does_not_unlock_the_head(
    migrated_db_url: str, restricted_db_url: str
) -> None:
    """Review finding: the trigger's EXISTS must not read the caller's temp schema.

    plpgsql resolves an unqualified relation against the CALLING session's
    `search_path`, and `pg_temp` is searched before `public`. Every role holds
    TEMPORARY on the database by default, so a connection holding only the
    application's credentials could create a temp table of the same name with
    one high `audit_seq` row, satisfy the trigger's "a higher seq exists"
    clause against it, and delete a quiet tenant's ancient head — after which
    that tenant has no anchor at all and verifies as intact for ever.

    Catches the `SET search_path` clause being dropped from the function. The
    delete targets `public` explicitly, so the trigger really does fire on the
    real table; only the EXISTS is shadowed, which is the whole attack.

    Both statements run on ONE connection: a temp table lives and dies with
    its session, so a case that opened a second one would pass whatever the
    function says.
    """
    tenant_id = _tenant(migrated_db_url)
    head_id = _plant_anchor(migrated_db_url, tenant_id, 1, NOW - timedelta(days=730))

    async def scenario() -> None:
        async with _engine(restricted_db_url) as engine, engine.connect() as conn:
            await conn.execute(
                text(
                    f"CREATE TEMP TABLE {ANCHOR_TABLE} "
                    "(tenant_id uuid, audit_seq bigint, anchored_at timestamptz)"
                )
            )
            await conn.execute(
                text(
                    f"INSERT INTO pg_temp.{ANCHOR_TABLE} (tenant_id, audit_seq, anchored_at) "
                    "VALUES (:t, 9223372036854775807, now())"
                ),
                {"t": tenant_id},
            )
            with pytest.raises(DBAPIError) as caught:
                await conn.execute(
                    text(f"DELETE FROM public.{ANCHOR_TABLE} WHERE id = :id"), {"id": head_id}
                )
        assert _sqlstate(caught.value) == TRIGGER_RAISED

    asyncio.run(scenario())
    assert _stored_seqs(migrated_db_url, tenant_id) == [1]


@pytest.mark.parametrize(
    ("label", "statement"),
    [
        ("UPDATE", f"UPDATE {ANCHOR_TABLE} SET audit_seq = 99 WHERE id = :id"),
        ("TRUNCATE", f"TRUNCATE {ANCHOR_TABLE}"),
    ],
)
def test_update_and_truncate_are_still_refused_by_privilege_after_the_grant(
    migrated_db_url: str, restricted_db_url: str, label: str, statement: str
) -> None:
    """Plan section 1: the grant widened by exactly one privilege and no more.

    Catches a revision that reached for `GRANT ALL` or `GRANT UPDATE, DELETE`
    to make the purge work. The row planted really matches the UPDATE's WHERE
    clause, so with the grant held the append-only trigger would stop it anyway
    and a case asserting only "this raises" would be green either way — the
    neighbouring-guard trap written out. Asserting 42501 exactly makes the case
    flip on the grant alone.

    An UPDATE that lands is how an attacker moves the head DOWN onto the
    truncation they just made; TRUNCATE empties every tenant's anchors at once,
    and a tenant with no anchor verifies clean.
    """
    tenant_id = _tenant(migrated_db_url)
    anchor_id = _plant_anchor(migrated_db_url, tenant_id, 1, NOW)

    async def scenario() -> None:
        parameters: dict[str, object] = {"id": anchor_id} if ":id" in statement else {}
        sqlstate = await _refused_sqlstate(restricted_db_url, statement, parameters)
        assert sqlstate == INSUFFICIENT_PRIVILEGE, (
            f"expected {INSUFFICIENT_PRIVILEGE} for {label} as {ROLE_NAME}, got {sqlstate}"
            + (
                f" ({TRIGGER_RAISED} means the trigger caught what the grants were "
                "supposed to stop)"
                if sqlstate == TRIGGER_RAISED
                else ""
            )
        )

    asyncio.run(scenario())


# --- the migration ------------------------------------------------------------


@pytest.mark.slow  # an earlier change: 2.8s call, runs the retention migration up and down
def test_the_retention_migration_round_trips_and_its_downgrade_restores_the_refusal(
    fresh_db_url: str,
) -> None:
    """Plan section 1's round trip, and the risk that the downgrade half-lands.

    Catches a downgrade that revokes the grant and leaves the branching
    function in place — the table would then permit deletes with no grant
    story, and the only thing standing between a future role and a head would
    be a clause nobody had reviewed. It is asserted from the behaviour, not the
    catalogue: after the downgrade an OLD NON-HEAD row, the one row this plan
    made deletable, must be refused again with P0001.

    Catches an upgrade that is not repeatable, too: the second upgrade runs
    over a database that has already had this revision applied once, so a
    `CREATE FUNCTION` where `CREATE OR REPLACE` belongs, or a `GRANT` guarded
    by a check that no longer holds, fails there.
    """
    _alembic("upgrade", "head", database_url=fresh_db_url)

    async def after_upgrade() -> None:
        granted = await _role_table_privileges(fresh_db_url)
        assert granted.get(ANCHOR_TABLE, frozenset()) == frozenset({"SELECT", "INSERT", "DELETE"})

    asyncio.run(after_upgrade())

    _alembic("downgrade", RETENTION_PARENT_REVISION, database_url=fresh_db_url)

    async def after_downgrade() -> None:
        granted = await _role_table_privileges(fresh_db_url)
        assert granted.get(ANCHOR_TABLE, frozenset()) == frozenset({"SELECT", "INSERT"}), (
            "the downgrade must revoke exactly the DELETE this revision granted, "
            "leaving plan 30bb's two"
        )
        tenant_id = await _plant_tenant(fresh_db_url, f"retention-down-{uuid.uuid4().hex[:8]}")
        old_id = await _insert_anchor(fresh_db_url, tenant_id, 1, PAST_WINDOW)
        await _insert_anchor(fresh_db_url, tenant_id, 5, PAST_WINDOW)
        sqlstate = await _refused_sqlstate(
            fresh_db_url, f"DELETE FROM {ANCHOR_TABLE} WHERE id = :id", {"id": old_id}
        )
        assert sqlstate == TRIGGER_RAISED, (
            "after the downgrade plan 30bb's blanket refusal must be back: an old "
            f"non-head row is refused again, got {sqlstate}"
        )

    asyncio.run(after_downgrade())

    _alembic("upgrade", "head", database_url=fresh_db_url)
    asyncio.run(after_upgrade())
