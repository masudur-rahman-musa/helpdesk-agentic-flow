"""Shared backing-service fixtures for the mcp-server suite.

Deliberately NOT a ``conftest.py``: a second module named ``conftest`` collides
with ``packages/helpdesk/tests/conftest.py`` under the repo-wide ``mypy`` run
(``files = ["packages", "services", "scripts"]``), so every test module imports
the fixtures it needs by name, the way ``packages/helpdesk/tests/_policy.py``
and ``_seed.py`` are already imported.

a project rule: the expensive things are built exactly once for the whole
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
import sys
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx2
import mcp.types as mcp_types
import pytest
from helpdesk.auth.jwt import issue_token
from helpdesk.core.db import create_engine
from helpdesk.core.settings import load_settings
from helpdesk.models import Base, Grant, Role, System, Tenant, User
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy import insert, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

# Make repo-root `scripts/` importable, same as packages/helpdesk/tests/conftest.py.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.seed import seed_all  # noqa: E402

# Obviously fake, tests only; 36 bytes so the Settings length gate (>= 32) passes.
TEST_JWT_SECRET = "mcp-test-jwt-secret-0123456789abcdef"
# A second, unrelated secret: tokens signed with it must never verify.
WRONG_JWT_SECRET = "mcp-test-wrong-secret-0123456789abcdef"

# This suite's dedicated Redis database index. 15 and 14 belong to the helpdesk
# suite (packages/helpdesk/tests/conftest.py), so this one cannot collide with
# a helpdesk test's FLUSHDB even when both run in the same session.
TEST_REDIS_DB = 12
_FALLBACK_TEST_REDIS_DB = 11

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ""})

_PG_UNREACHABLE_MSG = (
    "Postgres is unreachable at the configured DATABASE_URL.\n"
    "Plan 10a mcp-server tests require the compose stack: run `docker compose up -d` first.\n"
    "Original error: {error!r}"
)
_REDIS_UNREACHABLE_MSG = (
    "Redis is unreachable at the configured REDIS_URL.\n"
    "Plan 10a mcp-server tests require the compose stack: run `docker compose up -d` first.\n"
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


class TempDatabaseFactory:
    """Creates uniquely named temp databases on the compose Postgres."""

    def __init__(self, admin_url: str) -> None:
        self._admin_url = admin_url

    def create(self) -> str:
        name = f"helpdesk_mcp_test_{uuid.uuid4().hex[:12]}"
        asyncio.run(_admin_execute(self._admin_url, f'CREATE DATABASE "{name}"'))
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


@pytest.fixture(scope="session")
def seeded_mcp_db(mcp_pg: TempDatabaseFactory) -> Iterator[SeededMcp]:
    """One database, created and seeded once, plus the grants probe user."""
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

    try:
        yield asyncio.run(prepare())
    finally:
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
    index = TEST_REDIS_DB if configured != TEST_REDIS_DB else _FALLBACK_TEST_REDIS_DB
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
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SECRET)
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    return seeded_mcp_db


# --- tokens -------------------------------------------------------------------


def token_for(
    user: SeededUser,
    *,
    principals: tuple[str, ...] | None = None,
    now: datetime | None = None,
    secret: str = TEST_JWT_SECRET,
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
    secret: str = TEST_JWT_SECRET,
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
# The base URL must be `127.0.0.1:<port>`: the SDK turns on DNS-rebinding
# protection for a localhost host and allows `127.0.0.1:*` only.
_BASE_URL = "http://127.0.0.1:8000"


async def _drive[T](
    headers: Mapping[str, str],
    scenario: Callable[[Client], Awaitable[T]],
) -> T:
    from mcp_server import pipeline
    from mcp_server.main import create_app

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

    Reads the whole text, never a substring: the design says the boundary
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
        from mcp_server import pipeline
        from mcp_server.main import create_app

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
