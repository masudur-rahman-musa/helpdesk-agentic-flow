"""An earlier change acceptance: repository helpers are tenant-scoped by construction.

Expected surface (``helpdesk.core.repository``):

    class Repository[M]:  # generic over a tenant-scoped model
        def __init__(self, session: AsyncSession, model: type[M]) -> None: ...
        async def get(self, id: uuid.UUID) -> M | None
        async def list(self) -> Sequence[M]
        async def create(self, **fields: Any) -> M    # injects the bound tenant_id
        async def update(self, id: uuid.UUID, **fields: Any) -> M | None
        async def delete(self, id: uuid.UUID) -> bool

The session comes from ``helpdesk.core.db.tenant_session``; the repo never
takes tenant_id as an argument. Cross-tenant ids behave as not-found: ``get``
and ``update`` return None, ``delete`` returns False - never the row, never
a cross-tenant effect.
"""

import asyncio
import uuid

import pytest
from _seed import schema_engine, seed_tenant
from sqlalchemy import select

from helpdesk.shared.core.db import create_session_factory, tenant_session
from helpdesk.shared.core.repository import Repository
from helpdesk.shared.models import Ticket


def test_crud_within_bound_tenant(temp_db_url: str) -> None:
    async def scenario() -> None:
        async with schema_engine(temp_db_url) as engine:
            factory = create_session_factory(engine)
            seeded = await seed_tenant(factory, "repo-a")

            async with tenant_session(factory, seeded.tenant_id) as session:
                repo = Repository(session, Ticket)

                created = await repo.create(
                    user_id=seeded.user_id,
                    kind="general",
                    title="fake repo ticket",
                    body="fake body",
                    status="open",
                    session_id=None,
                )
                assert created.tenant_id == seeded.tenant_id, (
                    "create must stamp the bound tenant_id"
                )

                fetched = await repo.get(created.id)
                assert fetched is not None and fetched.id == created.id

                listed = {t.id for t in await repo.list()}
                assert listed == {seeded.ticket_id, created.id}

                updated = await repo.update(created.id, status="closed")
                assert updated is not None and updated.status == "closed"

                assert await repo.delete(created.id) is True
                assert await repo.get(created.id) is None

    asyncio.run(scenario())


@pytest.mark.parametrize("op", ["get", "update", "delete"])
def test_other_tenants_id_is_not_found(temp_db_url: str, op: str) -> None:
    """A repo bound to tenant B treats tenant A's id as nonexistent, with no side effect."""

    async def scenario() -> None:
        async with schema_engine(temp_db_url) as engine:
            factory = create_session_factory(engine)
            seeded_a = await seed_tenant(factory, "repo-x")
            seeded_b = await seed_tenant(factory, "repo-y")

            async with tenant_session(factory, seeded_b.tenant_id) as session:
                repo = Repository(session, Ticket)
                if op == "get":
                    assert await repo.get(seeded_a.ticket_id) is None
                elif op == "update":
                    assert await repo.update(seeded_a.ticket_id, status="closed") is None
                else:
                    assert await repo.delete(seeded_a.ticket_id) is False

            # Tenant A's row is untouched either way.
            async with tenant_session(factory, seeded_a.tenant_id) as session:
                row = (
                    (await session.execute(select(Ticket).where(Ticket.id == seeded_a.ticket_id)))
                    .scalars()
                    .one_or_none()
                )
                assert row is not None, "cross-tenant delete must not remove the row"
                assert row.status == "open", "cross-tenant update must not change the row"

    asyncio.run(scenario())


def test_list_returns_only_bound_tenant_rows(temp_db_url: str) -> None:
    async def scenario() -> None:
        async with schema_engine(temp_db_url) as engine:
            factory = create_session_factory(engine)
            seeded_a = await seed_tenant(factory, "repo-l1")
            seeded_b = await seed_tenant(factory, "repo-l2")

            async with tenant_session(factory, seeded_b.tenant_id) as session:
                repo = Repository(session, Ticket)
                ids = {t.id for t in await repo.list()}
                assert ids == {seeded_b.ticket_id}
                assert seeded_a.ticket_id not in ids

    asyncio.run(scenario())


@pytest.mark.parametrize("field", ["tenant_id", "id"])
@pytest.mark.parametrize("op", ["create", "update"])
def test_protected_fields_are_rejected(temp_db_url: str, op: str, field: str) -> None:
    """`tenant_id` and `id` come from the session/mixin, never from callers:
    accepting them would let an update move a row into another tenant behind
    the query guard (unit-of-work flush is not intercepted by do_orm_execute).
    """

    async def scenario() -> None:
        async with schema_engine(temp_db_url) as engine:
            factory = create_session_factory(engine)
            seeded = await seed_tenant(factory, f"repo-prot-{op}-{field}")

            async with tenant_session(factory, seeded.tenant_id) as session:
                repo = Repository(session, Ticket)
                rogue = {field: uuid.uuid4()}
                with pytest.raises(ValueError, match="protected"):
                    if op == "create":
                        await repo.create(
                            user_id=seeded.user_id,
                            kind="general",
                            title="fake",
                            body="fake",
                            status="open",
                            session_id=None,
                            **rogue,
                        )
                    else:
                        await repo.update(seeded.ticket_id, **rogue)

            # The seeded ticket is untouched and still in its tenant.
            async with tenant_session(factory, seeded.tenant_id) as session:
                repo = Repository(session, Ticket)
                row = await repo.get(seeded.ticket_id)
                assert row is not None and row.tenant_id == seeded.tenant_id

    asyncio.run(scenario())
