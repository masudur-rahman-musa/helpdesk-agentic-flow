"""An earlier change acceptance: hard tenant isolation at the session layer.

Two tenants seeded inline. A session bound to tenant B must not be able to
read, update, or delete tenant A's rows through any ORM statement - not just
through the repository helpers. An ORM query on a tenant-scoped model with
NO tenant bound must raise ``helpdesk.core.db.UnboundTenantQueryError``
immediately (guard error, never a silent full-table read). The guard applies
only to tenant-scoped models: querying ``Tenant`` unbound stays legal.
"""

import asyncio
import uuid
from typing import Any, cast

import pytest
from _seed import schema_engine, seed_tenant
from helpdesk.core.db import (
    UnboundTenantQueryError,
    create_session_factory,
    tenant_session,
)
from helpdesk.models import Grant, Role, Tenant, Ticket, User
from sqlalchemy import CursorResult, delete, select, update

# (model, attribute on SeededTenant holding that tenant's row id)
SCOPED_CASES: list[tuple[type[Any], str]] = [
    (User, "user_id"),
    (Role, "role_id"),
    (Grant, "grant_id"),
    (Ticket, "ticket_id"),
]


@pytest.mark.parametrize(("model", "id_attr"), SCOPED_CASES)
def test_session_bound_to_b_cannot_read_a_rows(
    temp_db_url: str, model: type[Any], id_attr: str
) -> None:
    async def scenario() -> None:
        async with schema_engine(temp_db_url) as engine:
            factory = create_session_factory(engine)
            seeded_a = await seed_tenant(factory, "iso-read-a")
            seeded_b = await seed_tenant(factory, "iso-read-b")
            a_id: uuid.UUID = getattr(seeded_a, id_attr)

            async with tenant_session(factory, seeded_b.tenant_id) as session:
                rows = (await session.execute(select(model))).scalars().all()
                ids = {row.id for row in rows}
                assert a_id not in ids, f"{model.__name__}: tenant A row leaked to B"
                assert all(row.tenant_id == seeded_b.tenant_id for row in rows)

                targeted = (
                    (await session.execute(select(model).where(model.id == a_id)))
                    .scalars()
                    .one_or_none()
                )
                assert targeted is None, f"{model.__name__}: direct id lookup crossed tenants"

    asyncio.run(scenario())


def test_session_bound_to_b_cannot_update_a_rows(temp_db_url: str) -> None:
    async def scenario() -> None:
        async with schema_engine(temp_db_url) as engine:
            factory = create_session_factory(engine)
            seeded_a = await seed_tenant(factory, "iso-upd-a")
            seeded_b = await seed_tenant(factory, "iso-upd-b")

            async with tenant_session(factory, seeded_b.tenant_id) as session:
                # cast is typing-only: at runtime a DML execute returns a CursorResult
                result = cast(
                    "CursorResult[Any]",
                    await session.execute(
                        update(User)
                        .where(User.id == seeded_a.user_id)
                        .values(display_name="hijacked")
                    ),
                )
                assert result.rowcount == 0, "cross-tenant UPDATE affected a row"

            async with tenant_session(factory, seeded_a.tenant_id) as session:
                row = (
                    (await session.execute(select(User).where(User.id == seeded_a.user_id)))
                    .scalars()
                    .one()
                )
                assert row.display_name == "Fake Person (iso-upd-a)"

    asyncio.run(scenario())


def test_session_bound_to_b_cannot_delete_a_rows(temp_db_url: str) -> None:
    async def scenario() -> None:
        async with schema_engine(temp_db_url) as engine:
            factory = create_session_factory(engine)
            seeded_a = await seed_tenant(factory, "iso-del-a")
            seeded_b = await seed_tenant(factory, "iso-del-b")

            async with tenant_session(factory, seeded_b.tenant_id) as session:
                # cast is typing-only: at runtime a DML execute returns a CursorResult
                result = cast(
                    "CursorResult[Any]",
                    await session.execute(delete(Ticket).where(Ticket.id == seeded_a.ticket_id)),
                )
                assert result.rowcount == 0, "cross-tenant DELETE affected a row"

            async with tenant_session(factory, seeded_a.tenant_id) as session:
                row = (
                    (await session.execute(select(Ticket).where(Ticket.id == seeded_a.ticket_id)))
                    .scalars()
                    .one_or_none()
                )
                assert row is not None, "tenant A's ticket was deleted by tenant B's session"

    asyncio.run(scenario())


def test_unbound_query_on_tenant_scoped_model_raises(temp_db_url: str) -> None:
    """No tenant bound -> guard error, not a silent full-table read.

    Covers the plan's risk note: a bare ``session.execute(select(...))`` on a
    plain (unbound) session must also hit the guard, both for full-entity and
    single-column selects.
    """

    async def scenario() -> None:
        async with schema_engine(temp_db_url) as engine:
            factory = create_session_factory(engine)
            await seed_tenant(factory, "iso-unbound")

            async with factory() as session:
                with pytest.raises(UnboundTenantQueryError):
                    await session.execute(select(User))
                with pytest.raises(UnboundTenantQueryError):
                    await session.execute(select(Ticket.id))

    asyncio.run(scenario())


def test_unbound_query_on_non_scoped_model_is_allowed(temp_db_url: str) -> None:
    """Boundary: the guard covers tenant-scoped models only; Tenant itself is queryable."""

    async def scenario() -> None:
        async with schema_engine(temp_db_url) as engine:
            factory = create_session_factory(engine)
            seeded = await seed_tenant(factory, "iso-tenant-ok")

            async with factory() as session:
                rows = (await session.execute(select(Tenant))).scalars().all()
                assert seeded.tenant_id in {row.id for row in rows}

    asyncio.run(scenario())


def test_the_engine_carries_the_pinned_connect_timeout() -> None:
    """Every engine in the project comes from `create_engine`, so a blackholed
    Postgres has exactly one place it can be stopped from hanging a request.

    Asserted on the engine that is actually built, at the exact value: a
    timeout that is merely "set" says nothing about whether it is 10 seconds
    or 10 minutes.
    """
    from helpdesk.core import db

    captured: dict[str, Any] = {}

    def fake_create_async_engine(url: str, **kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(db, "create_async_engine", fake_create_async_engine)
        db.create_engine("postgresql+asyncpg://u:p@127.0.0.1:9/db")

    assert db.CONNECT_TIMEOUT_SECONDS == 10
    assert captured["connect_args"] == {"timeout": 10}
