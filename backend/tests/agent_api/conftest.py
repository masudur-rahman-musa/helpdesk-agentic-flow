"""Shared fixtures for the agent-api suites that run a real chat turn."""

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from helpdesk.agent_api.agent.checkpointer import build_pool, psycopg_dsn, setup_checkpointer
from helpdesk.shared.core.db import create_session_factory
from helpdesk.shared.models import Base
from tests.conftest import TempDatabaseFactory


@dataclass(frozen=True)
class AgentDb:
    """A database holding the schema and the checkpoint tables."""

    url: str
    factory: async_sessionmaker[AsyncSession]


@pytest.fixture(scope="session")
def agent_db(pg: TempDatabaseFactory) -> Iterator[AgentDb]:
    """One database for the run, with the schema and the checkpoint tables.

    Its own database, not the shared one, because the checkpoint tables are
    not in `Base.metadata`. Each suite seeds its own tenant with its own slug.
    `NullPool` so the one engine works across every test's own `asyncio.run`.
    """
    url = pg.create()
    engine = create_async_engine(url, poolclass=NullPool)

    async def build() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        pool = build_pool(psycopg_dsn(url))
        async with pool:
            await setup_checkpointer(pool)

    asyncio.run(build())
    yield AgentDb(url=url, factory=create_session_factory(engine))
    asyncio.run(engine.dispose())
    pg.drop(url)
