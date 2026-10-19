"""Acceptance: one shared engine and a fresh tenant per test.

These tests pin the three NEW session fixtures (not built yet, so this file
is red until they are added to ``backend/tests/conftest.py``):

``shared_engine`` (session-scoped)
    The ONE ``AsyncEngine`` for the whole run; schema built once from
    ``helpdesk.shared.models.Base.metadata``; the same object every test gets.
``session_factory`` (session-scoped)
    An ``async_sessionmaker`` over ``shared_engine``, still carrying the
    tenant guard.
``fresh_tenant`` (function-scoped)
    One brand-new empty ``Tenant`` row (unique slug) per test; returns its id.

To prove "same engine across tests" and "new tenant each test" without a
service only one test can show, sibling tests share a module-level global:
the first writes it, a later one reads it. Order within this file is
top-to-bottom, so the writer runs before the reader.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from helpdesk.shared.core.db import UnboundTenantQueryError, tenant_session
from helpdesk.shared.models import Department, User
from tests.support.seed import ALL_TABLES

# Written by the "_first" sibling, read by the "_second" sibling. The reader
# runs later because file order is top-to-bottom.
_recorded_engine_id: int | None = None
_recorded_tenant_id: uuid.UUID | None = None


def test_the_engine_is_the_same_object_across_tests_first(shared_engine: AsyncEngine) -> None:
    """Records the engine id for its sibling; catches a fixture that rebuilds an
    engine per test, which would make the two recorded ids differ."""
    global _recorded_engine_id
    _recorded_engine_id = id(shared_engine)


def test_the_engine_is_the_same_object_across_tests_second(shared_engine: AsyncEngine) -> None:
    """Catches a fixture that hands each test its own fresh engine instead of the
    one session-scoped object: a per-test engine would change ``id()``."""
    assert _recorded_engine_id is not None, "sibling test must run first and record the id"
    assert id(shared_engine) == _recorded_engine_id, (
        "shared_engine gave a different object on the second test; it is not session-scoped"
    )


def test_each_test_gets_a_new_tenant_id_first(fresh_tenant: uuid.UUID) -> None:
    """Records this test's tenant id for its sibling; catches a session-scoped
    (reused) tenant, which would hand the sibling the same id."""
    global _recorded_tenant_id
    _recorded_tenant_id = fresh_tenant


def test_each_test_gets_a_new_tenant_id_second(fresh_tenant: uuid.UUID) -> None:
    """Catches a reused tenant: a session-scoped fixture would repeat the id, so a
    brand-new tenant per test is the only thing that makes these two differ."""
    assert _recorded_tenant_id is not None, "sibling test must run first and record the id"
    assert fresh_tenant != _recorded_tenant_id, (
        "fresh_tenant repeated an id across tests; it is not a new tenant per test"
    )


def test_a_fresh_tenants_scoped_tables_start_empty_first(
    fresh_tenant: uuid.UUID, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Writes one department under this test's tenant; its empty-check sibling must
    NOT see it, catching a shared/reused tenant namespace that leaks rows.

    A department needs only the bound tenant and a name (``manager_user_id`` is
    nullable), so it plants under a bare fresh tenant with no user/role chain."""

    async def scenario() -> None:
        async with tenant_session(session_factory, fresh_tenant) as session:
            before = (await session.execute(select(Department))).scalars().all()
            assert before == [], "a fresh tenant must start with no departments"

            session.add(
                Department(
                    tenant_id=fresh_tenant,
                    name="fake dept for 57a",
                    manager_user_id=None,
                )
            )
            await session.commit()

    asyncio.run(scenario())


def test_a_fresh_tenants_scoped_tables_start_empty_second(
    fresh_tenant: uuid.UUID, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Catches a reused tenant namespace: if fresh_tenant were shared, the ticket
    the sibling committed would show up here; a per-test clean slate shows zero."""

    async def scenario() -> None:
        async with tenant_session(session_factory, fresh_tenant) as session:
            rows = (await session.execute(select(Department))).scalars().all()
            assert rows == [], "this test's tenant saw another test's department; not fresh"

    asyncio.run(scenario())


def test_the_shared_schema_has_all_twenty_one_tables(shared_engine: AsyncEngine) -> None:
    """Catches a shared engine whose schema is missing a table or carries an extra
    one: the exact-set compare against the 21 names flips on any difference. The
    test runs no create_all, so it only passes if the fixture built the schema."""

    async def scenario() -> None:
        async with shared_engine.connect() as conn:
            names = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
        assert len(ALL_TABLES) == 21, "ALL_TABLES should name exactly 21 tables"
        assert set(names) == set(ALL_TABLES)

    asyncio.run(scenario())


def test_an_unbound_tenant_query_still_raises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Catches a shared factory that dropped the tenant guard: a plain session with
    no tenant bound must still refuse an ORM query on a tenant-scoped model, not
    silently run a full-table read."""

    async def scenario() -> None:
        async with session_factory() as session:
            with pytest.raises(UnboundTenantQueryError):
                await session.execute(select(User))

    asyncio.run(scenario())
