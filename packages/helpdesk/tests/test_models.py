"""An earlier change acceptance: model metadata shape, schema round-trip, migration parity.

Covers:
- exactly the declared tables exist: the nine directory-core ones (scope
  decision A: core only) plus ``audit_events`` from an earlier change
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
from _seed import ALL_TABLES, TENANT_SCOPED_TABLES, schema_engine, seed_tenant
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from helpdesk.core.db import create_session_factory, tenant_session
from helpdesk.models import (
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
from sqlalchemy import Connection, Table, Uuid, inspect, select
from sqlalchemy.ext.asyncio import create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_metadata_contains_exactly_the_declared_tables() -> None:
    assert set(Base.metadata.tables) == set(ALL_TABLES)


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
        cwd=REPO_ROOT,
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
