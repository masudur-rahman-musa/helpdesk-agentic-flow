"""Shared backing-service fixtures for the mcp-server suite.

A plain support module (``tests/support/mcp.py``). ``tests/conftest.py`` loads
it ONCE as a pytest plugin (``pytest_plugins``), so each session fixture below is
built once per run. Test files import its plain helpers by package path
(``tests.support.mcp``) but never its fixtures: pytest builds a session fixture
imported into a test module once per module, which made the seeded database
up to 38 times. Pinned by ``tests/support/test_fixture_builds.py``. The fixtures
are not autouse; a test asks for one by parameter name.

The expensive things are built exactly once for the whole
session — one throwaway Postgres database, created and seeded through the real
``scripts.seed.seed_all`` (argon2 makes seeding the slow part of the run), and
one Redis database index dedicated to this suite. Per-test isolation is a
namespace, not a rebuild: the Redis index is flushed per test and the database
is shared. Building the ASGI app is in-process and cheap, so ``create_app()``
is still called per test — the SDK's ``StreamableHTTPSessionManager`` refuses a
second ``run()`` on one instance, and each test drives its own ``asyncio.run``.

Postgres or Redis unreachable FAILS the run loudly. There is no skip: a green
run on this suite must mean the pipeline was proven against real backing
services, not that they were absent.

Every identity here is obviously fake (``*.invalid`` addresses, "Grants Probe")
and the one password hash placeholder is a literal marker, not a real hash.
"""

import asyncio
import dataclasses
import os
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

import httpx
import httpx2
import mcp.types as mcp_types
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import ValidationError
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis
from sqlalchemy import insert, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from starlette.routing import Mount

from helpdesk.mcp_server.services.rag.chunking import Chunk
from helpdesk.mcp_server.services.rag.chunks import write_chunks
from helpdesk.mcp_server.services.rag.embedding import DENSE_DIMENSION, SparseVector
from helpdesk.mcp_server.services.rag.store import register_document_version
from helpdesk.mcp_server.services.rag.vectors import (
    collection_name,
    ensure_collection,
    point_id,
    upsert_chunks,
)
from helpdesk.shared.auth.jwt import issue_token
from helpdesk.shared.core.config import load_settings
from helpdesk.shared.core.db import create_engine, create_session_factory
from helpdesk.shared.models import Base, Grant, Role, System, Tenant, Ticket, User
from scripts.seed import seed_all

# Obviously fake, tests only; 36 bytes so the Settings length gate (>= 32) passes.
TEST_JWT_SIGNING_PHRASE = "mcp-test-jwt-secret-0123456789abcdef"
# A second, unrelated secret: tokens signed with it must never verify.
WRONG_JWT_SIGNING_PHRASE = "mcp-test-wrong-secret-0123456789abcdef"

# This suite's dedicated Redis database index. 15 and 14 belong to the helpdesk
# suite (packages/helpdesk/tests/conftest.py), so this one cannot collide with
# a helpdesk test's FLUSHDB even when both run in the same session.
TEST_REDIS_DB = 12
_FALLBACK_TEST_REDIS_DB = 11


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


_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ""})

_PG_UNREACHABLE_MSG = (
    "Postgres is unreachable at the configured DATABASE_URL.\n"
    "Mcp-server tests require the compose stack: run `docker compose up -d` first.\n"
    "Original error: {error!r}"
)
# The Qdrant this suite drives. A literal rather than `load_settings()` because
# `mcp_env` EXPORTS it to the server under test: the gate below and the server
# must probe the same instance, or a green connectivity check says nothing
# about the URL the tool call will use.
TEST_QDRANT_URL = "http://localhost:6333"

_QDRANT_UNREACHABLE_MSG = (
    "Qdrant is unreachable at " + TEST_QDRANT_URL + ".\n"
    "Mcp-server tests require the compose stack: run `docker compose up -d` first.\n"
    "Original error: {error!r}"
)

_REDIS_UNREACHABLE_MSG = (
    "Redis is unreachable at the configured REDIS_URL.\n"
    "Mcp-server tests require the compose stack: run `docker compose up -d` first.\n"
    "Original error: {error!r}"
)

# --- fixed, obviously fake data for the grants probe user ---------------------
#
# Fixed instants, not offsets from "now": a grant row whose visibility depends
# on when the suite happens to run is a flaky test, not a fixture.
FAR_FUTURE_EXPIRY = datetime(2099, 6, 1, 12, 0, tzinfo=UTC)
BOUNDARY_EXPIRY = datetime(2099, 1, 1, 12, 0, tzinfo=UTC)
PAST_EXPIRY = datetime(2020, 1, 1, 12, 0, tzinfo=UTC)
GRANTED_AT = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
REVOKED_AT = datetime(2026, 2, 1, 9, 0, tzinfo=UTC)

PROBE_EMAIL = "grants.probe@fake.invalid"
PROBE_DISPLAY_NAME = "Grants Probe"
# Not a hash and not a secret: this user never authenticates, tokens are minted
# directly. A literal marker makes that impossible to mistake for a credential.
PROBE_PASSWORD_HASH = "not-a-real-hash-this-user-never-logs-in"

# A fixed instant for every planted policy version: an ingest time
# that moved with the clock would make a re-registration look like a change.
POLICY_INGESTED_AT = datetime(2026, 5, 4, 9, 30, tzinfo=UTC)


# --- handles ------------------------------------------------------------------


@dataclass(frozen=True)
class SeededUser:
    """One seeded user, with everything a token or a `get_me` assertion needs."""

    tenant_id: uuid.UUID
    tenant_slug: str
    user_id: uuid.UUID
    email: str
    display_name: str
    department_id: uuid.UUID
    principals: tuple[str, ...]
    status: str


@dataclass(frozen=True)
class ExpectedGrant:
    """What `list_my_access` must report for one of the probe user's grants."""

    grant_id: uuid.UUID
    role_slug: str
    role_name: str
    privilege: str
    system_slug: str
    system_name: str
    expires_at: datetime | None


@dataclass(frozen=True)
class SeededMcp:
    """The seeded temp database and the handles the tests address it by."""

    database_url: str
    clean_database_url: str
    """A copy made right after seeding and never connected to. A file that
    clones clones this one, so no other file's writes reach its copy."""
    tenant_a: SeededUser
    tenant_b: SeededUser
    probe: SeededUser
    visible_grants: tuple[ExpectedGrant, ...]
    hidden_grant_ids: tuple[uuid.UUID, ...]
    boundary_grant_id: uuid.UUID
    boundary_expires_at: datetime


# --- temp database ------------------------------------------------------------


async def _admin_execute(admin_url: str, sql: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


#: Same shape as the app-role migration: NOLOGIN, made only when missing. Two
#: workers racing to make it is fine; the loser's duplicate error is ignored.
_ENSURE_APP_ROLE = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'helpdesk_app') THEN
        CREATE ROLE "helpdesk_app" NOLOGIN;
    END IF;
EXCEPTION WHEN duplicate_object OR unique_violation THEN
    NULL;
END
$$
"""


class TempDatabaseFactory:
    """Creates uniquely named temp databases on the compose Postgres."""

    def __init__(self, admin_url: str) -> None:
        self._admin_url = admin_url

    def create(self) -> str:
        name = f"helpdesk_mcp_test_{uuid.uuid4().hex[:12]}"
        asyncio.run(_admin_execute(self._admin_url, f'CREATE DATABASE "{name}"'))
        return make_url(self._admin_url).set(database=name).render_as_string(hide_password=False)

    def clone(self, template_url: str) -> str:
        """A copy of an already-seeded database, made by Postgres in milliseconds.

        For a test file that WRITES to the seeded rows and so cannot share the
        one database: it copies the finished seed instead of paying for argon2
        again. The template must have no open connection when this runs.
        """
        template = make_url(template_url).database
        assert template is not None and template.startswith("helpdesk_mcp_test_")
        name = f"helpdesk_mcp_test_{uuid.uuid4().hex[:12]}"
        asyncio.run(
            _admin_execute(
                self._admin_url,
                f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname = '{template}' AND pid <> pg_backend_pid()",
            )
        )
        asyncio.run(
            _admin_execute(self._admin_url, f'CREATE DATABASE "{name}" TEMPLATE "{template}"')
        )
        return make_url(self._admin_url).set(database=name).render_as_string(hide_password=False)

    def drop(self, url: str) -> None:
        name = make_url(url).database
        assert name is not None and name.startswith("helpdesk_mcp_test_")
        asyncio.run(_admin_execute(self._admin_url, f'DROP DATABASE "{name}" WITH (FORCE)'))


@pytest.fixture(scope="session")
def mcp_pg() -> TempDatabaseFactory:
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
        pytest.fail(_PG_UNREACHABLE_MSG.format(error=exc), pytrace=False)
    # Roles belong to the whole server, not one database. A temp database that
    # skips the migrations still needs the app role the checkpointer grants to,
    # so make it here the way the migration does. Without this, a test passes or
    # fails depending on whether a migrating test ran earlier on the same server.
    asyncio.run(_admin_execute(settings.database_url, _ENSURE_APP_ROLE))
    return TempDatabaseFactory(settings.database_url)


def _user_from_row(row: Any) -> SeededUser:
    """Build a `SeededUser` from a Core row (every column is `Any` at this seam)."""
    department_id = row.department_id
    assert department_id is not None, "seeded user has no department"
    return SeededUser(
        tenant_id=uuid.UUID(str(row.tenant_id)),
        tenant_slug=str(row.slug),
        user_id=uuid.UUID(str(row.id)),
        email=str(row.email),
        display_name=str(row.display_name),
        department_id=uuid.UUID(str(department_id)),
        principals=tuple(str(principal) for principal in row.principals),
        status=str(row.status),
    )


# How many times `seeded_mcp_db` has been built in this process. Must stay 1.
SEEDED_DB_BUILDS = 0


@pytest.fixture(scope="session")
def seeded_mcp_db(mcp_pg: TempDatabaseFactory) -> Iterator[SeededMcp]:
    """One database, created and seeded once, plus the grants probe user."""
    global SEEDED_DB_BUILDS
    SEEDED_DB_BUILDS += 1
    url = mcp_pg.create()

    async def prepare() -> SeededMcp:
        engine = create_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            await seed_all(url)

            async with engine.begin() as conn:
                tenant_rows = (
                    await conn.execute(select(Tenant.id, Tenant.slug).order_by(Tenant.slug))
                ).all()
                assert len(tenant_rows) >= 2, (
                    f"the mcp tests need two seeded tenants, found {len(tenant_rows)}"
                )
                users: list[SeededUser] = []
                for tenant_row in tenant_rows[:2]:
                    user_row = (
                        await conn.execute(
                            select(
                                User.id,
                                User.tenant_id,
                                User.email,
                                User.display_name,
                                User.department_id,
                                User.principals,
                                User.status,
                                Tenant.slug,
                            )
                            .join(Tenant, Tenant.id == User.tenant_id)
                            .where(
                                User.tenant_id == tenant_row.id,
                                User.department_id.is_not(None),
                                User.status == "active",
                            )
                            .order_by(User.email)
                            .limit(1)
                        )
                    ).one()
                    users.append(_user_from_row(user_row))
                tenant_a, tenant_b = users[0], users[1]
                assert tenant_a.tenant_id != tenant_b.tenant_id

                role_rows = (
                    await conn.execute(
                        select(
                            Role.id,
                            Role.slug,
                            Role.name,
                            Role.privilege,
                            System.slug.label("system_slug"),
                            System.name.label("system_name"),
                        )
                        .join(System, System.id == Role.system_id)
                        .where(Role.tenant_id == tenant_a.tenant_id)
                        .order_by(Role.slug)
                        .limit(6)
                    )
                ).all()
                assert len(role_rows) == 6, (
                    f"the grants probe needs six roles in tenant {tenant_a.tenant_slug}, "
                    f"found {len(role_rows)}"
                )
                # The three visible grants are given roles in the REVERSE of
                # the order `list_active_grants_for_user` promises, so the
                # ordering test cannot pass on insertion order if the
                # ORDER BY is dropped.
                by_output_order = sorted(role_rows, key=lambda row: (row.system_slug, row.slug))
                role_rows = [*reversed(by_output_order[:3]), *by_output_order[3:]]

                probe_id = uuid.uuid4()
                await conn.execute(
                    insert(User).values(
                        id=probe_id,
                        tenant_id=tenant_a.tenant_id,
                        department_id=tenant_a.department_id,
                        email=PROBE_EMAIL,
                        display_name=PROBE_DISPLAY_NAME,
                        password_hash=PROBE_PASSWORD_HASH,
                        principals=["employee"],
                        status="active",
                        start_date=date(2026, 1, 1),
                        end_date=None,
                    )
                )

                # One row per conjunct of "active": the three hidden rows each
                # violate exactly one, so a dropped clause fails a named case
                # rather than hiding behind the other two.
                plans: list[tuple[str, datetime | None, datetime | None]] = [
                    ("active", FAR_FUTURE_EXPIRY, None),  # visible
                    ("active", None, None),  # visible: no expiry at all
                    ("active", BOUNDARY_EXPIRY, None),  # visible: boundary row
                    ("active", PAST_EXPIRY, None),  # hidden: expires_at passed
                    ("active", None, REVOKED_AT),  # hidden: revoked_at set
                    ("expired", None, None),  # hidden: status not active
                ]
                grant_ids = [uuid.uuid4() for _ in plans]
                await conn.execute(
                    insert(Grant),
                    [
                        {
                            "id": grant_id,
                            "tenant_id": tenant_a.tenant_id,
                            "user_id": probe_id,
                            "role_id": role_row.id,
                            "request_id": None,
                            "granted_by_user_id": None,
                            "granted_at": GRANTED_AT,
                            "expires_at": expires_at,
                            "revoked_at": revoked_at,
                            "revoke_reason": None,
                            "status": status,
                        }
                        for grant_id, role_row, (status, expires_at, revoked_at) in zip(
                            grant_ids, role_rows, plans, strict=True
                        )
                    ],
                )

                visible = tuple(
                    ExpectedGrant(
                        grant_id=grant_ids[index],
                        role_slug=str(role_rows[index].slug),
                        role_name=str(role_rows[index].name),
                        privilege=str(role_rows[index].privilege),
                        system_slug=str(role_rows[index].system_slug),
                        system_name=str(role_rows[index].system_name),
                        expires_at=plans[index][1],
                    )
                    for index in (0, 1, 2)
                )

            probe = SeededUser(
                tenant_id=tenant_a.tenant_id,
                tenant_slug=tenant_a.tenant_slug,
                user_id=probe_id,
                email=PROBE_EMAIL,
                display_name=PROBE_DISPLAY_NAME,
                department_id=tenant_a.department_id,
                principals=("employee",),
                status="active",
            )
            return SeededMcp(
                database_url=url,
                clean_database_url="",
                tenant_a=tenant_a,
                tenant_b=tenant_b,
                probe=probe,
                visible_grants=visible,
                hidden_grant_ids=(grant_ids[3], grant_ids[4], grant_ids[5]),
                boundary_grant_id=grant_ids[2],
                boundary_expires_at=BOUNDARY_EXPIRY,
            )
        finally:
            await engine.dispose()

    clean_url: str | None = None
    try:
        seeded = asyncio.run(prepare())
        clean_url = mcp_pg.clone(url)
        yield dataclasses.replace(seeded, clean_database_url=clean_url)
    finally:
        if clean_url is not None:
            mcp_pg.drop(clean_url)
        mcp_pg.drop(url)


# --- Redis --------------------------------------------------------------------


def _redis_url_for_db(url: str, index: int) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{index}", parts.query, parts.fragment))


def _redis_db_index(url: str) -> int:
    path = urlsplit(url).path.lstrip("/")
    return int(path) if path.isdigit() else 0


def _assert_local(url: str) -> None:
    """Refuse to FLUSHDB anything that is not on this machine.

    Same guard, same reasoning as `packages/helpdesk/tests/conftest.py`: a
    different index on a shared Redis is somebody else's live data.
    """
    host = urlsplit(url).hostname or ""
    if host not in _LOCAL_HOSTS:
        pytest.fail(
            f"REDIS_URL points at {host!r}, not this machine. The mcp-server tests "
            "FLUSHDB the database index they run against and will not do that to a "
            "remote or shared Redis. Point REDIS_URL at the compose stack.",
            pytrace=False,
        )


async def _redis_flushdb(url: str) -> None:
    client: Redis = Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
    try:
        await client.flushdb()
    finally:
        await client.aclose()


@pytest.fixture(scope="session")
def mcp_redis_base_url() -> str:
    """Connectivity gate: fails loudly (never skips) when Redis is down."""
    try:
        settings = load_settings()
    except ValidationError as exc:
        pytest.fail(
            f"REDIS_URL is not configured (env or .env). Copy .env.example to .env.\n{exc}",
            pytrace=False,
        )
    _assert_local(settings.redis_url)
    configured = _redis_db_index(settings.redis_url)
    index = _worker_redis_index(TEST_REDIS_DB, _FALLBACK_TEST_REDIS_DB, configured)
    url = _redis_url_for_db(settings.redis_url, index)
    try:
        asyncio.run(_redis_flushdb(url))
    except Exception as exc:
        pytest.fail(_REDIS_UNREACHABLE_MSG.format(error=exc), pytrace=False)
    return url


@pytest.fixture
def mcp_redis_url(mcp_redis_base_url: str) -> str:
    """A flushed, dedicated Redis index for one test (the per-test namespace)."""
    configured = _redis_db_index(load_settings().redis_url)
    index = _redis_db_index(mcp_redis_base_url)
    assert index != configured, (
        f"the test Redis db index ({index}) must differ from the configured one "
        f"({configured}); refusing to FLUSHDB"
    )
    asyncio.run(_redis_flushdb(mcp_redis_base_url))
    return mcp_redis_base_url


# --- the environment the server reads ----------------------------------------


@pytest.fixture
def mcp_env(
    seeded_mcp_db: SeededMcp, mcp_redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> SeededMcp:
    """Point the server's per-call `load_settings()` at the test backing services."""
    monkeypatch.setenv("DATABASE_URL", seeded_mcp_db.database_url)
    monkeypatch.setenv("REDIS_URL", mcp_redis_url)
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SIGNING_PHRASE)
    monkeypatch.setenv("QDRANT_URL", TEST_QDRANT_URL)
    return seeded_mcp_db


# --- one engine per database, shared by every probe ---------------------------

_PROBE_ENGINES: dict[str, AsyncEngine] = {}


def probe_engine(database_url: str) -> AsyncEngine:
    """The one engine for `database_url` in this run, built on first use.

    `NullPool` keeps no connection between calls, so the same engine is safe in
    every test's own `asyncio.run` loop. Test files reach the database through
    this and the helpers below; none builds its own.
    """
    engine = _PROBE_ENGINES.get(database_url)
    if engine is None:
        engine = create_async_engine(database_url, poolclass=NullPool)
        _PROBE_ENGINES[database_url] = engine
    return engine


def probe_session_factory(database_url: str) -> async_sessionmaker[AsyncSession]:
    """A session factory (tenant guard included) over `probe_engine`."""
    return create_session_factory(probe_engine(database_url))


def sql_rows(database_url: str, sql: str, params: dict[str, Any]) -> list[Any]:
    """Run one statement in its own transaction; rows back when it returns any."""

    async def scenario() -> list[Any]:
        async with probe_engine(database_url).begin() as conn:
            result = await conn.execute(text(sql), params)
            return list(result) if result.returns_rows else []

    return asyncio.run(scenario())


def sql_execute(database_url: str, sql: str, params: dict[str, Any]) -> None:
    """Run one write statement in its own transaction."""
    sql_rows(database_url, sql, params)


# --- tokens -------------------------------------------------------------------


def token_for(
    user: SeededUser,
    *,
    principals: tuple[str, ...] | None = None,
    now: datetime | None = None,
    secret: str = TEST_JWT_SIGNING_PHRASE,
) -> str:
    """A real JWT for a seeded user, signed the way login signs one."""
    return issue_token(
        {
            "sub": str(user.user_id),
            "tenant_id": str(user.tenant_id),
            "department_id": str(user.department_id),
            "principals": list(user.principals if principals is None else principals),
            "sid": str(uuid.uuid4()),
        },
        secret,
        now if now is not None else datetime.now(UTC),
    )


def token_with_claims(
    user: SeededUser,
    overrides: Mapping[str, object],
    *,
    now: datetime | None = None,
    secret: str = TEST_JWT_SIGNING_PHRASE,
) -> str:
    """A real JWT whose claims can be forged one at a time (non-UUID sub, ...)."""
    claims: dict[str, object] = {
        "sub": str(user.user_id),
        "tenant_id": str(user.tenant_id),
        "department_id": str(user.department_id),
        "principals": list(user.principals),
        "sid": str(uuid.uuid4()),
    }
    claims.update(overrides)
    return issue_token(claims, secret, now if now is not None else datetime.now(UTC))


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- driving the real MCP transport -------------------------------------------
#
# `create_app()` is called per call, not per session: the SDK's
# StreamableHTTPSessionManager refuses a second `run()` on one instance and each
# test drives its own `asyncio.run` loop. Building the ASGI app is in-process
# and cheap; the backing services it talks to stay session-scoped.
#
# The base URL must be a name `MCP_ALLOWED_HOSTS` carries: the transport's
# DNS-rebinding protection is configured, not inferred, and the
# default list holds the three loopback names plus the compose service name.
_BASE_URL = "http://127.0.0.1:8000"


async def _drive[T](
    headers: Mapping[str, str],
    scenario: Callable[[Client], Awaitable[T]],
) -> T:
    from helpdesk.mcp_server.main import create_app
    from helpdesk.mcp_server.tools import dispatch as pipeline

    app = create_app()
    transport = httpx2.ASGITransport(app=app)
    try:
        async with (
            app.router.lifespan_context(app),
            httpx2.AsyncClient(
                transport=transport, base_url=_BASE_URL, headers=dict(headers)
            ) as http,
            Client(streamable_http_client(f"{_BASE_URL}/mcp", http_client=http)) as client,
        ):
            return await scenario(client)
    finally:
        # This loop is about to die and its pooled Postgres and Redis
        # connections would die with it, unclosed. A few dozen tests of that
        # is `TooManyConnectionsError` for everything that runs afterwards.
        await pipeline.close_shared_connections()


def call_tool(
    headers: Mapping[str, str], name: str, arguments: Mapping[str, Any] | None = None
) -> mcp_types.CallToolResult:
    """One `tools/call` through a real MCP client session over the real transport."""

    async def scenario(client: Client) -> mcp_types.CallToolResult:
        return await client.call_tool(name, dict(arguments or {}))

    return asyncio.run(_drive(headers, scenario))


def list_tools(headers: Mapping[str, str] | None = None) -> mcp_types.ListToolsResult:
    """One `tools/list` through a real MCP client session."""

    async def scenario(client: Client) -> mcp_types.ListToolsResult:
        return await client.list_tools()

    return asyncio.run(_drive(headers or {}, scenario))


def error_code(result: mcp_types.CallToolResult) -> str:
    """The single content block of an errored result, as an exact string.

    Reads the whole text, never a substring: the error contract is that the result
    carries a code and nothing else, so a code with an explanation appended
    must fail here rather than pass a `in` check.
    """
    assert result.is_error is True, f"expected an errored result, got {result!r}"
    assert len(result.content) == 1, f"expected exactly one content block, got {result.content!r}"
    block = result.content[0]
    assert isinstance(block, mcp_types.TextContent), f"expected text content, got {block!r}"
    return block.text


def http_get(path: str) -> httpx2.Response:
    """One plain HTTP GET against a fresh app (for /healthz and /readyz)."""

    async def scenario() -> httpx2.Response:
        from helpdesk.mcp_server.main import create_app
        from helpdesk.mcp_server.tools import dispatch as pipeline

        app = create_app()
        try:
            async with (
                app.router.lifespan_context(app),
                httpx2.AsyncClient(
                    transport=httpx2.ASGITransport(app=app), base_url=_BASE_URL
                ) as http,
            ):
                return await http.get(path)
        finally:
            await pipeline.close_shared_connections()

    return asyncio.run(scenario())


# The smallest well-formed JSON-RPC envelope the transport will read. The body
# only has to get PAST the middleware, so `tools/list` is used rather than a
# tool call: it needs no token and touches no tenant row.
_TOOLS_LIST_BODY: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/list",
    "params": {},
}

# `TransportSecurityMiddleware.validate_request` checks Content-Type BEFORE the
# Host header and answers 400 when it is not `application/json`, so a POST that
# omits it never reaches `_validate_host` and a Host case built on one would be
# red for the wrong reason. `accept` carries both
# media types because the streamable HTTP transport answers 406 without them,
# which is likewise not the refusal a Host case is about.
_MCP_POST_HEADERS: dict[str, str] = {
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}


def mcp_post(
    headers: Mapping[str, str] | None = None, *, drop_host: bool = False
) -> httpx2.Response:
    """One raw JSON-RPC POST to `/mcp`, with the caller's headers verbatim.

    The only way to choose the `Host` header the transport sees: every other
    helper here drives the SDK `Client`, which builds its own request from
    `_BASE_URL` and always sends the loopback name.

    `drop_host=True` sends NO `Host` header at all. httpx derives one from the
    URL, so the request is built first and the header deleted off the built
    object; `httpx2.Request` is used rather than a client kwarg because a
    `None` value in a header mapping is not a removal. Verified against the
    installed SDK, which logs "Missing Host header in request" and answers 421.
    """

    async def scenario() -> httpx2.Response:
        from helpdesk.mcp_server.main import create_app
        from helpdesk.mcp_server.tools import dispatch as pipeline

        app = create_app()
        try:
            async with (
                app.router.lifespan_context(app),
                httpx2.AsyncClient(
                    transport=httpx2.ASGITransport(app=app), base_url=_BASE_URL
                ) as http,
            ):
                request = httpx2.Request("POST", f"{_BASE_URL}{'/mcp'}", json=_TOOLS_LIST_BODY)
                for name, value in {**_MCP_POST_HEADERS, **dict(headers or {})}.items():
                    request.headers[name] = value
                if drop_host:
                    del request.headers["host"]
                return await http.send(request)
        finally:
            # Same reason as `_drive`: this loop is about to die and its pooled
            # Postgres and Redis connections would die with it, unclosed.
            await pipeline.close_shared_connections()

    return asyncio.run(scenario())


def transport_security_of_a_fresh_app() -> object:
    """The `TransportSecuritySettings` the constructed app's transport holds.

    Read off the built application rather than off `build_server()`: the check is
    about what `create_app()` PASSES, and the settings object
    reaches `StreamableHTTPSessionManager(security_settings=...)` inside
    `Server.streamable_http_app`. The path is the mount `create_app()` adds at
    `/`, that Starlette app's `/mcp` route, and the `StreamableHTTPASGIApp`
    endpoint's own `session_manager` (read off the SDK body,
    not guessed). Returns `object` so the caller carries the narrowing
    annotation itself.
    """
    from helpdesk.mcp_server.main import create_app

    app = create_app()
    for route in app.routes:
        if isinstance(route, Mount) and route.path == "":
            for inner in route.app.routes:  # type: ignore[attr-defined]
                endpoint = getattr(inner, "endpoint", None)
                manager = getattr(endpoint, "session_manager", None)
                if manager is not None:
                    settings: object = manager.security_settings
                    return settings
    raise AssertionError("no streamable HTTP transport found on the constructed app")


# --- Qdrant --------------------------------------------------------
#
# Same idea, one level down. The instance is the compose one and is
# never wiped; the namespace is the two seeded tenants' collections, which only
# this suite ever writes to, and they are dropped at BOTH ends of the session so
# a previous run's points can neither hide a leak nor invent one. Nothing here
# is rebuilt per test: per-test isolation is a uuid-suffixed DOCUMENT slug
# inside the session's collections.
#
# No file in this suite loads a real model. `RecordingEmbedder` returns vectors
# the tests choose and records every call, so a case can tell "refused" from
# "refused before any effect", and `RecordingQdrant` does the
# same for the store while answering each arm with the page the case programmed.


@pytest.fixture(scope="session")
def mcp_qdrant() -> str:
    """Connectivity gate: fails loudly (never skips) when Qdrant is down.

    A skip would let tenant isolation at the TOOL level, the only
    level a client can reach, go unproven on a green run.
    """
    try:
        response = httpx.get(f"{TEST_QDRANT_URL}/collections", timeout=5.0)
        response.raise_for_status()
    except Exception as exc:
        pytest.fail(_QDRANT_UNREACHABLE_MSG.format(error=exc), pytrace=False)
    return TEST_QDRANT_URL


@pytest.fixture(scope="session")
def mcp_policy_collections(seeded_mcp_db: SeededMcp, mcp_qdrant: str) -> Iterator[tuple[str, str]]:
    """Both seeded tenants' policy collections, dropped at both ends.

    The names are derived from `seeded_mcp_db.tenant_a/​tenant_b.tenant_slug` —
    the handles the seeding fixture read back — never from a slug pinned by
    name here: the pair the fixture returns is whichever two
    tenants the seed produced, and a file that dropped one name while planting
    into the other would be right half the time.
    """
    names = (
        collection_name(seeded_mcp_db.tenant_a.tenant_slug),
        collection_name(seeded_mcp_db.tenant_b.tenant_slug),
    )
    for name in names:
        httpx.delete(f"{mcp_qdrant}/collections/{name}", timeout=30.0)
    yield names
    for name in names:
        httpx.delete(f"{mcp_qdrant}/collections/{name}", timeout=30.0)


def dense_vector() -> tuple[float, ...]:
    """One all-positive vector of the model's width, shared by every text.

    Cosine is then ~1.0 between any planted chunk and any query, so what a real
    search returns is decided by the COLLECTION and the filters — which is what
    the isolation case is about — and never by whether a fake similarity
    happened to clear the refusal floor.
    """
    return tuple(0.05 + (index % 7) / 100.0 for index in range(DENSE_DIMENSION))


class RecordingEmbedder:
    """An `Embedder` that records every call and costs nothing.

    Vectors are at the real model's width because they are written into and
    queried against the real collection, which rejects any other.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed_dense(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        self.calls.append("embed_dense")
        return [dense_vector() for _ in texts]

    def embed_sparse(self, texts: Sequence[str]) -> list[SparseVector]:
        self.calls.append("embed_sparse")
        return [SparseVector(indices=(1, 5, 9), values=(0.4, 0.3, 0.3)) for _ in texts]

    def count_tokens(self, text: str) -> int:
        self.calls.append("count_tokens")
        return len(text.split())


@dataclass(frozen=True)
class StubPoint:
    """One point as `retrieval._hits` reads it: an id, a score and a payload."""

    id: str
    score: float
    payload: dict[str, Any]


@dataclass(frozen=True)
class StubResponse:
    points: list[StubPoint]


def stub_point(
    *,
    chunk_id: uuid.UUID,
    document_id: uuid.UUID,
    document_slug: str,
    text: str,
    score: float,
    version: str = "v1",
    heading_path: tuple[str, ...] = ("1. Purpose",),
) -> StubPoint:
    """A point carrying exactly the payload keys upsert writes."""
    return StubPoint(
        id=str(chunk_id),
        score=score,
        payload={
            "document_id": str(document_id),
            "document_slug": document_slug,
            "version": version,
            "is_current": True,
            "heading_path": list(heading_path),
            "chunk_index": 0,
            "text": text,
        },
    )


class RecordingQdrant:
    """A stand-in store: records every call, answers each arm as programmed.

    Two uses. The validation cases assert `calls == []`, which is how "refused"
    is told apart from "refused before any effect". The page
    cases program the two arms independently, so the ORDER the tool returns can
    be made to differ from both arms' own orders and a dense-only or
    unfused implementation is visible at the wire.
    """

    def __init__(
        self,
        dense: Sequence[StubPoint] = (),
        sparse: Sequence[StubPoint] = (),
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.collections: list[str] = []
        self._dense = list(dense)
        self._sparse = list(sparse)

    async def query_points(self, collection_name: str, /, **kwargs: Any) -> StubResponse:
        self.calls.append(("query_points", dict(kwargs)))
        self.collections.append(collection_name)
        using = kwargs.get("using")
        return StubResponse(points=list(self._dense if using == "dense" else self._sparse))

    async def close(self) -> None:
        self.calls.append(("close", {}))


def as_client(store: RecordingQdrant) -> AsyncQdrantClient:
    return cast(AsyncQdrantClient, store)


@dataclass(frozen=True)
class PlantedDocument:
    """One registered policy version, in Postgres and (optionally) in Qdrant."""

    document_id: uuid.UUID
    slug: str
    title: str
    version: str
    texts: tuple[str, ...]
    chunk_ids: tuple[uuid.UUID, ...]


async def plant_policy_document(
    session: AsyncSession,
    *,
    tenant_slug: str,
    tenant_id: uuid.UUID,
    slug: str,
    texts: Sequence[str],
    version: str = "v1",
    title: str | None = None,
    client: AsyncQdrantClient | None = None,
    ingested_at: datetime | None = None,
) -> PlantedDocument:
    """Register a version and its chunk rows; write its points when asked.

    `client=None` plants the Postgres half only, which is what a case driving a
    `RecordingQdrant` needs: the citation titles are resolved from
    `policy_documents`, so those rows must be real even when the page itself is
    programmed. With a client the same document also reaches the real
    collection, which is what the isolation case searches.

    Point ids are `vectors.point_id`, computed rather than invented, so a
    programmed point names a `policy_chunks` row that actually exists.
    """
    document_title = title if title is not None else f"Fake Policy {slug}"
    result = await register_document_version(
        session,
        tenant_slug=tenant_slug,
        slug=slug,
        title=document_title,
        version=version,
        source_path=f"policies/{tenant_slug}/{version}/{slug}.md",
        source=f"# {document_title}\n\n{' '.join(texts)}\n".encode(),
        ingested_at=ingested_at if ingested_at is not None else POLICY_INGESTED_AT,
    )
    document_id = result.document.id
    chunks = [
        Chunk(
            heading_path=("1. Purpose",),
            char_start=index * 200,
            char_end=index * 200 + len(text),
            text=text,
            dense=dense_vector(),
            sparse=SparseVector(indices=(1, 5, 9), values=(0.4, 0.3, 0.3)),
        )
        for index, text in enumerate(texts)
    ]
    chunk_ids = [point_id(tenant_id, document_id, index) for index in range(len(chunks))]
    if client is not None:
        await ensure_collection(client, tenant_slug)
        upserted = await upsert_chunks(
            client,
            tenant_slug,
            tenant_id=tenant_id,
            document_id=document_id,
            document_slug=slug,
            version=version,
            chunks=chunks,
        )
        assert list(upserted) == chunk_ids, "the deterministic point ids moved"
    await write_chunks(session, document_id=document_id, chunks=chunks, point_ids=chunk_ids)
    return PlantedDocument(
        document_id=document_id,
        slug=slug,
        title=document_title,
        version=version,
        texts=tuple(texts),
        chunk_ids=tuple(chunk_ids),
    )


def plant_ticket(
    database_url: str,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    number: int,
    created_at: datetime,
    status: str = "open",
) -> uuid.UUID:
    """One ticket written around the tools, with the time and number chosen.

    The tools stamp `now()` and count numbers themselves, so a case that needs
    a known order (older planted after newer, or two at the same instant)
    writes the row here. The engine is built and disposed inside this one
    call, like `_admin_execute`; no test file builds its own.
    """
    ticket_id = uuid.uuid4()

    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    insert(Ticket).values(
                        id=ticket_id,
                        tenant_id=tenant_id,
                        number=number,
                        user_id=user_id,
                        kind="general",
                        title=f"fake planted ticket {ticket_id.hex}",
                        body="fake body that a list must leave out",
                        status=status,
                        session_id=None,
                        created_at=created_at,
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())
    return ticket_id
