"""The readable-numbers migration: its place in the chain and its backfill.

The migration file's name carries a generated revision id, so it is found by
the `_readable_numbers.py` suffix. Its backfill lives in a module-level
function, ``backfill_numbers(connection)``, that the migration's ``upgrade``
also calls, so the test runs the very same SQL.

Cost: the backfill case carries ``db`` because ordering a window over real
rows and filling a counter table are things only Postgres can show. It builds
its own schema on a pristine database (the schema is exactly what it
changes), the way the other schema and migration tests do.
"""

import asyncio
import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import Connection, insert, text
from sqlalchemy.ext.asyncio import AsyncEngine

from helpdesk.shared.core.db import create_session_factory
from helpdesk.shared.models import AccessRequest
from tests.support.seed import SeededTenant, schema_engine, seed_tenant

VERSIONS = Path(__file__).resolve().parents[2] / "migrations" / "versions"
PREVIOUS_HEAD = "e2c5a9f70b14"
BASE_TIME = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


def _migration() -> ModuleType:
    matches = sorted(VERSIONS.glob("*_readable_numbers.py"))
    assert len(matches) == 1, f"expected one readable_numbers migration, got {matches}"
    spec = importlib.util.spec_from_file_location("readable_numbers_migration", matches[0])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_sits_on_the_previous_head() -> None:
    """Catches a migration chained to the wrong parent: a second head, or one
    that skips the anchor-retention migration, makes `alembic upgrade head`
    fail or run out of order."""
    assert _migration().down_revision == PREVIOUS_HEAD


async def _plant_request(engine: AsyncEngine, seeded: SeededTenant, *, minutes: int) -> uuid.UUID:
    """One old request with NO number, created `minutes` after the base time."""
    request_id = uuid.uuid4()
    stamp = BASE_TIME + timedelta(minutes=minutes)
    async with engine.begin() as conn:
        await conn.execute(
            insert(AccessRequest).values(
                id=request_id,
                tenant_id=seeded.tenant_id,
                number=None,
                requester_user_id=seeded.user_id,
                target_user_id=seeded.user_id,
                role_id=seeded.role_id,
                justification="fake old request",
                requested_expiry_days=None,
                risk_class="low",
                decision="auto",
                decision_reasons=[],
                citations=[],
                status="executed",
                approver_user_ids=[],
                approvals=[],
                session_id=None,
                trace_id=None,
                idempotency_key=None,
                created_at=stamp,
                updated_at=stamp,
            )
        )
    return request_id


async def _numbers(engine: AsyncEngine, table: str, tenant_id: uuid.UUID) -> dict[uuid.UUID, int]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(f"SELECT id, number FROM {table} WHERE tenant_id = :t"),
            {"t": tenant_id},
        )
        return {row[0]: row[1] for row in rows}


async def _counters(engine: AsyncEngine, tenant_id: uuid.UUID) -> dict[str, int]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT kind, last FROM reference_counters WHERE tenant_id = :t"),
            {"t": tenant_id},
        )
        return {row[0]: row[1] for row in rows}


async def _as_before_the_migration(engine: AsyncEngine) -> None:
    """Put the schema back as it was before: no counter rows, no numbers."""
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE access_requests ALTER COLUMN number DROP NOT NULL"))
        await conn.execute(text("ALTER TABLE tickets ALTER COLUMN number DROP NOT NULL"))
        await conn.execute(text("UPDATE tickets SET number = NULL"))
        await conn.execute(text("DELETE FROM reference_counters"))


async def _run_backfill(engine: AsyncEngine) -> None:
    backfill = _migration().backfill_numbers

    def call(connection: Connection) -> None:
        backfill(connection)

    async with engine.begin() as conn:
        await conn.run_sync(call)


@pytest.mark.db
def test_backfill_numbers_old_rows_by_creation_time(fresh_db_url: str) -> None:
    """Catches a backfill that numbers by row id or insert order instead of
    creation time (the rows are inserted newest first, so only ordering by
    `created_at` gives 1, 2, 3 oldest to newest), one that counts across
    companies (company B must restart at 1), and one that leaves the counter
    table behind the rows (the next real request would repeat a number):
    the counters must read 3 and 1, and the tickets are numbered the same way."""

    async def scenario() -> None:
        async with schema_engine(fresh_db_url) as engine:
            factory = create_session_factory(engine)
            company_a = await seed_tenant(factory, "fill-a")
            company_b = await seed_tenant(factory, "fill-b")
            await _as_before_the_migration(engine)
            newest = await _plant_request(engine, company_a, minutes=30)
            oldest = await _plant_request(engine, company_a, minutes=10)
            middle = await _plant_request(engine, company_a, minutes=20)
            only_b = await _plant_request(engine, company_b, minutes=5)

            await _run_backfill(engine)

            numbers_a = await _numbers(engine, "access_requests", company_a.tenant_id)
            assert numbers_a == {oldest: 1, middle: 2, newest: 3}
            numbers_b = await _numbers(engine, "access_requests", company_b.tenant_id)
            assert numbers_b == {only_b: 1}
            counters_a = await _counters(engine, company_a.tenant_id)
            counters_b = await _counters(engine, company_b.tenant_id)
            assert counters_a == {"access_request": 3, "ticket": 1}
            assert counters_b == {"access_request": 1, "ticket": 1}
            tickets_a = await _numbers(engine, "tickets", company_a.tenant_id)
            assert list(tickets_a.values()) == [1]

    asyncio.run(scenario())
