"""Tests for POST /auth/login on agent-api.

Expected implementation surface:

``agent_api.auth_routes`` mounted on ``agent_api.main.app``:
    POST /auth/login  body {"tenant_slug": str, "email": str, "password": str}
      200 -> {"access_token": "<jwt>", "token_type": "bearer"}
             The JWT is HS256, signed with settings.jwt_secret, and its claims
             (checked via helpdesk.auth.jwt.verify_token) match the seeded
             user: sub/tenant_id/department_id/principals plus a fresh sid
             per login and an 8-hour expiry.
      401 -> ONE typed error body, code FORBIDDEN (the design), byte-identical
             for wrong password, unknown email, wrong tenant slug and a
             leaver-status user, so the response never reveals whether an
             account exists.

Seam (mirrors how ``agent_api.readiness`` works): the route obtains
``database_url`` and ``jwt_secret`` by calling
``helpdesk.core.settings.load_settings()`` per request, so the ``login_env``
fixture below can redirect the app to a freshly seeded temp database through
environment variables alone.

Fixtures live in this module (not a conftest) because a second ``conftest``
module would collide with ``packages/helpdesk/tests/conftest.py`` under the
repo-wide ``mypy`` run. One throwaway database is created on the compose
Postgres, schema built from ``Base.metadata``, and the real ``scripts/seed.py``
run into it once per session (argon2 makes seeding expensive). One seeded
non-demo user is then flipped to ``status='leaver'`` with a known password,
because the seed itself only creates active users. If Postgres is unreachable
the ``pg`` fixture FAILS loudly; there is no silent skip.
"""

import asyncio
import json
import re
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from agent_api.main import app
from helpdesk.auth.jwt import TokenClaims, verify_token
from helpdesk.auth.passwords import hash_password
from helpdesk.core.db import create_engine
from helpdesk.core.settings import load_settings
from helpdesk.models import Base, Tenant, User
from pydantic import ValidationError
from sqlalchemy import select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

# Make repo-root `scripts/` importable, same as packages/helpdesk/tests/conftest.py.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.seed import seed_all  # noqa: E402

DEMO_ACCOUNTS_DOC = _REPO_ROOT / "docs" / "demo-accounts.md"

# Obviously fake, tests only; >= 32 bytes so the settings length gate passes.
TEST_JWT_SECRET = "login-test-jwt-secret-0123456789abcdef"
LEAVER_PASSWORD = "Leaver-Known-Test-Password-2026!"  # obviously fake, tests only

EIGHT_HOURS = 8 * 60 * 60

# Words that would leak WHY a login failed; the typed FORBIDDEN body carries none.
_LEAKY_WORDS = ("password", "email", "user", "tenant", "leaver", "exist", "found", "unknown")

_UNREACHABLE_MSG = (
    "Postgres is unreachable at the configured DATABASE_URL.\n"
    "Plan 05 login tests require the compose stack: run `docker compose up -d` first.\n"
    "Original error: {error!r}"
)

# --- demo-accounts.md parsing -------------------------------------------------

_SECTION_RE = re.compile(r"^##\s+.*\(`(?P<slug>[^`]+)`\)\s*$")
_ROW_RE = re.compile(
    r"^\|\s*(?P<principal>[a-z_]+)\s*\|\s*(?P<email>\S+@\S+?)\s*\|\s*`(?P<password>[^`]+)`\s*\|\s*$"
)


@dataclass(frozen=True)
class DemoAccount:
    tenant_slug: str
    principal: str
    email: str
    password: str


def parse_demo_accounts() -> list[DemoAccount]:
    """Every credential row in docs/demo-accounts.md, tagged with its tenant slug."""
    accounts: list[DemoAccount] = []
    slug: str | None = None
    for line in DEMO_ACCOUNTS_DOC.read_text().splitlines():
        section = _SECTION_RE.match(line)
        if section:
            slug = section.group("slug")
            continue
        row = _ROW_RE.match(line)
        if row:
            assert slug is not None, f"credential row before any tenant heading: {line!r}"
            accounts.append(
                DemoAccount(
                    tenant_slug=slug,
                    principal=row.group("principal"),
                    email=row.group("email"),
                    password=row.group("password"),
                )
            )
    return accounts


def _demo_account(tenant_slug: str, principal: str) -> DemoAccount:
    for account in parse_demo_accounts():
        if account.tenant_slug == tenant_slug and account.principal == principal:
            return account
    raise AssertionError(f"no {principal!r} demo account for tenant {tenant_slug!r} in the doc")


# --- fixtures: temp database, seeded once, plus a leaver ----------------------


async def _admin_execute(admin_url: str, sql: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


class TempDatabaseFactory:
    def __init__(self, admin_url: str) -> None:
        self._admin_url = admin_url

    def create(self) -> str:
        name = f"helpdesk_login_test_{uuid.uuid4().hex[:12]}"
        asyncio.run(_admin_execute(self._admin_url, f'CREATE DATABASE "{name}"'))
        return make_url(self._admin_url).set(database=name).render_as_string(hide_password=False)

    def drop(self, url: str) -> None:
        name = make_url(url).database
        assert name is not None and name.startswith("helpdesk_login_test_")
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


@dataclass(frozen=True)
class SeededLogin:
    """Handle onto the seeded temp database for the login tests."""

    database_url: str
    leaver_email: str
    leaver_password: str
    leaver_tenant_slug: str


@pytest.fixture(scope="session")
def seeded_login_db(pg: TempDatabaseFactory) -> Iterator[SeededLogin]:
    """One freshly seeded database for the whole session, plus a leaver user."""
    demo_emails = {account.email for account in parse_demo_accounts()}
    assert demo_emails, f"no demo accounts parsed from {DEMO_ACCOUNTS_DOC}"

    url = pg.create()

    async def prepare() -> SeededLogin:
        engine = create_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            await seed_all(url)

            async with engine.begin() as conn:
                row = (
                    await conn.execute(
                        select(User.id, User.email, Tenant.slug)
                        .join(Tenant, Tenant.id == User.tenant_id)
                        .where(User.email.notin_(demo_emails))
                        .order_by(User.email)
                        .limit(1)
                    )
                ).one()
                await conn.execute(
                    update(User)
                    .where(User.id == row.id)
                    .values(status="leaver", password_hash=hash_password(LEAVER_PASSWORD))
                )
            return SeededLogin(
                database_url=url,
                leaver_email=row.email,
                leaver_password=LEAVER_PASSWORD,
                leaver_tenant_slug=row.slug,
            )
        finally:
            await engine.dispose()

    try:
        yield asyncio.run(prepare())
    finally:
        pg.drop(url)


@pytest.fixture
def login_env(seeded_login_db: SeededLogin, monkeypatch: pytest.MonkeyPatch) -> SeededLogin:
    """Point the app's per-request load_settings() at the seeded temp database."""
    monkeypatch.setenv("DATABASE_URL", seeded_login_db.database_url)
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SECRET)
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    return seeded_login_db


# --- helpers ------------------------------------------------------------------


def _post_login(tenant_slug: str, email: str, password: str) -> httpx.Response:
    async def _run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/auth/login",
                json={"tenant_slug": tenant_slug, "email": email, "password": password},
            )

    return asyncio.run(_run())


def _seeded_user(database_url: str, email: str) -> tuple[str, str, str, list[str], str]:
    """(user_id, tenant_id, department_id, principals, tenant_slug) straight from the DB."""

    async def _run() -> tuple[str, str, str, list[str], str]:
        engine = create_engine(database_url)
        try:
            async with engine.connect() as conn:
                row = (
                    await conn.execute(
                        select(
                            User.id,
                            User.tenant_id,
                            User.department_id,
                            User.principals,
                            Tenant.slug,
                        )
                        .join(Tenant, Tenant.id == User.tenant_id)
                        .where(User.email == email)
                    )
                ).one()
            assert row.department_id is not None, f"seeded user {email} has no department"
            return (
                str(row.id),
                str(row.tenant_id),
                str(row.department_id),
                list(row.principals),
                row.slug,
            )
        finally:
            await engine.dispose()

    return asyncio.run(_run())


# --- happy path ---------------------------------------------------------------


def test_login_happy_path_token_matches_seeded_user(login_env: SeededLogin) -> None:
    account = _demo_account("nordlicht", "employee")
    before = datetime.now(UTC)

    response = _post_login(account.tenant_slug, account.email, account.password)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_type"] == "bearer"

    claims = verify_token(body["access_token"], TEST_JWT_SECRET, datetime.now(UTC))
    assert isinstance(claims, TokenClaims)

    user_id, tenant_id, department_id, principals, slug = _seeded_user(
        login_env.database_url, account.email
    )
    assert slug == account.tenant_slug
    assert claims.sub == user_id
    assert claims.tenant_id == tenant_id
    assert claims.department_id == department_id
    assert sorted(claims.principals) == sorted(principals)
    assert account.principal in claims.principals
    assert claims.exp - claims.iat == EIGHT_HOURS
    assert claims.iat >= int(before.timestamp()) - 5  # issued now, not a canned token
    uuid.UUID(claims.sid)  # sid is a well-formed uuid


def test_each_login_gets_a_fresh_sid(login_env: SeededLogin) -> None:
    account = _demo_account("nordlicht", "employee")
    now = datetime.now(UTC)

    first = _post_login(account.tenant_slug, account.email, account.password)
    second = _post_login(account.tenant_slug, account.email, account.password)

    assert first.status_code == 200 and second.status_code == 200
    sid_a = verify_token(first.json()["access_token"], TEST_JWT_SECRET, now).sid
    sid_b = verify_token(second.json()["access_token"], TEST_JWT_SECRET, now).sid
    assert sid_a != sid_b, "sid must be a fresh UUID per login (plan 05 decision B)"


# --- the four 401 paths: one indistinguishable typed body ---------------------


def _refusal_cases(seeded: SeededLogin) -> list[tuple[str, tuple[str, str, str]]]:
    """(label, (tenant_slug, email, password)) rows that must all be refused."""
    nordlicht_employee = _demo_account("nordlicht", "employee")
    cuan_employee = _demo_account("cuan", "employee")
    return [
        (
            "wrong password",
            (nordlicht_employee.tenant_slug, nordlicht_employee.email, "Wrong-Password-2026!"),
        ),
        (
            "unknown email",
            (nordlicht_employee.tenant_slug, "nobody.here@nordlicht.example", "Any-Password-1!"),
        ),
        (
            "wrong tenant slug (cross-tenant, otherwise valid credentials)",
            ("nordlicht", cuan_employee.email, cuan_employee.password),
        ),
        (
            "nonexistent tenant slug",
            ("no-such-tenant", nordlicht_employee.email, nordlicht_employee.password),
        ),
        (
            "leaver user, correct password",
            (seeded.leaver_tenant_slug, seeded.leaver_email, seeded.leaver_password),
        ),
    ]


def test_all_refusals_return_identical_forbidden_bodies(login_env: SeededLogin) -> None:
    responses = [(label, _post_login(*args)) for label, args in _refusal_cases(login_env)]

    for label, response in responses:
        assert response.status_code == 401, f"{label}: expected 401, got {response.status_code}"

    bodies = [(label, response.json()) for label, response in responses]
    first_label, first_body = bodies[0]
    for label, body in bodies[1:]:
        assert body == first_body, (
            f"refusal body for {label!r} differs from {first_label!r}: "
            f"{body!r} != {first_body!r} — responses must not reveal why login failed"
        )

    # Typed code, no free text (the design).
    serialized = json.dumps(first_body).lower()
    assert "forbidden" in serialized, f"expected typed code FORBIDDEN in body, got {first_body!r}"
    for word in _LEAKY_WORDS:
        assert word not in serialized, (
            f"refusal body leaks {word!r}: {first_body!r} — typed code only, no free text"
        )


# --- demo-account tie-in -------------------------------


def test_every_documented_demo_account_can_log_in(login_env: SeededLogin) -> None:
    accounts = parse_demo_accounts()
    # Parse failure must fail loudly, not pass vacuously: the doc pins one
    # account per app principal for both tenants (7 x 2).
    assert {a.tenant_slug for a in accounts} == {"nordlicht", "cuan"}, accounts
    assert len(accounts) == 14, f"expected 14 demo accounts in the doc, parsed {len(accounts)}"

    now = datetime.now(UTC)
    for account in accounts:
        response = _post_login(account.tenant_slug, account.email, account.password)
        assert response.status_code == 200, (
            f"documented demo login failed for {account.email} "
            f"({account.tenant_slug}/{account.principal}): "
            f"{response.status_code} {response.text}"
        )
        claims = verify_token(response.json()["access_token"], TEST_JWT_SECRET, now)
        _user_id, tenant_id, _dept, _principals, slug = _seeded_user(
            login_env.database_url, account.email
        )
        assert claims.tenant_id == tenant_id
        assert slug == account.tenant_slug
        assert account.principal in claims.principals
