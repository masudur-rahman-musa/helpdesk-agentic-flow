"""The `audit_events` table and its append-only trigger.

Expected implementation surface:

``helpdesk.models.audit.AuditEvent`` (also exported from ``helpdesk.models``)
    A ``TenantScoped`` model on table ``audit_events`` with these
    columns: ``seq`` (bigserial, DB-assigned), ``occurred_at``,
    ``actor_kind`` (``user``/``agent``/``system``), ``actor_ref``,
    ``session_id``, ``trace_id``, ``event_type``, ``subject_type``,
    ``subject_id``, ``payload`` (jsonb), ``prev_hash``, ``hash``.

One Alembic revision creates the table, a ``(tenant_id, seq)`` index, and a
trigger plus function that raise on UPDATE and DELETE. ``downgrade`` drops the trigger and the
function explicitly, so
downgrade → upgrade round-trips.

These tests run Alembic against a pristine throwaway database (the
``fresh_db_url`` fixture) exactly like ``test_models.py`` does, because the
trigger exists only in the migration — ``Base.metadata.create_all`` never
creates it.
"""

import asyncio
import os
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import BigInteger, Connection, inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from helpdesk.shared.models import AuditEvent, TenantScoped

REPO_ROOT = Path(__file__).resolve().parents[4]

SPEC_COLUMNS = (
    "tenant_id",
    "seq",
    "occurred_at",
    "actor_kind",
    "actor_ref",
    "session_id",
    "trace_id",
    "event_type",
    "subject_type",
    "subject_id",
    "payload",
    "prev_hash",
    "hash",
)

GENESIS = "0" * 64
FAKE_HASH = "a" * 64

_INSERT_TENANT = text(
    "INSERT INTO tenants (id, slug, name, settings) "
    "VALUES (:id, :slug, 'Fake Audit Tenant', '{}'::jsonb)"
)

_INSERT_EVENT = text(
    "INSERT INTO audit_events "
    "(id, tenant_id, occurred_at, actor_kind, actor_ref, session_id, trace_id, "
    " event_type, subject_type, subject_id, payload, prev_hash, hash) "
    "VALUES (:id, :tenant_id, :occurred_at, 'system', :actor_ref, NULL, 'trace-fake', "
    " :event_type, 'user', :subject_id, '{\"fake\": true}'::jsonb, :prev_hash, :hash) "
    "RETURNING seq"
)


def _alembic(command: str, revision: str, database_url: str) -> None:
    result = subprocess.run(
        ["uv", "run", "alembic", command, revision],
        cwd=REPO_ROOT / "backend",  # alembic.ini lives in backend/
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"alembic {command} {revision} failed:\n{result.stderr}"


def _table_names(conn: Connection) -> set[str]:
    return set(inspect(conn).get_table_names())


def _column_names(conn: Connection, table: str) -> set[str]:
    return {col["name"] for col in inspect(conn).get_columns(table)}


def _indexes(conn: Connection, table: str) -> list[dict[str, Any]]:
    return [dict(index) for index in inspect(conn).get_indexes(table)]


# --- model shape (no DB) ------------------------------------------------------


def test_audit_event_is_tenant_scoped_on_the_spec_table() -> None:
    assert issubclass(AuditEvent, TenantScoped)
    assert AuditEvent.__tablename__ == "audit_events"


def test_audit_event_has_every_spec_column() -> None:
    columns = set(AuditEvent.__table__.c.keys())
    missing = set(SPEC_COLUMNS) - columns
    assert not missing, f"audit_events is missing SPEC §6 columns: {sorted(missing)}"


def test_seq_is_a_db_assigned_bigint() -> None:
    seq = AuditEvent.__table__.c.seq
    assert isinstance(seq.type, BigInteger), "seq must be bigserial-backed (BigInteger)"
    assert not seq.nullable
    # Assigned by the database, never by the writer; the
    # insert tests below prove it by never supplying a value for it.
    assert "seq" not in {column.name for column in AuditEvent.__table__.primary_key}


@pytest.mark.parametrize("column", ["prev_hash", "hash"])
def test_chain_columns_are_not_nullable(column: str) -> None:
    assert not AuditEvent.__table__.c[column].nullable


# --- migration + trigger (DB) -------------------------------------------------


async def _seed_one_event(database_url: str) -> tuple[uuid.UUID, uuid.UUID, int]:
    """(tenant_id, event_id, seq) for one inserted genesis event."""
    tenant_id = uuid.uuid4()
    event_id = uuid.uuid4()
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                _INSERT_TENANT, {"id": tenant_id, "slug": f"fake-{tenant_id.hex[:8]}"}
            )
            seq = (
                await conn.execute(
                    _INSERT_EVENT,
                    {
                        "id": event_id,
                        "tenant_id": tenant_id,
                        "occurred_at": datetime.now(UTC),
                        "actor_ref": str(uuid.uuid4()),
                        "event_type": "auth.login",
                        "subject_id": str(uuid.uuid4()),
                        "prev_hash": GENESIS,
                        "hash": FAKE_HASH,
                    },
                )
            ).scalar_one()
        return tenant_id, event_id, int(seq)
    finally:
        await engine.dispose()


def test_migration_creates_audit_events_with_spec_columns_and_index(fresh_db_url: str) -> None:
    _alembic("upgrade", "head", fresh_db_url)

    async def check() -> None:
        engine = create_async_engine(fresh_db_url)
        try:
            async with engine.connect() as conn:
                tables = await conn.run_sync(_table_names)
                assert "audit_events" in tables, f"migration did not create audit_events: {tables}"

                columns = await conn.run_sync(lambda c: _column_names(c, "audit_events"))
                missing = set(SPEC_COLUMNS) - columns
                assert not missing, f"audit_events missing columns: {sorted(missing)}"

                indexes = await conn.run_sync(lambda c: _indexes(c, "audit_events"))
                assert any(
                    list(index["column_names"])[:2] == ["tenant_id", "seq"] for index in indexes
                ), f"expected an index led by (tenant_id, seq), got {indexes}"
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_insert_works_and_seq_is_assigned_increasing(fresh_db_url: str) -> None:
    _alembic("upgrade", "head", fresh_db_url)

    async def scenario() -> None:
        tenant_id, _event_id, first_seq = await _seed_one_event(fresh_db_url)
        engine = create_async_engine(fresh_db_url)
        try:
            async with engine.begin() as conn:
                second_seq = int(
                    (
                        await conn.execute(
                            _INSERT_EVENT,
                            {
                                "id": uuid.uuid4(),
                                "tenant_id": tenant_id,
                                "occurred_at": datetime.now(UTC),
                                "actor_ref": str(uuid.uuid4()),
                                "event_type": "auth.failed",
                                "subject_id": str(uuid.uuid4()),
                                "prev_hash": FAKE_HASH,
                                "hash": "b" * 64,
                            },
                        )
                    ).scalar_one()
                )
            assert second_seq > first_seq, "seq must increase; it orders the verify walk"

            async with engine.connect() as conn:
                count = int(
                    (
                        await conn.execute(
                            text("SELECT count(*) FROM audit_events WHERE tenant_id = :t"),
                            {"t": tenant_id},
                        )
                    ).scalar_one()
                )
            assert count == 2
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("label", "statement"),
    [
        ("update", "UPDATE audit_events SET payload = '{\"fake\": false}'::jsonb WHERE id = :id"),
        ("delete", "DELETE FROM audit_events WHERE id = :id"),
        # TRUNCATE fires no row-level trigger, and an emptied chain verifies
        # as intact, so it needs its own statement-level guard.
        ("truncate", "TRUNCATE audit_events"),
    ],
)
def test_update_and_delete_are_rejected_by_the_trigger(
    fresh_db_url: str, label: str, statement: str
) -> None:
    _alembic("upgrade", "head", fresh_db_url)

    async def scenario() -> None:
        _tenant_id, event_id, _seq = await _seed_one_event(fresh_db_url)
        engine = create_async_engine(fresh_db_url)
        try:
            with pytest.raises(DBAPIError):
                async with engine.begin() as conn:
                    await conn.execute(text(statement), {"id": event_id})

            async with engine.connect() as conn:
                surviving = int(
                    (
                        await conn.execute(
                            text(
                                "SELECT count(*) FROM audit_events "
                                "WHERE id = :id AND payload->>'fake' = 'true'"
                            ),
                            {"id": event_id},
                        )
                    ).scalar_one()
                )
            assert surviving == 1, f"{label} was not rejected: the row changed or vanished"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.slow  # 2.6s call, runs the audit migration down then up
def test_downgrade_then_upgrade_round_trips_with_the_trigger_intact(fresh_db_url: str) -> None:
    """The trigger and its function must be dropped explicitly."""
    _alembic("upgrade", "head", fresh_db_url)
    _alembic("downgrade", "base", fresh_db_url)

    async def check_gone() -> None:
        engine = create_async_engine(fresh_db_url)
        try:
            async with engine.connect() as conn:
                tables = await conn.run_sync(_table_names)
                assert "audit_events" not in tables, f"downgrade left audit_events: {tables}"
        finally:
            await engine.dispose()

    asyncio.run(check_gone())

    _alembic("upgrade", "head", fresh_db_url)

    async def check_enforced_again() -> None:
        _tenant_id, event_id, _seq = await _seed_one_event(fresh_db_url)
        engine = create_async_engine(fresh_db_url)
        try:
            with pytest.raises(DBAPIError):
                async with engine.begin() as conn:
                    await conn.execute(
                        text("DELETE FROM audit_events WHERE id = :id"), {"id": event_id}
                    )
        finally:
            await engine.dispose()

    asyncio.run(check_enforced_again())
