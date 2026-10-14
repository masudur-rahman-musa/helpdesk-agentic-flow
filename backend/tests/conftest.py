"""Shared backing-service fixtures: Postgres and Redis.

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
single ``asyncio.run`` call; every engine, connection and Redis client
lives and dies inside that one loop.
"""

import asyncio
import os
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from helpdesk.shared.core.config import load_settings

# The repo root, used as the cwd for the Alembic subprocess below. The import
# path for `scripts.seed` and `tests.support.*` is set once in
# `backend/pyproject.toml`'s `[tool.pytest.ini_options] pythonpath`,
# so no `sys.path` surgery lives here any more.
_REPO_ROOT = str(Path(__file__).resolve().parents[2])

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


# --- Redis ---------------------------------------------

# The killswitch tests flush the database they run against, so they must never
# run against the configured one. This index is dedicated to the test suite and
# the `redis_url` fixture asserts it differs from the configured index before
# issuing FLUSHDB.
TEST_REDIS_DB = 15
_FALLBACK_TEST_REDIS_DB = 14


# Under pytest-xdist each worker gets its own Redis index: this file's base
# minus 4 per worker. The four suites that flush Redis use bases 15, 12 and 10
# (the rate limit shares 15, as it always has), so three workers fit in Redis's
# 16 indexes with no two suites or workers on one index. A plain run is worker 0.
_MAX_REDIS_WORKERS = 3


def _worker_redis_index(base: int, fallback: int, configured: int) -> int:
    number = os.environ.get("PYTEST_XDIST_WORKER", "gw0").removeprefix("gw")
    worker = int(number) if number.isdigit() else 0
    if worker >= _MAX_REDIS_WORKERS:
        pytest.fail(
            f"pytest-xdist worker gw{worker}: at most {_MAX_REDIS_WORKERS} workers get "
            "their own Redis index. Run with -n 3 or fewer.",
            pytrace=False,
        )
    index = base - 4 * worker
    return index if index != configured else fallback - 4 * worker


_REDIS_UNREACHABLE_MSG = (
    "Redis is unreachable at the configured REDIS_URL.\n"
    "Plan 09b tests require the compose stack: run `docker compose up -d` first.\n"
    "Original error: {error!r}"
)


def _redis_url_for_db(url: str, index: int) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{index}", parts.query, parts.fragment))


_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ""})


def _assert_local(url: str) -> None:
    """Refuse to FLUSHDB anything that is not on this machine.

    "A different index from the configured one" is not the same as "safe": a
    developer whose REDIS_URL points at a shared Redis with /15 configured
    would have the fallback index flushed instead, and those keys are just as
    live. Host is the check that actually holds.
    """
    host = urlsplit(url).hostname or ""
    if host not in _LOCAL_HOSTS:
        pytest.fail(
            f"REDIS_URL points at {host!r}, not this machine. The killswitch tests "
            "FLUSHDB the database they run against and will not do that to a "
            "remote or shared Redis. Point REDIS_URL at the compose stack.",
            pytrace=False,
        )


def _redis_db_index(url: str) -> int:
    """The db index the configured URL points at (Redis defaults to 0 when absent)."""
    path = urlsplit(url).path.lstrip("/")
    return int(path) if path.isdigit() else 0


async def _redis_flushdb(url: str) -> None:
    client = Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
    try:
        await client.flushdb()
    finally:
        await client.aclose()


@pytest.fixture(scope="session")
def redis_base_url() -> str:
    """Connectivity gate: fails loudly (never skips) when Redis is down.

    A killswitch that silently went untested would be a governance hole, so a
    missing Redis is a failure, not a skip.
    """
    try:
        settings = load_settings()
    except ValidationError as exc:
        pytest.fail(
            f"REDIS_URL is not configured (env or .env). Copy .env.example to .env.\n{exc}",
            pytrace=False,
        )
    _assert_local(settings.redis_url)
    configured = _redis_db_index(settings.redis_url)
    test_index = _worker_redis_index(TEST_REDIS_DB, _FALLBACK_TEST_REDIS_DB, configured)
    url = _redis_url_for_db(settings.redis_url, test_index)
    try:
        asyncio.run(_redis_flushdb(url))
    except Exception as exc:
        pytest.fail(_REDIS_UNREACHABLE_MSG.format(error=exc), pytrace=False)
    return url


@pytest.fixture
def redis_url(redis_base_url: str) -> str:
    """A flushed, dedicated Redis database index for one test.

    Re-asserts the isolation guarantee per test rather than trusting the
    session fixture: the cost of being wrong once is flushing live keys.
    """
    settings = load_settings()
    configured = _redis_db_index(settings.redis_url)
    index = _redis_db_index(redis_base_url)
    assert index != configured, (
        f"the test Redis db index ({index}) must differ from the configured one "
        f"({configured}); refusing to FLUSHDB"
    )
    asyncio.run(_redis_flushdb(redis_base_url))
    return redis_base_url


# --- The restricted application DB role ---------------------------

# An earlier change decision C: the role name is a fixed constant, never configuration,
# so there is no identifier to interpolate from an untrusted source. The literal
# is repeated in `test_audit_db_role.py::ROLE_NAME`, which is what pins it.
RESTRICTED_DB_ROLE = "helpdesk_app"


def _alembic_upgrade_head(database_url: str) -> None:
    """Migrate one throwaway database to head as the OWNER."""
    result = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=_REPO_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"alembic upgrade head failed:\n{result.stderr}"


@pytest.fixture(scope="session")
def migrated_db_url(pg: TempDatabaseFactory) -> Iterator[str]:
    """One throwaway database, migrated to head once, for the whole run.

    Built once and shared: isolation inside it comes from a
    fresh tenant row per test, never from a second database. `temp_db_url` is
    not usable here because it carries a `Base.metadata.create_all` schema and
    no migration ever runs against it, so it has no roles and no grants.
    """
    url = pg.create()
    _alembic_upgrade_head(url)
    yield url
    pg.drop(url)


@pytest.fixture(scope="session")
def restricted_db_password() -> str:
    """A throwaway password generated per run, never a secret and never from env.

    a project rule keeps secrets out of tests and fixtures; the migration creates
    `helpdesk_app` NOLOGIN precisely so that no password lives in the schema.
    A random hex value is not a credential to anything outside this run.
    """
    return f"t{uuid.uuid4().hex}"


@pytest.fixture
def restricted_db_url(migrated_db_url: str, restricted_db_password: str) -> str:
    """The migrated database's URL, connecting as `helpdesk_app` instead of the owner.

    The migration creates the role NOLOGIN, so nothing can connect as it until
    the owner grants LOGIN and a password here. Roles are CLUSTER-GLOBAL, not
    per database: this ALTER is visible to every database on the compose
    Postgres, which is safe only because the suite runs one test run at a time. It is re-applied per test rather than once per session
    because the migration round-trip case downgrades to base, which may drop
    the role and take the LOGIN with it.

    The database is the SAME one `migrated_db_url` built; only the user and the
    password in the URL change.
    """
    assert restricted_db_password.isalnum(), (
        "the generated password must be alphanumeric; it is interpolated into "
        "an SQL literal that takes no escaping"
    )
    asyncio.run(
        _admin_execute(
            migrated_db_url,
            f"ALTER ROLE \"{RESTRICTED_DB_ROLE}\" LOGIN PASSWORD '{restricted_db_password}'",
        )
    )
    return (
        make_url(migrated_db_url)
        .set(username=RESTRICTED_DB_ROLE, password=restricted_db_password)
        .render_as_string(hide_password=False)
    )
