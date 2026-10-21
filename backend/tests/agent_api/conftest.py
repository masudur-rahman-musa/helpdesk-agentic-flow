"""Shared fixtures for the agent-api suites that run a real chat turn."""

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from helpdesk.shared.core.db import create_session_factory
from helpdesk.shared.models import Base
from tests.conftest import TempDatabaseFactory


@dataclass(frozen=True)
class AgentDb:
    """A database holding the schema."""

    url: str
    factory: async_sessionmaker[AsyncSession]


@pytest.fixture(scope="session")
def agent_db(pg: TempDatabaseFactory) -> Iterator[AgentDb]:
    """One database for the run, with the schema.

    Its own database, not the shared one, so the chat suites' rows never meet
    another suite's. Each suite seeds its own tenant with its own slug.
    `NullPool` so the one engine works across every test's own `asyncio.run`.
    """
    url = pg.create()
    engine = create_async_engine(url, poolclass=NullPool)

    async def build() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(build())
    yield AgentDb(url=url, factory=create_session_factory(engine))
    asyncio.run(engine.dispose())
    pg.drop(url)
