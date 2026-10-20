"""The gauge of chats left waiting on a question.

Written before the implementation exists. Surface under test:

``helpdesk.shared.core.observability.metrics``
    ``CHATS_WAITING = "helpdesk_chats_waiting"``, a gauge with exactly the label
    ``tenant``, installed by ``install_sweep_gauges`` as
    ``SweepGauges.chats_waiting``; ``record_chats_waiting(*, tenant, count)``
    SETS it, never adds.
``helpdesk.scheduler.jobs.metrics_sweep.refresh_waiting_chats_gauge(session, *, now)``
    counts the bound tenant's chats whose open question (``asked_at``) or
    clarifying ask (``asked_clarifying_at``) was asked 30 minutes or more and
    less than 24 hours before ``now``, and records it under that tenant.
    Thirty minutes is in, twenty-four hours is out (after 24 hours the question
    is dropped, so the chat is no longer waiting on it).
The query lives in ``shared/crud/sessions.py``.

Cost: the count case is `db`, because only Postgres shows that the JSONB time
filter picks the right rows. It takes the run's shared session factory and
plants its own tenants, so nothing else is rebuilt and no other test sees its
rows. The label case is a plain registry.
"""

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from prometheus_client import CollectorRegistry
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from helpdesk.agent_api.agent.confirmation import OpenQuestion
from helpdesk.agent_api.agent.state import (
    Intent,
    IntentEntities,
    IntentExtraction,
    ResolvedEntities,
    ValidatedCall,
)
from helpdesk.scheduler.jobs.metrics_sweep import refresh_waiting_chats_gauge
from helpdesk.shared.core.db import tenant_session
from helpdesk.shared.core.observability import metrics
from helpdesk.shared.crud.sessions import (
    get_or_create_session,
    set_asked_clarifying,
    set_open_question,
)
from tests.support.seed import SeededTenant, seed_tenant

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
"""The one instant every case counts at. Passed in, never read from a clock."""

CHATS_WAITING = "helpdesk_chats_waiting"


@pytest.fixture
def sweep_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[CollectorRegistry]:
    """A registry of this test's own, with the sweep gauges installed on it."""
    registry = CollectorRegistry()
    monkeypatch.setattr(metrics, "SWEEP_GAUGES", metrics.install_sweep_gauges(registry))
    yield registry


def _open_question(asked_at: datetime) -> dict[str, Any]:
    """A well-formed stored "I'll file this" question, asked at `asked_at`."""
    question = OpenQuestion(
        asked_at=asked_at,
        message="fake message",
        calls=(ValidatedCall(tool="create_access_request", arguments={}),),
        extraction=IntentExtraction(
            intent=Intent.REQUEST_ACCESS,
            entities=IntentEntities(
                system="wiki",
                role=None,
                target_user=None,
                department=None,
                expiry_days=None,
                justification=None,
            ),
            confidence=0.9,
            clarifying_question=None,
            second_request=None,
        ),
        resolved=ResolvedEntities(),
    )
    return question.model_dump(mode="json")


def _plant(
    factory: async_sessionmaker[AsyncSession],
    tenant: SeededTenant,
    *,
    asked_ago: timedelta,
    as_open_question: bool,
) -> uuid.UUID:
    """One chat of the tenant's seeded user, with one stored question."""
    chat_id = uuid.uuid4()
    asked_at = NOW - asked_ago

    async def scenario() -> None:
        async with tenant_session(factory, tenant.tenant_id) as session:
            await get_or_create_session(
                session, session_id=chat_id, user_id=tenant.user_id, now=asked_at
            )
            if as_open_question:
                await set_open_question(session, session_id=chat_id, value=_open_question(asked_at))
            else:
                await set_asked_clarifying(session, session_id=chat_id, value=asked_at.isoformat())

    asyncio.run(scenario())
    return chat_id


def _clear(
    factory: async_sessionmaker[AsyncSession], tenant: SeededTenant, chat_id: uuid.UUID
) -> None:
    async def scenario() -> None:
        async with tenant_session(factory, tenant.tenant_id) as session:
            await set_open_question(session, session_id=chat_id, value=None)
            await set_asked_clarifying(session, session_id=chat_id, value=None)

    asyncio.run(scenario())


def _refresh(
    factory: async_sessionmaker[AsyncSession], tenant: SeededTenant, *, now: datetime = NOW
) -> None:
    async def scenario() -> None:
        async with tenant_session(factory, tenant.tenant_id) as session:
            await refresh_waiting_chats_gauge(session, now=now)

    asyncio.run(scenario())


def _gauge(registry: CollectorRegistry, tenant: SeededTenant) -> float | None:
    return registry.get_sample_value(CHATS_WAITING, {"tenant": str(tenant.tenant_id)})


def _tenant(factory: async_sessionmaker[AsyncSession]) -> SeededTenant:
    return asyncio.run(seed_tenant(factory, f"waiting-{uuid.uuid4().hex[:8]}"))


@pytest.mark.db
def test_waiting_chats_are_counted_from_the_chat_rows(
    session_factory: async_sessionmaker[AsyncSession],
    sweep_registry: CollectorRegistry,
) -> None:
    """Catches a count with the wrong window or the wrong rows.

    Four chats ask at 29m59s, 30m, 23h59m and 24h ago. Only the 30m and the
    23h59m ones count, so an exclusive lower bound (`> 30m`), an inclusive
    upper bound (`<= 24h`) and a missing lower bound (every recent question)
    each give a different number than 2. The two kinds of stored question are
    alternated across the four rows, so a count that reads only one of the two
    keys sees 1.

    A row of ANOTHER tenant, asked 1 hour ago, sits in the same table: the
    gauge for the bound tenant stays 2 and the other tenant gets its own 1
    only when its own sweep runs. After the questions are answered (both keys
    dropped) the next sweep sets 0, not the old 2: the gauge is set, not left.
    """
    tenant = _tenant(session_factory)
    other = _tenant(session_factory)
    answered = [
        _plant(
            session_factory,
            tenant,
            asked_ago=timedelta(minutes=29, seconds=59),
            as_open_question=False,
        ),
        _plant(
            session_factory,
            tenant,
            asked_ago=timedelta(minutes=30),
            as_open_question=True,
        ),
        _plant(
            session_factory,
            tenant,
            asked_ago=timedelta(hours=23, minutes=59),
            as_open_question=False,
        ),
        _plant(
            session_factory,
            tenant,
            asked_ago=timedelta(hours=24),
            as_open_question=True,
        ),
    ]
    _plant(session_factory, other, asked_ago=timedelta(hours=1), as_open_question=True)

    _refresh(session_factory, tenant)

    assert _gauge(sweep_registry, tenant) == 2.0
    assert _gauge(sweep_registry, other) is None

    _refresh(session_factory, other)

    assert _gauge(sweep_registry, other) == 1.0
    assert _gauge(sweep_registry, tenant) == 2.0

    for chat_id in answered:
        _clear(session_factory, tenant, chat_id)
    _refresh(session_factory, tenant)

    assert _gauge(sweep_registry, tenant) == 0.0


def test_the_waiting_gauge_is_labelled_by_tenant_only(sweep_registry: CollectorRegistry) -> None:
    """Catches a label set that is not exactly `tenant`: an extra label (a user
    or a chat id) would put a person in the unauthenticated scrape, and no label
    would merge every tenant into one series.

    Driven through `.labels()` so it fails on what a call site gets, and through
    `record_chats_waiting` to pin that it SETS: 3 then 5 reads 5, not 8.
    """
    gauges = metrics.install_sweep_gauges(sweep_registry)
    chats_waiting: Any = gauges.chats_waiting

    with pytest.raises(ValueError):
        chats_waiting.labels(tenant="fake-tenant", unexpected="fake-value")
    with pytest.raises(ValueError):
        chats_waiting.labels()
    with pytest.raises(ValueError):
        chats_waiting.labels(user="fake-user")

    metrics.record_chats_waiting(tenant="fake-tenant", count=3)
    metrics.record_chats_waiting(tenant="fake-tenant", count=5)

    assert sweep_registry.get_sample_value(CHATS_WAITING, {"tenant": "fake-tenant"}) == 5.0
    assert gauges.chats_waiting is not gauges.grants_active
