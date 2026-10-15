"""An earlier change section 6: the idempotency key table and its repository helpers.

Expected implementation surface:

``helpdesk.models.IdempotencyKey`` (``helpdesk/models/idempotency.py``,
re-exported from ``helpdesk.models``)
    Table ``idempotency_keys`` (the design), a ``TenantScoped`` model with
    ``key: str``, ``tool_name: str``, ``request_fingerprint: str``,
    ``response: dict[str, Any]`` (JSONB) and ``expires_at: datetime``
    (timezone-aware). Unique on ``(tenant_id, key)`` — the backstop behind
    the advisory lock — plus an index on
    ``expires_at`` for the purge.

``helpdesk.core.repository`` (module-level helpers, like
``find_user_by_email``; they take a ``tenant_session`` and never a tenant id)
    ``create_idempotency_key(session, *, key, tool_name, request_fingerprint,
        response, expires_at) -> IdempotencyKey``
    ``find_idempotency_key(session, key, now) -> IdempotencyKey | None``
        the bound tenant's live row for ``key``; an expired row is absent.
    ``purge_expired(session, now) -> int``
        deletes the bound tenant's expired rows, returns how many.

Expiry boundary assumed here: a row is expired when ``expires_at <= now``
(the instant it names is the first instant it is gone).

Style follows ``test_models.py``/``test_repository.py``: schema from
``Base.metadata`` via ``_seed.schema_engine`` on the session-scoped
throwaway database, one ``asyncio.run`` per scenario.
"""

import asyncio
import os
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from helpdesk.shared.core.db import (
    UnboundTenantQueryError,
    create_session_factory,
    tenant_session,
)
from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.idempotency import (
    create_idempotency_key,
    delete_expired_idempotency_key,
    find_idempotency_key,
    purge_expired,
)
from helpdesk.shared.models import Base, IdempotencyKey
from tests.support.seed import schema_engine, seed_tenant

REPO_ROOT = Path(__file__).resolve().parents[5]

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
LIVE = NOW + timedelta(days=7)
EXPECTED_COLUMNS = frozenset(
    {
        "id",
        "tenant_id",
        "key",
        "tool_name",
        "request_fingerprint",
        "response",
        "expires_at",
        "created_at",
        "updated_at",
    }
)


async def _store(
    session: AsyncSession,
    key: str,
    *,
    expires_at: datetime = LIVE,
    tool_name: str = "revoke_access",
    fingerprint: str = "a" * 64,
    response: dict[str, Any] | None = None,
) -> IdempotencyKey:
    return await create_idempotency_key(
        session,
        key=key,
        tool_name=tool_name,
        request_fingerprint=fingerprint,
        response={"ok": True} if response is None else response,
        expires_at=expires_at,
    )


async def _rows(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> list[IdempotencyKey]:
    async with tenant_session(factory, tenant_id) as session:
        return list(await Repository(session, IdempotencyKey).list())


# --- the table itself ---------------------------------------------------------


def test_model_is_registered_under_the_spec_table_name() -> None:
    assert IdempotencyKey.__tablename__ == "idempotency_keys"
    assert "idempotency_keys" in Base.metadata.tables


def test_metadata_declares_the_spec_columns() -> None:
    columns = set(Base.metadata.tables["idempotency_keys"].c.keys())
    assert columns >= EXPECTED_COLUMNS, f"missing: {sorted(EXPECTED_COLUMNS - columns)}"


def test_metadata_is_unique_on_tenant_and_key() -> None:
    """The backstop behind the advisory lock: two rows for one key must be impossible."""
    table = Base.metadata.tables["idempotency_keys"]
    unique_sets = [
        tuple(col.name for col in index.columns) for index in table.indexes if index.unique
    ]
    unique_sets += [
        tuple(col.name for col in constraint.columns)
        for constraint in table.constraints
        if hasattr(constraint, "columns") and type(constraint).__name__ == "UniqueConstraint"
    ]
    assert ("tenant_id", "key") in unique_sets, f"no unique (tenant_id, key); found {unique_sets}"


def test_metadata_indexes_expires_at_for_the_purge() -> None:
    table = Base.metadata.tables["idempotency_keys"]
    indexed_leads = {next(iter(index.columns)).name for index in table.indexes}
    assert "expires_at" in indexed_leads, "purge_expired scans by expires_at"


def _reflect(conn: Connection) -> tuple[set[str], list[Any], list[Any]]:
    """(columns, indexes, unique constraints) as Postgres actually has them."""
    inspector = inspect(conn)
    return (
        {col["name"] for col in inspector.get_columns("idempotency_keys")},
        list(inspector.get_indexes("idempotency_keys")),
        list(inspector.get_unique_constraints("idempotency_keys")),
    )


def test_migration_creates_the_table_with_its_indexes(fresh_db_url: str) -> None:
    """Section 6: the table exists after `alembic upgrade head`, not only in metadata."""
    result = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT / "backend",  # alembic.ini lives in backend/
        env={**os.environ, "DATABASE_URL": fresh_db_url},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"alembic upgrade failed:\n{result.stderr}"

    async def check() -> None:
        engine = create_async_engine(fresh_db_url)
        try:
            async with engine.connect() as conn:
                columns, indexes, uniques = await conn.run_sync(_reflect)
        finally:
            await engine.dispose()

        assert columns >= EXPECTED_COLUMNS, f"missing: {sorted(EXPECTED_COLUMNS - columns)}"

        unique_sets = [tuple(idx["column_names"]) for idx in indexes if idx["unique"]]
        unique_sets += [tuple(uq["column_names"]) for uq in uniques]
        assert ("tenant_id", "key") in unique_sets, f"no unique (tenant_id, key): {unique_sets}"

        lead_columns = {next(iter(idx["column_names"])) for idx in indexes if idx["column_names"]}
        assert "expires_at" in lead_columns, f"expires_at is not indexed: {indexes}"

    asyncio.run(check())


def test_unique_constraint_rejects_a_second_row_for_the_same_key(temp_db_url: str) -> None:
    async def scenario() -> None:
        async with schema_engine(temp_db_url) as engine:
            factory = create_session_factory(engine)
            seeded = await seed_tenant(factory, "idem-uniq")

            with pytest.raises(IntegrityError):
                async with tenant_session(factory, seeded.tenant_id) as session:
                    await _store(session, "fake-key-1")
                    await _store(session, "fake-key-1", fingerprint="b" * 64)

    asyncio.run(scenario())


# --- the helpers are tenant-scoped --------------------------------------------


def test_create_stamps_the_bound_tenant_and_find_returns_the_row(temp_db_url: str) -> None:
    async def scenario() -> None:
        async with schema_engine(temp_db_url) as engine:
            factory = create_session_factory(engine)
            seeded = await seed_tenant(factory, "idem-roundtrip")

            async with tenant_session(factory, seeded.tenant_id) as session:
                created = await _store(
                    session,
                    "fake-key-roundtrip",
                    tool_name="approve_request",
                    fingerprint="c" * 64,
                    response={"status": "approved", "fake": True},
                )
                assert created.tenant_id == seeded.tenant_id

            async with tenant_session(factory, seeded.tenant_id) as session:
                found = await find_idempotency_key(session, "fake-key-roundtrip", NOW)
            assert found is not None
            assert found.key == "fake-key-roundtrip"
            assert found.tool_name == "approve_request"
            assert found.request_fingerprint == "c" * 64
            assert found.response == {"status": "approved", "fake": True}
            assert found.expires_at == LIVE

    asyncio.run(scenario())


def test_an_unknown_key_is_absent(temp_db_url: str) -> None:
    async def scenario() -> None:
        async with schema_engine(temp_db_url) as engine:
            factory = create_session_factory(engine)
            seeded = await seed_tenant(factory, "idem-missing")

            async with tenant_session(factory, seeded.tenant_id) as session:
                assert await find_idempotency_key(session, "never-stored", NOW) is None

    asyncio.run(scenario())


def test_the_same_key_in_another_tenant_is_an_independent_entry(temp_db_url: str) -> None:
    """Keys are tenant-scoped: one tenant's UUID v4 can never answer another's call."""

    async def scenario() -> None:
        async with schema_engine(temp_db_url) as engine:
            factory = create_session_factory(engine)
            seeded_a = await seed_tenant(factory, "idem-x")
            seeded_b = await seed_tenant(factory, "idem-y")

            async with tenant_session(factory, seeded_a.tenant_id) as session:
                await _store(session, "shared-fake-key", response={"tenant": "a"})
            async with tenant_session(factory, seeded_b.tenant_id) as session:
                await _store(session, "shared-fake-key", response={"tenant": "b"})

            async with tenant_session(factory, seeded_a.tenant_id) as session:
                row_a = await find_idempotency_key(session, "shared-fake-key", NOW)
            async with tenant_session(factory, seeded_b.tenant_id) as session:
                row_b = await find_idempotency_key(session, "shared-fake-key", NOW)

            assert row_a is not None and row_b is not None
            assert row_a.id != row_b.id
            assert row_a.response == {"tenant": "a"}
            assert row_b.response == {"tenant": "b"}

    asyncio.run(scenario())


def test_a_key_stored_by_another_tenant_reads_as_absent(temp_db_url: str) -> None:
    async def scenario() -> None:
        async with schema_engine(temp_db_url) as engine:
            factory = create_session_factory(engine)
            seeded_a = await seed_tenant(factory, "idem-cross-a")
            seeded_b = await seed_tenant(factory, "idem-cross-b")

            async with tenant_session(factory, seeded_a.tenant_id) as session:
                await _store(session, "only-in-a")

            async with tenant_session(factory, seeded_b.tenant_id) as session:
                assert await find_idempotency_key(session, "only-in-a", NOW) is None

            assert len(await _rows(factory, seeded_b.tenant_id)) == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("helper", ["create", "find", "purge", "delete-expired"])
def test_helpers_refuse_a_session_with_no_tenant_bound(temp_db_url: str, helper: str) -> None:
    """No tenant bound means no silent full-table access (a project rule, an earlier change)."""

    async def scenario() -> None:
        async with schema_engine(temp_db_url) as engine:
            factory = create_session_factory(engine)
            await seed_tenant(factory, f"idem-unbound-{helper}")

            async with factory() as session:
                with pytest.raises(UnboundTenantQueryError):
                    if helper == "create":
                        await _store(session, "fake-key-unbound")
                    elif helper == "find":
                        await find_idempotency_key(session, "fake-key-unbound", NOW)
                    elif helper == "purge":
                        await purge_expired(session, NOW)
                    else:
                        await delete_expired_idempotency_key(session, "fake-key-unbound", NOW)

    asyncio.run(scenario())


# --- expiry -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expires_at", "still_visible"),
    [
        pytest.param(NOW + timedelta(days=7), True, id="fresh"),
        pytest.param(NOW + timedelta(seconds=1), True, id="one-second-left"),
        pytest.param(NOW, False, id="boundary-expires-exactly-now"),
        pytest.param(NOW - timedelta(seconds=1), False, id="one-second-past"),
        pytest.param(NOW - timedelta(days=7), False, id="long-gone"),
    ],
)
def test_expiry_boundary_decides_whether_the_reader_sees_the_row(
    temp_db_url: str, expires_at: datetime, still_visible: bool
) -> None:
    """An expired key is treated as absent, so the effect may run again."""

    async def scenario() -> None:
        async with schema_engine(temp_db_url) as engine:
            factory = create_session_factory(engine)
            seeded = await seed_tenant(factory, f"idem-exp-{expires_at.timestamp()}")

            async with tenant_session(factory, seeded.tenant_id) as session:
                await _store(session, "fake-key-expiry", expires_at=expires_at)

            async with tenant_session(factory, seeded.tenant_id) as session:
                found = await find_idempotency_key(session, "fake-key-expiry", NOW)
            assert (found is not None) is still_visible

            # Absent to the reader, but still on disk until the purge runs.
            assert len(await _rows(factory, seeded.tenant_id)) == 1

    asyncio.run(scenario())


def test_deleting_one_expired_key_never_touches_a_live_one(temp_db_url: str) -> None:
    """The guard calls this to free an expired key before re-claiming it, so
    the expiry condition has to be in the statement, not in the caller."""

    async def scenario() -> None:
        async with schema_engine(temp_db_url) as engine:
            factory = create_session_factory(engine)
            seeded = await seed_tenant(factory, "idem-delete-one")

            async with tenant_session(factory, seeded.tenant_id) as session:
                await _store(session, "stale", expires_at=NOW - timedelta(seconds=1))
                await _store(session, "live", expires_at=NOW + timedelta(days=1))

            async with tenant_session(factory, seeded.tenant_id) as session:
                assert await delete_expired_idempotency_key(session, "live", NOW) is False
                assert await delete_expired_idempotency_key(session, "absent", NOW) is False
                assert await delete_expired_idempotency_key(session, "stale", NOW) is True

            assert {row.key for row in await _rows(factory, seeded.tenant_id)} == {"live"}

    asyncio.run(scenario())


def test_deleting_an_expired_key_is_tenant_scoped(temp_db_url: str) -> None:
    async def scenario() -> None:
        async with schema_engine(temp_db_url) as engine:
            factory = create_session_factory(engine)
            seeded_a = await seed_tenant(factory, "idem-del-a")
            seeded_b = await seed_tenant(factory, "idem-del-b")

            async with tenant_session(factory, seeded_b.tenant_id) as session:
                await _store(session, "stale", expires_at=NOW - timedelta(seconds=1))

            async with tenant_session(factory, seeded_a.tenant_id) as session:
                assert await delete_expired_idempotency_key(session, "stale", NOW) is False

            assert len(await _rows(factory, seeded_b.tenant_id)) == 1

    asyncio.run(scenario())


def test_purge_expired_deletes_only_expired_rows_and_returns_the_count(temp_db_url: str) -> None:
    async def scenario() -> None:
        async with schema_engine(temp_db_url) as engine:
            factory = create_session_factory(engine)
            seeded = await seed_tenant(factory, "idem-purge")

            async with tenant_session(factory, seeded.tenant_id) as session:
                await _store(session, "gone-1", expires_at=NOW - timedelta(days=1))
                await _store(session, "gone-2", expires_at=NOW - timedelta(seconds=1))
                await _store(session, "gone-3-boundary", expires_at=NOW)
                await _store(session, "kept-1", expires_at=NOW + timedelta(seconds=1))
                await _store(session, "kept-2", expires_at=NOW + timedelta(days=7))

            async with tenant_session(factory, seeded.tenant_id) as session:
                deleted = await purge_expired(session, NOW)
            assert deleted == 3

            remaining = {row.key for row in await _rows(factory, seeded.tenant_id)}
            assert remaining == {"kept-1", "kept-2"}

    asyncio.run(scenario())


def test_purge_expired_returns_zero_when_nothing_has_expired(temp_db_url: str) -> None:
    async def scenario() -> None:
        async with schema_engine(temp_db_url) as engine:
            factory = create_session_factory(engine)
            seeded = await seed_tenant(factory, "idem-purge-none")

            async with tenant_session(factory, seeded.tenant_id) as session:
                await _store(session, "kept", expires_at=NOW + timedelta(days=1))

            async with tenant_session(factory, seeded.tenant_id) as session:
                assert await purge_expired(session, NOW) == 0

            assert len(await _rows(factory, seeded.tenant_id)) == 1

    asyncio.run(scenario())


def test_purge_expired_is_tenant_scoped(temp_db_url: str) -> None:
    """A purge run for one tenant must never reach into another's rows."""

    async def scenario() -> None:
        async with schema_engine(temp_db_url) as engine:
            factory = create_session_factory(engine)
            seeded_a = await seed_tenant(factory, "idem-purge-a")
            seeded_b = await seed_tenant(factory, "idem-purge-b")

            for seeded in (seeded_a, seeded_b):
                async with tenant_session(factory, seeded.tenant_id) as session:
                    await _store(session, "expired", expires_at=NOW - timedelta(days=1))

            async with tenant_session(factory, seeded_a.tenant_id) as session:
                assert await purge_expired(session, NOW) == 1

            assert await _rows(factory, seeded_a.tenant_id) == []
            assert len(await _rows(factory, seeded_b.tenant_id)) == 1

    asyncio.run(scenario())
