"""Shared DB fixtures for an earlier change (models-repo) tests.

These fixtures talk to the compose Postgres using the URL from
``helpdesk.core.settings.load_settings()`` (env / .env). They create a
throwaway database and drop it afterwards, so the real ``helpdesk``
database is never touched.

If Postgres is unreachable the ``pg`` fixture FAILS the tests loudly with
an explicit message. There is no silent skip: a green run must mean the
isolation guarantees were actually proven against Postgres.

This conftest deliberately imports nothing from ``helpdesk.models`` or
``helpdesk.core.db`` so that, before the implementation exists, the
collection errors point at the test modules that name the missing API.

No event-loop plugin is installed (pytest-asyncio is not a dependency),
so tests are synchronous functions that drive their async scenario with a
single ``asyncio.run`` call; every engine and connection lives and dies
inside that one loop.
"""

import asyncio
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from helpdesk.core.settings import load_settings
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

# An earlier change: make `scripts/` at the repo root importable (`from scripts.seed import ...`).
# Conftest runs before any test module import, so this is safe here.
_REPO_ROOT = str(Path(__file__).resolve().parents[3])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_UNREACHABLE_MSG = (
    "Postgres is unreachable at the configured DATABASE_URL.\n"
    "Plan 03 tests require the compose stack: run `docker compose up -d` first.\n"
    "Original error: {error!r}"
)


async def _admin_execute(admin_url: str, sql: str) -> None:
    """Run one statement outside a transaction (CREATE/DROP DATABASE need that)."""
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


def _url_for_database(admin_url: str, database: str) -> str:
    return make_url(admin_url).set(database=database).render_as_string(hide_password=False)


class TempDatabaseFactory:
    """Creates uniquely named temp databases on the compose Postgres, drops them on request."""

    def __init__(self, admin_url: str) -> None:
        self._admin_url = admin_url

    def create(self) -> str:
        name = f"helpdesk_test_{uuid.uuid4().hex[:12]}"
        asyncio.run(_admin_execute(self._admin_url, f'CREATE DATABASE "{name}"'))
        return _url_for_database(self._admin_url, name)

    def drop(self, url: str) -> None:
        name = make_url(url).database
        assert name is not None and name.startswith("helpdesk_test_")
        asyncio.run(_admin_execute(self._admin_url, f'DROP DATABASE "{name}" WITH (FORCE)'))


@pytest.fixture(scope="session")
def pg() -> TempDatabaseFactory:
    """Connectivity gate: fails loudly (never skips) when Postgres is down."""
    try:
        settings = load_settings()
    except ValidationError as exc:
        pytest.fail(
            f"DATABASE_URL is not configured (env or .env). Copy .env.example to .env.\n{exc}",
            pytrace=False,
        )
    try:
        asyncio.run(_admin_execute(settings.database_url, "SELECT 1"))
    except Exception as exc:
        pytest.fail(_UNREACHABLE_MSG.format(error=exc), pytrace=False)
    return TempDatabaseFactory(settings.database_url)


@pytest.fixture(scope="session")
def temp_db_url(pg: TempDatabaseFactory) -> Iterator[str]:
    """One throwaway database for the whole run; ORM tests create/drop the schema per test."""
    url = pg.create()
    yield url
    pg.drop(url)


@pytest.fixture
def fresh_db_url(pg: TempDatabaseFactory) -> Iterator[str]:
    """A pristine database for tests that must start from empty (Alembic round-trip)."""
    url = pg.create()
    yield url
    pg.drop(url)
