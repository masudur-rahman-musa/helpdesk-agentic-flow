"""An earlier change section 6: the audit writer against Postgres.

Expected implementation surface:

``helpdesk.audit.AuditEventInput`` (re-exported from ``helpdesk.audit.writer``)
    Pydantic input for one event: ``event_type``, ``actor_kind``,
    ``actor_ref``, ``session_id``, ``trace_id``, ``subject_type``,
    ``subject_id``, ``payload``, and an optional ``occurred_at`` (defaults to
    now). It carries no ``tenant_id``, ``seq``, ``prev_hash`` or ``hash``:
    the chain fields belong to the writer, the tenant to the caller's
    identity.

``helpdesk.audit.writer.append_event(session, tenant_id, event) -> AuditEvent``
    Takes a per-tenant transaction-scoped advisory lock,
    reads that tenant's last hash (``GENESIS_HASH`` when there is none),
    computes ``hash``, inserts. The caller's transaction commits it.

``helpdesk.audit.verify.verify_tenant_chain(session, tenant_id) -> VerifyResult``
    Loads the tenant's events ordered by ``seq`` and runs
    ``chain.verify_chain`` over them.

Style follows ``test_models.py``: the schema comes from ``Base.metadata`` via
``_seed.schema_engine`` on the session-scoped throwaway database, and each
scenario runs inside a single ``asyncio.run``.
"""

import asyncio
import uuid
from collections.abc import Sequence
from datetime import datetime
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from helpdesk.shared.audit import AuditEventInput
from helpdesk.shared.audit.chain import GENESIS_HASH
from helpdesk.shared.audit.verify import verify_tenant_chain
from helpdesk.shared.audit.writer import TenantMismatchError, append_event
from helpdesk.shared.core.db import create_session_factory, tenant_session
from helpdesk.shared.models import AuditEvent, Tenant
from tests.support.seed import schema_engine


def _event(index: int) -> AuditEventInput:
    """One obviously fake event; only the trace id varies between events."""
    return AuditEventInput(
        occurred_at=None,
        actor_kind="system",
        actor_ref=str(uuid.UUID(int=index)),
        session_id=None,
        trace_id=f"trace-fake-{index}",
        event_type="ticket.created",
        subject_type="ticket",
        subject_id=str(uuid.UUID(int=1000 + index)),
        payload={"fake": True, "index": index},
    )


async def _make_tenant(factory: async_sessionmaker[AsyncSession], slug: str) -> uuid.UUID:
    async with factory() as session:
        tenant = Tenant(slug=slug, name=f"Fake Tenant {slug}", residency="eu", settings={})
        session.add(tenant)
        await session.commit()
        return tenant.id


async def _append(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, index: int
) -> None:
    """One append in its own transaction, so the advisory lock is really contended."""
    async with tenant_session(factory, tenant_id) as session:
        await append_event(session, tenant_id, _event(index))


async def _rows(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> Sequence[AuditEvent]:
    async with tenant_session(factory, tenant_id) as session:
        result = await session.execute(select(AuditEvent).order_by(AuditEvent.seq))
        return list(result.scalars().all())


def _assert_links(rows: Sequence[AuditEvent]) -> None:
    """Genesis start, every prev_hash the previous hash, no two links alike."""
    assert rows, "expected at least one event"
    assert rows[0].prev_hash == GENESIS_HASH, "a tenant's first event starts from genesis"
    for previous, current in pairwise(rows):
        assert current.prev_hash == previous.hash, (
            f"seq {current.seq} does not link to seq {previous.seq}"
        )
    prev_hashes = [row.prev_hash for row in rows]
    assert len(set(prev_hashes)) == len(prev_hashes), (
        f"two events share a prev_hash — a fork, not a chain: {prev_hashes}"
    )
    hashes = [row.hash for row in rows]
    assert len(set(hashes)) == len(hashes), f"duplicate hashes: {hashes}"


def test_sequential_appends_produce_a_verifiable_chain(temp_db_url: str) -> None:
    async def scenario() -> None:
        async with schema_engine(temp_db_url) as engine:
            factory = create_session_factory(engine)
            tenant_id = await _make_tenant(factory, "audit-seq")

            async with tenant_session(factory, tenant_id) as session:
                first = await append_event(session, tenant_id, _event(1))
                assert first.tenant_id == tenant_id
                assert first.prev_hash == GENESIS_HASH
                assert len(first.hash) == 64

            for index in range(2, 6):
                await _append(factory, tenant_id, index)

            rows = await _rows(factory, tenant_id)
            assert len(rows) == 5
            assert [row.seq for row in rows] == sorted(row.seq for row in rows)
            _assert_links(rows)

            async with tenant_session(factory, tenant_id) as session:
                result = await verify_tenant_chain(session, tenant_id)
            assert result.ok is True, f"chain should verify, broken at {result.broken_at}"
            assert result.broken_at is None

    asyncio.run(scenario())


def test_empty_tenant_chain_verifies_ok(temp_db_url: str) -> None:
    async def scenario() -> None:
        async with schema_engine(temp_db_url) as engine:
            factory = create_session_factory(engine)
            tenant_id = await _make_tenant(factory, "audit-empty")

            async with tenant_session(factory, tenant_id) as session:
                result = await verify_tenant_chain(session, tenant_id)
            assert result.ok is True
            assert result.broken_at is None

    asyncio.run(scenario())


def test_interleaved_appends_keep_two_tenant_chains_independent(temp_db_url: str) -> None:
    async def scenario() -> None:
        async with schema_engine(temp_db_url) as engine:
            factory = create_session_factory(engine)
            tenant_a = await _make_tenant(factory, "audit-a")
            tenant_b = await _make_tenant(factory, "audit-b")

            for index in range(1, 4):
                await _append(factory, tenant_a, index)
                await _append(factory, tenant_b, 100 + index)

            rows_a = await _rows(factory, tenant_a)
            rows_b = await _rows(factory, tenant_b)
            assert len(rows_a) == 3 and len(rows_b) == 3
            _assert_links(rows_a)
            _assert_links(rows_b)

            hashes_a = {row.hash for row in rows_a}
            hashes_b = {row.hash for row in rows_b}
            assert hashes_a.isdisjoint(hashes_b)
            links_b = {row.prev_hash for row in rows_b} - {GENESIS_HASH}
            assert links_b.isdisjoint(hashes_a), "tenant B links into tenant A's chain"

            for tenant_id in (tenant_a, tenant_b):
                async with tenant_session(factory, tenant_id) as session:
                    result = await verify_tenant_chain(session, tenant_id)
                assert result.ok is True, f"{tenant_id} broken at {result.broken_at}"

    asyncio.run(scenario())


def test_an_event_timestamped_in_another_zone_still_verifies(temp_db_url: str) -> None:
    """Postgres returns UTC, so the writer must hash UTC or verify goes false."""

    async def scenario() -> None:
        async with schema_engine(temp_db_url) as engine:
            factory = create_session_factory(engine)
            tenant_id = await _make_tenant(factory, "audit-tz")

            event = _event(1)
            event.occurred_at = datetime.now(ZoneInfo("Asia/Dhaka"))
            async with tenant_session(factory, tenant_id) as session:
                await append_event(session, tenant_id, event)

            async with tenant_session(factory, tenant_id) as session:
                result = await verify_tenant_chain(session, tenant_id)
            assert result.ok is True, f"non-UTC timestamp broke the chain at {result.broken_at}"

    asyncio.run(scenario())


def test_relabelling_a_chain_onto_another_tenant_is_detected(temp_db_url: str) -> None:
    """The tenant is inside the digest, so a moved chain no longer recomputes."""

    async def scenario() -> None:
        async with schema_engine(temp_db_url) as engine:
            factory = create_session_factory(engine)
            tenant_a = await _make_tenant(factory, "audit-relabel-a")
            tenant_b = await _make_tenant(factory, "audit-relabel-b")

            for index in range(1, 4):
                await _append(factory, tenant_a, index)

            # The insider move: A's rows become B's, chain links untouched.
            async with factory() as session:
                await session.execute(
                    text("UPDATE audit_events SET tenant_id = :b WHERE tenant_id = :a"),
                    {"a": tenant_a, "b": tenant_b},
                )
                await session.commit()

            async with tenant_session(factory, tenant_b) as session:
                result = await verify_tenant_chain(session, tenant_b)
            assert result.ok is False, "a chain moved between tenants must not verify"

    asyncio.run(scenario())


def test_appending_for_a_tenant_the_session_is_not_bound_to_is_refused(temp_db_url: str) -> None:
    """Identity comes from the session, never from the argument."""

    async def scenario() -> None:
        async with schema_engine(temp_db_url) as engine:
            factory = create_session_factory(engine)
            tenant_a = await _make_tenant(factory, "audit-bound-a")
            tenant_b = await _make_tenant(factory, "audit-bound-b")

            with pytest.raises(TenantMismatchError):
                async with tenant_session(factory, tenant_a) as session:
                    await append_event(session, tenant_b, _event(1))

            assert await _rows(factory, tenant_b) == []

    asyncio.run(scenario())


def test_concurrent_same_tenant_appends_produce_an_unbroken_chain(temp_db_url: str) -> None:
    """Ten appends racing on one tenant: no fork, no duplicate prev_hash."""

    async def scenario() -> None:
        async with schema_engine(temp_db_url) as engine:
            factory = create_session_factory(engine)
            tenant_id = await _make_tenant(factory, "audit-race")

            results: list[Any] = await asyncio.gather(
                *(_append(factory, tenant_id, index) for index in range(1, 11)),
                return_exceptions=True,
            )
            failures = [r for r in results if isinstance(r, BaseException)]
            assert not failures, f"concurrent appends raised: {failures!r}"

            rows = await _rows(factory, tenant_id)
            assert len(rows) == 10, f"expected 10 events, got {len(rows)}"
            _assert_links(rows)

            async with tenant_session(factory, tenant_id) as session:
                result = await verify_tenant_chain(session, tenant_id)
            assert result.ok is True, f"chain broken at seq {result.broken_at}"

    asyncio.run(scenario())


def test_concurrent_appends_across_tenants_do_not_block_or_cross(temp_db_url: str) -> None:
    """The advisory lock is per tenant; two tenants racing keep separate chains."""

    async def scenario() -> None:
        async with schema_engine(temp_db_url) as engine:
            factory = create_session_factory(engine)
            tenant_a = await _make_tenant(factory, "audit-race-a")
            tenant_b = await _make_tenant(factory, "audit-race-b")

            coros = [_append(factory, tenant_a, i) for i in range(1, 6)]
            coros += [_append(factory, tenant_b, 100 + i) for i in range(1, 6)]
            await asyncio.gather(*coros)

            for tenant_id in (tenant_a, tenant_b):
                rows = await _rows(factory, tenant_id)
                assert len(rows) == 5
                _assert_links(rows)
                async with tenant_session(factory, tenant_id) as session:
                    result = await verify_tenant_chain(session, tenant_id)
                assert result.ok is True, f"{tenant_id} broken at {result.broken_at}"

    asyncio.run(scenario())
