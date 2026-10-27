"""Model metadata shape, schema round-trip, migration parity.

Covers:
- exactly the declared tables exist: the nine directory-core ones (core only) plus ``audit_events``
- every table: single UUID pk ``id``, ``created_at``, ``updated_at`` (server-set)
- every tenant-scoped table: ``tenant_id`` FK to tenants + composite index led by it
- create_all schema round-trips one row per table through tenant-bound sessions
- ``alembic upgrade head`` from empty produces a schema identical to
  ``Base.metadata``; ``downgrade base`` returns to empty
"""

import asyncio
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, Table, Uuid, inspect, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from helpdesk.shared.core.db import create_session_factory, tenant_session
from helpdesk.shared.models import (
    Base,
    Department,
    Grant,
    Role,
    RoleTemplate,
    SodRule,
    System,
    Tenant,
    Ticket,
    User,
)
from tests.support.seed import ALL_TABLES, TENANT_SCOPED_TABLES, schema_engine, seed_tenant

REPO_ROOT = Path(__file__).resolve().parents[4]


def test_metadata_contains_exactly_the_declared_tables() -> None:
    """The hand-kept schema literal, and how many tables it names.

    The count is asserted beside the set (19 tenant-scoped tables
    with `audit_chain_anchors`, plus `tenants`). The allowlist
    is not derived from the code, so a plan that adds a table to BOTH
    `Base.metadata` and `_seed.py` still has to say so here; without the
    count, extending the allowlist is the whole of what it takes to keep this
    file green, and the one file meant to notice a new table never notices
    one.
    """
    assert set(Base.metadata.tables) == set(ALL_TABLES)
    assert len(ALL_TABLES) == 21
    assert len(TENANT_SCOPED_TABLES) == 20


@pytest.mark.parametrize("table_name", ALL_TABLES)
def test_standard_columns(table_name: str) -> None:
    table = Base.metadata.tables[table_name]

    pk_columns = [col.name for col in table.primary_key.columns]
    assert pk_columns == ["id"], f"{table_name}: pk must be exactly ['id'], got {pk_columns}"
    assert isinstance(table.c.id.type, Uuid), f"{table_name}: id must be a UUID column"

    for name in ("created_at", "updated_at"):
        assert name in table.c, f"{table_name}: missing {name}"
        col = table.c[name]
        assert not col.nullable, f"{table_name}.{name} must be NOT NULL"
        assert col.server_default is not None or col.default is not None, (
            f"{table_name}.{name} needs a default so inserts set it automatically"
        )


@pytest.mark.parametrize("table_name", TENANT_SCOPED_TABLES)
def test_tenant_scoped_tables_have_tenant_id_and_composite_index(table_name: str) -> None:
    table = Base.metadata.tables[table_name]

    assert "tenant_id" in table.c, f"{table_name}: missing tenant_id"
    col = table.c.tenant_id
    assert not col.nullable, f"{table_name}.tenant_id must be NOT NULL"
    fk_targets = {fk.column.table.name for fk in col.foreign_keys}
    assert fk_targets == {"tenants"}, f"{table_name}.tenant_id must FK to tenants"

    composite = [
        idx
        for idx in table.indexes
        if len(idx.columns) >= 2 and next(iter(idx.columns)).name == "tenant_id"
    ]
    assert composite, f"{table_name}: needs a composite index with tenant_id as the leading column"


def test_tenants_table_is_not_tenant_scoped() -> None:
    assert "tenant_id" not in Base.metadata.tables["tenants"].c


def test_tenants_table_has_no_residency_column() -> None:
    """Catches a model that still maps `Tenant.residency` (the column would then
    be written on every insert and the drop migration would drift from the
    metadata), and one that drops a neighbouring column by mistake.

    The wanted columns are asserted too, so an emptied table cannot pass.
    """
    columns = Base.metadata.tables["tenants"].c
    assert "residency" not in columns
    assert {"slug", "name", "settings"} <= set(columns.keys())


def test_schema_round_trip_one_row_per_table(temp_db_url: str) -> None:
    """create_all, insert one row per table (two roles), read every row back."""

    async def scenario() -> None:
        async with schema_engine(temp_db_url) as engine:
            factory = create_session_factory(engine)
            seeded = await seed_tenant(factory, "roundtrip-a")

            async with factory() as session:
                tenant = (await session.execute(select(Tenant))).scalars().one()
                assert tenant.id == seeded.tenant_id
                assert tenant.created_at is not None
                assert tenant.updated_at is not None

            async with tenant_session(factory, seeded.tenant_id) as session:
                expectations: list[tuple[type[Any], set[uuid.UUID]]] = [
                    (Department, {seeded.department_id}),
                    (User, {seeded.user_id}),
                    (System, {seeded.system_id}),
                    (Role, {seeded.role_id, seeded.role_b_id}),
                    (RoleTemplate, {seeded.role_template_id}),
                    (SodRule, {seeded.sod_rule_id}),
                    (Grant, {seeded.grant_id}),
                    (Ticket, {seeded.ticket_id}),
                ]
                for model, expected_ids in expectations:
                    rows = (await session.execute(select(model))).scalars().all()
                    assert {row.id for row in rows} == expected_ids, model.__name__
                    for row in rows:
                        assert row.tenant_id == seeded.tenant_id
                        assert row.created_at is not None
                        assert row.updated_at is not None

    asyncio.run(scenario())


def _alembic(command: str, database_url: str) -> None:
    result = subprocess.run(
        ["uv", "run", "alembic", command, "head" if command == "upgrade" else "base"],
        cwd=REPO_ROOT / "backend",  # alembic.ini lives in backend/
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"alembic {command} failed:\n{result.stderr}"


def _metadata_diff(conn: Connection) -> list[Any]:
    ctx = MigrationContext.configure(conn)
    diffs = compare_metadata(ctx, Base.metadata)
    return [d for d in diffs if not _mentions_alembic_version(d)]


def _mentions_alembic_version(diff: Any) -> bool:
    head = diff[0] if isinstance(diff, list) else diff
    for item in head if isinstance(head, tuple) else (head,):
        if isinstance(item, Table) and item.name == "alembic_version":
            return True
    return False


def _table_names(conn: Connection) -> set[str]:
    return set(inspect(conn).get_table_names())


def test_migrated_schema_matches_metadata_and_downgrades_clean(fresh_db_url: str) -> None:
    _alembic("upgrade", fresh_db_url)

    async def check() -> None:
        engine = create_async_engine(fresh_db_url)
        try:
            async with engine.connect() as conn:
                diffs = await conn.run_sync(_metadata_diff)
                assert diffs == [], f"migration drifts from Base.metadata: {diffs}"
        finally:
            await engine.dispose()

    asyncio.run(check())

    _alembic("downgrade", fresh_db_url)

    async def check_empty() -> None:
        engine = create_async_engine(fresh_db_url)
        try:
            async with engine.connect() as conn:
                names = await conn.run_sync(_table_names)
                assert names <= {"alembic_version"}, f"downgrade base left tables: {names}"
        finally:
            await engine.dispose()

    asyncio.run(check_empty())


_REVISION_BEFORE_DROP = "a9d3e5b17c40"
_DROP_REVISION = "e2b7c4a91f06"


def _alembic_to(args: list[str], database_url: str) -> None:
    result = subprocess.run(
        ["uv", "run", "alembic", *args],
        cwd=REPO_ROOT / "backend",
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"alembic {args} failed:\n{result.stderr}"


@pytest.mark.db
def test_drop_residency_migration_keeps_tenant_rows_and_downgrades_back(
    fresh_db_url: str,
) -> None:
    """Catches a migration that drops the table or rows instead of the column,
    one that leaves `residency` in place, a downgrade that adds the column
    nullable or without a fill (the `any` row must read `eu`), and a drop that
    is not one step past `a9d3e5b17c40`.

    The tenant is inserted at the old revision with `residency = 'any'`, so a
    downgrade that restored the old value (or no value) would fail on `eu`.
    """
    tenant_id = uuid.uuid4()
    _alembic_to(["upgrade", _REVISION_BEFORE_DROP], fresh_db_url)

    async def insert_old_row() -> None:
        engine = create_async_engine(fresh_db_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO tenants (id, slug, name, residency, settings) "
                        "VALUES (:id, 'fake-drop-res', 'Fake Drop Co', 'any', "
                        "CAST('{\"k\": 1}' AS jsonb))"
                    ),
                    {"id": tenant_id},
                )
        finally:
            await engine.dispose()

    asyncio.run(insert_old_row())

    _alembic_to(["upgrade", _DROP_REVISION], fresh_db_url)

    async def read_row_and_columns() -> tuple[Any, set[str]]:
        engine = create_async_engine(fresh_db_url)
        try:
            async with engine.connect() as conn:
                row = (
                    await conn.execute(
                        text("SELECT id, slug, name, settings FROM tenants WHERE id = :id"),
                        {"id": tenant_id},
                    )
                ).one()
                cols = await conn.run_sync(
                    lambda c: {col["name"] for col in inspect(c).get_columns("tenants")}
                )
                return row, cols
        finally:
            await engine.dispose()

    row, cols = asyncio.run(read_row_and_columns())
    assert "residency" not in cols
    assert tuple(row) == (tenant_id, "fake-drop-res", "Fake Drop Co", {"k": 1})

    _alembic_to(["downgrade", "-1"], fresh_db_url)

    async def read_after_downgrade() -> tuple[Any, Any]:
        engine = create_async_engine(fresh_db_url)
        try:
            async with engine.connect() as conn:
                value = (
                    await conn.execute(
                        text("SELECT residency FROM tenants WHERE id = :id"), {"id": tenant_id}
                    )
                ).scalar_one()
                column = await conn.run_sync(
                    lambda c: next(
                        col
                        for col in inspect(c).get_columns("tenants")
                        if col["name"] == "residency"
                    )
                )
                version = (
                    await conn.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar_one()
                return (value, column), version
        finally:
            await engine.dispose()

    (value, column), version = asyncio.run(read_after_downgrade())
    assert value == "eu"
    assert column["nullable"] is False
    assert str(column["type"]) == "VARCHAR(16)"
    assert version == _REVISION_BEFORE_DROP
