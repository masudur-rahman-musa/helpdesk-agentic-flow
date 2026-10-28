"""GET /admin/audit/verify on agent-api.

Expected implementation surface (`agent_api.admin_routes`, mounted on
`agent_api.main.app`):

    GET /admin/audit/verify   Authorization: Bearer <JWT>
      200 -> {"ok": true,  "broken_at": null,
              "truncated_at": null, "anchored_seq": null}   intact chain
      200 -> {"ok": false, "broken_at": <seq>,
              "truncated_at": null, "anchored_seq": ...}    first broken link
      200 -> {"ok": false, "broken_at": null,
              "truncated_at": <seq>, "anchored_seq": <seq>} tail truncated
      403 -> {"code": "FORBIDDEN"}   token valid, principal outside
             {auditor, it_admin, platform_admin} (the audit-read roles)
      401 -> {"code": "FORBIDDEN"}   missing, malformed, wrongly signed or
             expired token — the same typed body used for refusals

The tenant comes from the JWT only: the endpoint
takes no tenant argument and a caller never sees another tenant's chain.

Fixtures live in this module rather than a conftest, for the same reason
`test_login.py` gives: a second `conftest` module would collide with
`packages/helpdesk/tests/conftest.py` under the repo-wide mypy run. The
schema comes from `alembic upgrade head` (not `create_all`) because the
tamper test has to prove that changing a stored row needs the append-only
trigger disabled first.
"""

import asyncio
import os
import subprocess
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import create_async_engine

from helpdesk.agent_api.api.router import app
from helpdesk.agent_api.services import audit as services_audit
from helpdesk.shared.audit import AuditEventInput
from helpdesk.shared.audit.anchor import anchor_tenant_chain
from helpdesk.shared.audit.writer import append_event
from helpdesk.shared.auth.jwt import issue_token
from helpdesk.shared.core.config import load_settings
from helpdesk.shared.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.shared.models import Tenant

REPO_ROOT = Path(__file__).resolve().parents[3]

# Obviously fake, tests only; >= 32 bytes so the settings length gate passes.
TEST_JWT_SIGNING_PHRASE = "audit-verify-test-jwt-secret-0123456789"

ALLOWED_PRINCIPALS = ("auditor", "it_admin", "platform_admin")
DENIED_PRINCIPALS = ("employee", "manager", "system_owner", "hr")

_UNREACHABLE_MSG = (
    "Postgres is unreachable at the configured DATABASE_URL.\n"
    "Audit tests require the compose stack: run `docker compose up -d` first.\n"
    "Original error: {error!r}"
)


# --- throwaway database, migrated -------------------------------------------


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
        name = f"helpdesk_audit_test_{uuid.uuid4().hex[:12]}"
        asyncio.run(_admin_execute(self._admin_url, f'CREATE DATABASE "{name}"'))
        return make_url(self._admin_url).set(database=name).render_as_string(hide_password=False)

    def drop(self, url: str) -> None:
        name = make_url(url).database
        assert name is not None and name.startswith("helpdesk_audit_test_")
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
def audit_db(pg: TempDatabaseFactory) -> Iterator[str]:
    """One migrated database for the module; every test seeds its own tenant."""
    url = pg.create()
    result = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT / "backend",  # alembic.ini lives in backend/
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"alembic upgrade head failed:\n{result.stderr}"
    try:
        yield url
    finally:
        pg.drop(url)


@pytest.fixture
def audit_env(audit_db: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """Point the app's per-request load_settings() at the migrated temp database."""
    monkeypatch.setenv("DATABASE_URL", audit_db)
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SIGNING_PHRASE)
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    return audit_db


# --- helpers ------------------------------------------------------------------


def _event(index: int) -> AuditEventInput:
    return AuditEventInput(
        occurred_at=None,
        actor_kind="system",
        actor_ref=str(uuid.UUID(int=index)),
        session_id=None,
        trace_id=f"trace-fake-{index}",
        event_type="ticket.created",
        subject_type="ticket",
        subject_id=str(uuid.UUID(int=2000 + index)),
        payload={"fake": True, "index": index},
    )


def _seed_tenant_with_events(database_url: str, slug: str, count: int) -> uuid.UUID:
    """A fresh tenant with `count` chained audit events; returns its id."""

    async def _run() -> uuid.UUID:
        engine = create_engine(database_url)
        try:
            factory = create_session_factory(engine)
            async with factory() as session:
                tenant = Tenant(slug=slug, name=f"Fake Tenant {slug}", settings={})
                session.add(tenant)
                await session.commit()
                tenant_id = tenant.id
            for index in range(1, count + 1):
                async with tenant_session(factory, tenant_id) as session:
                    await append_event(session, tenant_id, _event(index))
            return tenant_id
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def _seqs(database_url: str, tenant_id: uuid.UUID) -> list[int]:
    async def _run() -> list[int]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as conn:
                rows = await conn.execute(
                    text("SELECT seq FROM audit_events WHERE tenant_id = :t ORDER BY seq"),
                    {"t": tenant_id},
                )
                return [int(row[0]) for row in rows]
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def _tamper(database_url: str, tenant_id: uuid.UUID, seq: int) -> None:
    """Rewrite one stored row the only way possible: superuser, trigger disabled."""

    async def _run() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("ALTER TABLE audit_events DISABLE TRIGGER USER"))
                await conn.execute(
                    text(
                        'UPDATE audit_events SET payload = \'{"fake": "tampered"}\'::jsonb '
                        "WHERE tenant_id = :t AND seq = :s"
                    ),
                    {"t": tenant_id, "s": seq},
                )
                await conn.execute(text("ALTER TABLE audit_events ENABLE TRIGGER USER"))
        finally:
            await engine.dispose()

    asyncio.run(_run())


def _anchor(database_url: str, tenant_id: uuid.UUID) -> int:
    """Anchor the tenant's verified head and return the seq that was stored.

    this is what the scheduler's `chain_anchor` job does, called
    directly so the route case does not need a running scheduler.
    """

    async def _run() -> int:
        engine = create_engine(database_url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant_id) as session:
                written = await anchor_tenant_chain(session)
                await session.commit()
            assert written is not None, "a clean chain with events must anchor"
            return int(written.audit_seq)
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def _truncate_tail(database_url: str, tenant_id: uuid.UUID, keep: int) -> None:
    """Delete every event past the `keep`th: superuser, triggers disabled.

    The surviving rows still link perfectly, which is why nothing but the
    stored anchor can notice.
    """

    async def _run() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("ALTER TABLE audit_events DISABLE TRIGGER USER"))
                await conn.execute(
                    text(
                        "DELETE FROM audit_events WHERE tenant_id = :t AND seq > ("
                        "SELECT seq FROM audit_events WHERE tenant_id = :t "
                        "ORDER BY seq OFFSET :offset LIMIT 1)"
                    ),
                    {"t": tenant_id, "offset": keep - 1},
                )
                await conn.execute(text("ALTER TABLE audit_events ENABLE TRIGGER USER"))
        finally:
            await engine.dispose()

    asyncio.run(_run())


def _token(
    tenant_id: uuid.UUID,
    principals: list[str],
    *,
    secret: str = TEST_JWT_SIGNING_PHRASE,
    now: datetime | None = None,
) -> str:
    return issue_token(
        {
            "sub": str(uuid.uuid4()),
            "tenant_id": str(tenant_id),
            "department_id": str(uuid.uuid4()),
            "principals": principals,
            "sid": str(uuid.uuid4()),
        },
        secret,
        now or datetime.now(UTC),
    )


def _get_verify(headers: dict[str, str] | None = None) -> httpx.Response:
    async def _run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/admin/audit/verify", headers=headers or {})

    return asyncio.run(_run())


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- happy path ---------------------------------------------------------------


@pytest.mark.parametrize("principal", ALLOWED_PRINCIPALS)
def test_clean_chain_verifies_ok_for_each_allowed_principal(audit_env: str, principal: str) -> None:
    tenant_id = _seed_tenant_with_events(audit_env, f"verify-ok-{principal}", 3)

    response = _get_verify(_auth(_token(tenant_id, [principal])))

    assert response.status_code == 200, response.text
    assert response.json() == {
        "ok": True,
        "broken_at": None,
        "truncated_at": None,
        "anchored_seq": None,
    }


def test_empty_chain_verifies_ok(audit_env: str) -> None:
    tenant_id = _seed_tenant_with_events(audit_env, "verify-empty", 0)

    response = _get_verify(_auth(_token(tenant_id, ["auditor"])))

    assert response.status_code == 200, response.text
    assert response.json() == {
        "ok": True,
        "broken_at": None,
        "truncated_at": None,
        "anchored_seq": None,
    }


# --- tampering ----------------------------------------------------------------


@pytest.mark.parametrize("position", [0, 1, 4])
def test_tampered_row_is_reported_at_its_seq(audit_env: str, position: int) -> None:
    """First, middle and last rows: verify names exactly the tampered seq."""
    tenant_id = _seed_tenant_with_events(audit_env, f"verify-tamper-{position}", 5)
    seqs = _seqs(audit_env, tenant_id)
    assert len(seqs) == 5, seqs

    _tamper(audit_env, tenant_id, seqs[position])
    response = _get_verify(_auth(_token(tenant_id, ["auditor"])))

    assert response.status_code == 200, response.text
    assert response.json() == {
        "ok": False,
        "broken_at": seqs[position],
        "truncated_at": None,
        "anchored_seq": None,
    }


def test_verify_covers_the_callers_tenant_only(audit_env: str) -> None:
    """A tampered chain in tenant B never shows up for tenant A's auditor."""
    tenant_a = _seed_tenant_with_events(audit_env, "verify-scope-a", 3)
    tenant_b = _seed_tenant_with_events(audit_env, "verify-scope-b", 3)
    _tamper(audit_env, tenant_b, _seqs(audit_env, tenant_b)[1])

    clean = _get_verify(_auth(_token(tenant_a, ["auditor"])))
    broken = _get_verify(_auth(_token(tenant_b, ["auditor"])))

    assert clean.status_code == 200 and clean.json() == {
        "ok": True,
        "broken_at": None,
        "truncated_at": None,
        "anchored_seq": None,
    }
    assert broken.status_code == 200
    assert broken.json()["ok"] is False


def test_a_truncated_chain_is_reported_as_truncated_not_as_broken(audit_env: str) -> None:
    """The route carries the new verdict through unchanged.

    Catches a route that keeps its own response model, or picks two fields out
    of the verdict: `admin_routes` returns `result.model_dump()`, so the two
    new fields are the body's, and an endpoint that dropped them would leave
    the web panel and any operator curl reading `ok=false` with `broken_at`
    null and nothing at all to say why.

    Catches the whole detection being absent end to end: without it this same
    request answers `{"ok": true, ...}`, which is what every other case in
    this file asserted before. The seq is read back from the anchor
    that was actually written, never a literal.
    """
    tenant_id = _seed_tenant_with_events(audit_env, "verify-truncated", 5)
    anchored_seq = _anchor(audit_env, tenant_id)
    _truncate_tail(audit_env, tenant_id, keep=2)

    response = _get_verify(_auth(_token(tenant_id, ["auditor"])))

    assert response.status_code == 200, response.text
    assert response.json() == {
        "ok": False,
        "broken_at": None,
        "truncated_at": anchored_seq,
        "anchored_seq": anchored_seq,
    }


# --- authorization ------------------------------------------------------------


@pytest.mark.parametrize("principal", DENIED_PRINCIPALS)
def test_principal_outside_the_allowed_set_gets_typed_forbidden(
    audit_env: str, principal: str
) -> None:
    tenant_id = _seed_tenant_with_events(audit_env, f"verify-deny-{principal}", 2)

    response = _get_verify(_auth(_token(tenant_id, [principal])))

    assert response.status_code == 403, response.text
    assert response.json() == {"code": "FORBIDDEN"}


def test_one_allowed_principal_among_several_is_enough(audit_env: str) -> None:
    tenant_id = _seed_tenant_with_events(audit_env, "verify-multi-principal", 2)

    response = _get_verify(_auth(_token(tenant_id, ["employee", "auditor"])))

    assert response.status_code == 200, response.text
    assert response.json() == {
        "ok": True,
        "broken_at": None,
        "truncated_at": None,
        "anchored_seq": None,
    }


def test_unknown_principal_string_never_widens_access(audit_env: str) -> None:
    tenant_id = _seed_tenant_with_events(audit_env, "verify-unknown-principal", 2)

    response = _get_verify(_auth(_token(tenant_id, ["superuser", "root"])))

    assert response.status_code == 403, response.text
    assert response.json() == {"code": "FORBIDDEN"}


# --- authentication -----------------------------------------------------------


def _bad_token_cases(tenant_id: uuid.UUID) -> list[tuple[str, dict[str, str]]]:
    expired_issued_at = datetime.now(UTC) - timedelta(hours=9)  # 8h TTL, so already dead
    return [
        ("no Authorization header", {}),
        ("empty bearer", _auth("")),
        ("not a jwt", _auth("not-a-token")),
        ("wrong scheme", {"Authorization": _token(tenant_id, ["auditor"])}),
        ("signed with another secret", _auth(_token(tenant_id, ["auditor"], secret="x" * 40))),
        ("expired", _auth(_token(tenant_id, ["auditor"], now=expired_issued_at))),
    ]


def test_a_database_failure_still_answers_with_a_typed_code(
    audit_env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No response on this route may be an untyped 500."""
    tenant_id = _seed_tenant_with_events(audit_env, "verify-db-down", 1)

    async def boom(*args: object, **kwargs: object) -> None:
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(services_audit, "verify_tenant_chain", boom)

    response = _get_verify(_auth(_token(tenant_id, ["auditor"])))

    assert response.status_code == 500, response.text
    assert response.json() == {"code": "INTERNAL"}


def test_missing_or_invalid_token_gets_the_typed_401(audit_env: str) -> None:
    tenant_id = _seed_tenant_with_events(audit_env, "verify-401", 2)

    for label, headers in _bad_token_cases(tenant_id):
        response = _get_verify(headers)
        assert response.status_code == 401, (
            f"{label}: expected 401, got {response.status_code} {response.text}"
        )
        assert response.json() == {"code": "FORBIDDEN"}, label
