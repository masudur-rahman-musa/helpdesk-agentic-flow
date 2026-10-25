"""Deterministic, obviously fake seed data for both tenants.

Expected implementation surface (the implementer must provide exactly this):

``backend/scripts/seed.py`` (importable as ``scripts.seed`` — the repo root is on
``sys.path`` via ``backend/pyproject.toml``'s pytest ``pythonpath``)
    ``async def seed_all(database_url: str) -> None``
        Seeds BOTH tenants into the given database. The database must already
        be migrated (``alembic upgrade head`` / equivalent schema); ``seed_all``
        never creates or alters schema. Deterministic (fixed Faker seed,
        uuid5 ids from natural keys) and idempotent (re-run changes nothing).
        One transaction per tenant: a failure leaves no partial writes.
    ``class SeedError(Exception)``
        Raised with a clear message (mentioning migrations/alembic) when the
        target database has no schema.

``helpdesk.auth.passwords``
    ``def hash_password(password: str) -> str`` — argon2 hash (``$argon2...``).

Determinism comparison covers every column of every table INCLUDING ids,
except the infrastructure timestamps ``created_at``/``updated_at`` (they are
``server_default=now()`` from the Base mixin and thus wall-clock by design).
All seed-controlled datetimes (``granted_at``, ``expires_at``, ...) ARE compared.

Tenants are identified by company name ("Nordlicht" / "Cuan", fixed),
not by slug, so the implementer is free to pick tenant slugs.
"""

import asyncio
import dataclasses
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, Table, event, insert, inspect, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

import scripts.seed as seed_module
from helpdesk.shared.auth.passwords import hash_password
from helpdesk.shared.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.shared.models import (
    Base,
    Department,
    Grant,
    Role,
    RoleTemplate,
    SodRule,
    System,
    Tenant,
    TenantScoped,
    Ticket,
    User,
)
from helpdesk.shared.models.numbers import ReferenceCounter
from scripts.seed import SeedError, seed_all
from tests.conftest import TempDatabaseFactory

# Most tests here still take 1.5-4.9s (each seeds both tenants
# into a real database), so the whole module is slow and `make test-fast` skips it.
pytestmark = pytest.mark.slow

# --- The spec constants -------------------------------------------------------

EXPECTED_USERS = {"nordlicht": 220, "cuan": 180}  # ±10% tolerance below
EXPECTED_EMAIL_DOMAIN = {"nordlicht": "@nordlicht.example", "cuan": "@cuan.example"}
PRINCIPALS = (
    "employee",
    "manager",
    "system_owner",
    "hr",
    "it_admin",
    "auditor",
    "platform_admin",
)

# PINNED SAMPLE — determinism guard against Faker/version drift.
# Pinned 2026-09-03 from the first green run with faker==40.38.0; NEVER
# regenerate. If this fails, Faker output or the generator drifted — fix the
# drift (repin faker), don't repin the sample.
PINNED_SAMPLE: dict[str, str] = {
    "adriane.karge@nordlicht.example": "Adriane Karge",
    "sophie.gude@nordlicht.example": "Sophie Gude",
    "jorn.schweitzer@nordlicht.example": "Jörn Schweitzer",
    "faith.steed@cuan.example": "Faith Steed",
    "jesse.gaughan@cuan.example": "Jesse Gaughan",
    "cal.colligan@cuan.example": "Cal Colligan",
}

# Anomaly band: ~5% of active grants are planted stale/non-standard. The band is
# wide enough to absorb legitimate non-template grants (owners/admins) but tight
# enough that "no anomalies" or "everything anomalous" both fail.
ANOMALY_FRACTION_MIN = 0.02
ANOMALY_FRACTION_MAX = 0.15

_INFRA_COLUMNS = frozenset({"created_at", "updated_at"})

# The most SQL statements one `seed_all` may send, on an empty
# database and on a re-seed alike (before the batching: 5,985 and 2,993).
MAX_SEED_STATEMENTS = 100


# --- helpers -----------------------------------------------------------------


@asynccontextmanager
async def _engine(url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(url)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _create_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@contextmanager
def _count_statements() -> Iterator[list[str]]:
    """Record every statement sent to Postgres while the block runs.

    Listens on the `Engine` class, not on one engine, because `seed_all`
    builds its own engine inside. The listener is removed on exit.
    """
    sent: list[str] = []

    def _record(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        sent.append(statement)

    event.listen(Engine, "before_cursor_execute", _record)
    try:
        yield sent
    finally:
        event.remove(Engine, "before_cursor_execute", _record)


@pytest.fixture(scope="module")
def seeded_db_url(pg: TempDatabaseFactory) -> Iterator[str]:
    """One database seeded once, shared by the read-only checks of this file."""
    url = pg.create()

    async def setup() -> None:
        async with _engine(url) as engine:
            await _create_schema(engine)
        await seed_all(url)

    try:
        asyncio.run(setup())
        yield url
    finally:
        pg.drop(url)


async def _snapshot(engine: AsyncEngine) -> dict[str, list[tuple[object, ...]]]:
    """Full contents of every table, ordered by id, minus infra timestamps."""
    out: dict[str, list[tuple[object, ...]]] = {}
    async with engine.connect() as conn:
        table: Table
        for table in Base.metadata.sorted_tables:
            cols = [c for c in table.columns if c.name not in _INFRA_COLUMNS]
            rows = (await conn.execute(select(*cols).order_by(table.c.id))).all()
            out[table.name] = [tuple(row) for row in rows]
    return out


async def _tenant_ids(conn: AsyncConnection) -> dict[str, uuid.UUID]:
    """Map 'nordlicht'/'cuan' to tenant ids via the fixed company names."""
    rows = (await conn.execute(select(Tenant.id, Tenant.name))).all()
    assert len(rows) == 2, f"expected exactly two tenants, got {len(rows)}: {rows!r}"
    mapping: dict[str, uuid.UUID] = {}
    for tenant_id, name in rows:
        if "Nordlicht" in name:
            mapping["nordlicht"] = tenant_id
        elif "Cuan" in name:
            mapping["cuan"] = tenant_id
    assert set(mapping) == {"nordlicht", "cuan"}, f"unrecognised tenant names: {rows!r}"
    return mapping


@dataclass(frozen=True)
class TenantData:
    users: Sequence[tuple[uuid.UUID, uuid.UUID | None, str, str, list[str], str]]
    # (id, department_id, email, password_hash, principals, display_name)
    departments: Sequence[tuple[uuid.UUID, uuid.UUID | None]]  # (id, manager_user_id)
    systems: Sequence[tuple[uuid.UUID, str, uuid.UUID | None]]  # (id, slug, owner_user_id)
    roles: Sequence[uuid.UUID]
    sod_pairs: Sequence[tuple[uuid.UUID, uuid.UUID]]
    grants: Sequence[tuple[uuid.UUID, uuid.UUID, str, datetime | None]]
    # (user_id, role_id, status, expires_at)
    templates: Sequence[tuple[uuid.UUID, uuid.UUID]]  # (department_id, role_id)
    open_tickets: int


async def _tenant_data(conn: AsyncConnection, tenant_id: uuid.UUID) -> TenantData:
    users = [
        (r[0], r[1], r[2], r[3], list(r[4]), r[5])
        for r in (
            await conn.execute(
                select(
                    User.id,
                    User.department_id,
                    User.email,
                    User.password_hash,
                    User.principals,
                    User.display_name,
                ).where(User.tenant_id == tenant_id)
            )
        ).all()
    ]
    departments = [
        (r[0], r[1])
        for r in (
            await conn.execute(
                select(Department.id, Department.manager_user_id).where(
                    Department.tenant_id == tenant_id
                )
            )
        ).all()
    ]
    systems = [
        (r[0], r[1], r[2])
        for r in (
            await conn.execute(
                select(System.id, System.slug, System.owner_user_id).where(
                    System.tenant_id == tenant_id
                )
            )
        ).all()
    ]
    roles = list(
        (await conn.execute(select(Role.id).where(Role.tenant_id == tenant_id))).scalars().all()
    )
    sod_pairs = [
        (r[0], r[1])
        for r in (
            await conn.execute(
                select(SodRule.role_a_id, SodRule.role_b_id).where(SodRule.tenant_id == tenant_id)
            )
        ).all()
    ]
    grants = [
        (r[0], r[1], r[2], r[3])
        for r in (
            await conn.execute(
                select(Grant.user_id, Grant.role_id, Grant.status, Grant.expires_at).where(
                    Grant.tenant_id == tenant_id
                )
            )
        ).all()
    ]
    templates = [
        (r[0], r[1])
        for r in (
            await conn.execute(
                select(RoleTemplate.department_id, RoleTemplate.role_id).where(
                    RoleTemplate.tenant_id == tenant_id
                )
            )
        ).all()
    ]
    open_tickets = len(
        (
            await conn.execute(
                select(Ticket.id).where(Ticket.tenant_id == tenant_id, Ticket.status == "open")
            )
        )
        .scalars()
        .all()
    )
    return TenantData(
        users=users,
        departments=departments,
        systems=systems,
        roles=roles,
        sod_pairs=sod_pairs,
        grants=grants,
        templates=templates,
        open_tickets=open_tickets,
    )


# --- tests -------------------------------------------------------------------


def test_deterministic_idempotent(fresh_db_url: str, pg: TempDatabaseFactory) -> None:
    """Two seeds from empty produce identical rows (ids included); a re-run
    over already-seeded data changes nothing."""
    second_url = pg.create()

    async def scenario() -> None:
        async with _engine(fresh_db_url) as engine_a, _engine(second_url) as engine_b:
            await _create_schema(engine_a)
            await _create_schema(engine_b)

            await seed_all(fresh_db_url)
            await seed_all(second_url)

            snap_a = await _snapshot(engine_a)
            snap_b = await _snapshot(engine_b)
            assert snap_a["tenants"], "seed produced no tenants"
            assert snap_a["users"], "seed produced no users"
            assert snap_a == snap_b, "two seeds from empty must produce identical rows"

            # Idempotency: a third run over existing data is a no-op.
            await seed_all(second_url)
            snap_b_again = await _snapshot(engine_b)
            assert snap_b_again == snap_b, "re-running the seed over existing data changed rows"

    try:
        asyncio.run(scenario())
    finally:
        pg.drop(second_url)


def test_counts_and_shape(seeded_db_url: str) -> None:
    async def scenario() -> None:
        async with _engine(seeded_db_url) as engine, engine.connect() as conn:
            tenants = await _tenant_ids(conn)
            for key, tenant_id in tenants.items():
                data = await _tenant_data(conn, tenant_id)
                expected = EXPECTED_USERS[key]
                lo, hi = int(expected * 0.9), int(expected * 1.1)
                assert lo <= len(data.users) <= hi, (
                    f"{key}: expected ~{expected} users (band {lo}..{hi}), got {len(data.users)}"
                )

                assert 20 <= len(data.roles) <= 30, (
                    f"{key}: expected ~25 roles, got {len(data.roles)}"
                )
                assert len(data.sod_pairs) >= 6, (
                    f"{key}: expected >=6 SoD rules, got {len(data.sod_pairs)}"
                )

                # Exactly one owner per system, and the owner is a real
                # tenant user holding the system_owner principal.
                users_by_id = {u[0]: u for u in data.users}
                for system_id, slug, owner_id in data.systems:
                    assert owner_id is not None, f"{key}: system {slug} has no owner"
                    owner = users_by_id.get(owner_id)
                    assert owner is not None, (
                        f"{key}: system {slug} owner {owner_id} is not a tenant user"
                    )
                    assert "system_owner" in owner[4], (
                        f"{key}: owner of {slug} lacks the system_owner principal"
                    )
                    del system_id  # id only needed for the row shape

                # One manager per department, member of that department.
                for department_id, manager_id in data.departments:
                    assert manager_id is not None, (
                        f"{key}: department {department_id} has no manager"
                    )
                    manager = users_by_id.get(manager_id)
                    assert manager is not None, (
                        f"{key}: department {department_id} manager is not a tenant user"
                    )
                    assert manager[1] == department_id, (
                        f"{key}: manager {manager[2]} is not in the department they manage"
                    )
                    assert "manager" in manager[4], (
                        f"{key}: manager {manager[2]} lacks the manager principal"
                    )

                # At least one user per principal.
                held = {p for u in data.users for p in u[4]}
                missing = set(PRINCIPALS) - held
                assert not missing, f"{key}: no user holds principal(s) {sorted(missing)}"

                assert data.open_tickets >= 1, f"{key}: expected a few open tickets, got none"

    asyncio.run(scenario())


def test_planted_anomalies(seeded_db_url: str) -> None:
    async def scenario() -> None:
        async with _engine(seeded_db_url) as engine, engine.connect() as conn:
            tenants = await _tenant_ids(conn)
            now = datetime.now(UTC)
            for key, tenant_id in tenants.items():
                data = await _tenant_data(conn, tenant_id)

                # Latent SoD conflicts: one user actively holding both
                # roles of a seeded sod_rules pair (either order).
                active_roles_by_user: dict[uuid.UUID, set[uuid.UUID]] = {}
                for user_id, role_id, status, _expires in data.grants:
                    if status == "active":
                        active_roles_by_user.setdefault(user_id, set()).add(role_id)
                conflicts = {
                    (user_id, role_a, role_b)
                    for role_a, role_b in data.sod_pairs
                    for user_id, roles in active_roles_by_user.items()
                    if role_a in roles and role_b in roles
                }
                assert len(conflicts) >= 3, (
                    f"{key}: expected >=3 latent SoD conflicts "
                    f"(grants joined against sod_rules), found {len(conflicts)}"
                )

                # ~5% stale or non-standard active grants.
                template_roles: dict[uuid.UUID, set[uuid.UUID]] = {}
                for department_id, role_id in data.templates:
                    template_roles.setdefault(department_id, set()).add(role_id)
                dept_by_user = {u[0]: u[1] for u in data.users}

                active = [g for g in data.grants if g[2] == "active"]
                assert active, f"{key}: seed produced no active grants"
                anomalous = 0
                for user_id, role_id, _status, expires_at in active:
                    stale = expires_at is not None and expires_at < now
                    dept = dept_by_user.get(user_id)
                    standard = dept is not None and role_id in template_roles.get(dept, set())
                    if stale or not standard:
                        anomalous += 1
                fraction = anomalous / len(active)
                assert ANOMALY_FRACTION_MIN <= fraction <= ANOMALY_FRACTION_MAX, (
                    f"{key}: expected ~5% stale/non-standard active grants "
                    f"({ANOMALY_FRACTION_MIN:.0%}..{ANOMALY_FRACTION_MAX:.0%}), "
                    f"got {fraction:.1%} ({anomalous}/{len(active)})"
                )

    asyncio.run(scenario())


def test_obviously_fake(seeded_db_url: str) -> None:
    async def scenario() -> None:
        async with _engine(seeded_db_url) as engine, engine.connect() as conn:
            tenants = await _tenant_ids(conn)
            emails_to_names: dict[str, str] = {}
            for key, tenant_id in tenants.items():
                data = await _tenant_data(conn, tenant_id)
                domain = EXPECTED_EMAIL_DOMAIN[key]
                for _id, _dept, email, password_hash, _principals, name in data.users:
                    assert email.endswith(domain), (
                        f"{key}: email {email!r} does not end with {domain!r}"
                    )
                    assert password_hash.startswith("$argon2"), (
                        f"{key}: {email} has a non-argon2 password hash"
                    )
                    emails_to_names[email] = name

            # The auth helper produces the same hash family the seed stores.
            assert hash_password("pinned-check-only").startswith("$argon2")

            # Determinism pin: a fixed sample of generated identities must
            # match. Empty means it was never pinned — fail loudly rather
            # than pass vacuously (see PINNED_SAMPLE comment).
            assert PINNED_SAMPLE, (
                "PINNED_SAMPLE is empty: pin three seeded email -> display_name "
                "pairs on the first implementation run, then never regenerate"
            )
            for email, expected_name in PINNED_SAMPLE.items():
                assert emails_to_names.get(email) == expected_name, (
                    f"pinned identity drifted: {email!r} -> "
                    f"{emails_to_names.get(email)!r}, expected {expected_name!r}"
                )

    asyncio.run(scenario())


def test_midrun_failure_writes_nothing(fresh_db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A crash mid-way through a tenant's writes leaves no
    partial tenant-scoped rows (one transaction per tenant), and a clean
    re-run afterwards completes normally.

    The two ``tenants`` root rows are the documented exception: they are
    committed in their own transaction before the per-tenant writes start.
    """
    # The fault lands on the users batch, which `_write_tenant` sends
    # after the departments batch inside the same tenant transaction.
    fault_fired = False
    departments_before_fault: list[uuid.UUID] = []
    original_upsert_many = seed_module._SeedRepository.upsert_many

    async def failing_upsert_many(
        self: seed_module._SeedRepository[TenantScoped],
        rows: Mapping[uuid.UUID, Mapping[str, object]],
        /,
    ) -> dict[uuid.UUID, TenantScoped]:
        nonlocal fault_fired
        if self._model is User:
            fault_fired = True
            written = await self._session.execute(select(Department.id))
            departments_before_fault.extend(written.scalars().all())
            raise RuntimeError("injected mid-seed failure")
        return await original_upsert_many(self, rows)

    async def scenario() -> None:
        async with _engine(fresh_db_url) as engine:
            await _create_schema(engine)

            monkeypatch.setattr(seed_module._SeedRepository, "upsert_many", failing_upsert_many)
            with pytest.raises(RuntimeError, match="injected mid-seed failure"):
                await seed_all(fresh_db_url)
            assert fault_fired, "fault never fired; the test injected nothing"
            assert departments_before_fault, (
                "fixture premise: departments must be written in the tenant "
                "transaction before the users batch fails"
            )

            async with engine.connect() as conn:
                for table in Base.metadata.sorted_tables:
                    if table.name == "tenants":
                        continue
                    rows = (await conn.execute(select(table.c.id))).all()
                    assert rows == [], (
                        f"mid-run failure leaked {len(rows)} partial row(s) "
                        f"into {table.name!r}; the tenant transaction must roll back"
                    )

            # Recovery: with the fault removed, the same database seeds fully.
            monkeypatch.setattr(seed_module._SeedRepository, "upsert_many", original_upsert_many)
            await seed_all(fresh_db_url)
            snap = await _snapshot(engine)
            assert snap["users"], "re-run after a failed seed produced no users"

    asyncio.run(scenario())


def test_a_reseed_backfills_system_categories_and_changes_nothing_else(
    fresh_db_url: str,
) -> None:
    """A database seeded before the category
    column existed holds the migration's `other` server_default, and only a
    re-seed can supply the real values. Simulated by seeding, resetting every
    category to `other`, and re-running: the categories must come back and no
    other row may change (the one exception to insert-when-absent, stated in
    the seed's module docstring)."""

    async def scenario() -> None:
        async with _engine(fresh_db_url) as engine:
            await _create_schema(engine)
            await seed_all(fresh_db_url)
            before = await _snapshot(engine)

            async def categories(conn: AsyncConnection) -> set[str]:
                return set((await conn.execute(select(System.category))).scalars())

            async with engine.connect() as conn:
                assert await categories(conn) == {"other", "hris", "prod_infra"}, (
                    "fixture premise: the fresh seed writes all three categories"
                )

            async with engine.begin() as conn:
                await conn.execute(update(System).values(category="other"))
            async with engine.connect() as conn:
                assert await categories(conn) == {"other"}, (
                    "fixture premise: the reset must actually erase the categories"
                )

            await seed_all(fresh_db_url)
            after = await _snapshot(engine)
            assert after == before, (
                "the re-seed must restore the real categories and change nothing else"
            )

    asyncio.run(scenario())


def _phrases(about: str) -> list[str]:
    return [phrase.strip() for phrase in about.split(",") if phrase.strip()]


def test_every_system_has_phrases() -> None:
    """Catches a system left out of `system_about` (found by name only), a
    stray slug that matches no system, a value over the 512-character column,
    a single phrase (too thin to match on), and a phrase holding "and" (the
    split is on commas, so "and" would glue two ideas into one text)."""
    for spec in (seed_module.NORDLICHT, seed_module.CUAN):
        slugs = {slug for slug, _name, _sensitivity, _category in spec.systems}
        assert set(spec.system_about) == slugs, spec.slug
        for slug, about in spec.system_about.items():
            assert 1 <= len(about) <= 512, (spec.slug, slug, len(about))
            phrases = _phrases(about)
            assert len(phrases) >= 2, (spec.slug, slug, about)
            for phrase in phrases:
                assert " and " not in f" {phrase} ", (spec.slug, slug, phrase)


def test_tenant_spec_has_no_residency_field() -> None:
    """Catches a seed that still carries a residency value per tenant (it would
    be written to a column that no longer exists).

    The rebuild starts from a complete real spec, so the only thing that can
    raise `TypeError` is the unknown `residency` keyword, not a missing field.
    """
    assert "residency" not in {f.name for f in dataclasses.fields(seed_module.TenantSpec)}
    with pytest.raises(TypeError):
        dataclasses.replace(seed_module.NORDLICHT, residency="eu")  # type: ignore[call-arg]


@pytest.mark.db
def test_a_reseed_backfills_system_about(fresh_db_url: str) -> None:
    """Catches a re-seed that leaves `systems.about` empty on existing rows (an
    old database would then never find systems by meaning), one that writes the
    wrong tenant's phrases, and one that changes anything else: seed, erase
    every `about`, re-seed, and the whole database must equal the first seed."""

    async def scenario() -> None:
        async with _engine(fresh_db_url) as engine:
            await _create_schema(engine)
            await seed_all(fresh_db_url)
            before = await _snapshot(engine)

            async def abouts(conn: AsyncConnection) -> set[tuple[str, str, str]]:
                rows = await conn.execute(
                    select(Tenant.name, System.slug, System.about).join(
                        Tenant, Tenant.id == System.tenant_id
                    )
                )
                return {(str(name), str(slug), str(about)) for name, slug, about in rows}

            expected = {
                (spec.name, slug, about)
                for spec in (seed_module.NORDLICHT, seed_module.CUAN)
                for slug, about in spec.system_about.items()
            }
            async with engine.connect() as conn:
                assert await abouts(conn) == expected, (
                    "fixture premise: the first seed writes every spec value"
                )

            async with engine.begin() as conn:
                await conn.execute(update(System).values(about=""))
            async with engine.connect() as conn:
                assert {a for _n, _s, a in await abouts(conn)} == {""}, (
                    "fixture premise: the reset must actually erase the phrases"
                )

            await seed_all(fresh_db_url)

            async with engine.connect() as conn:
                assert await abouts(conn) == expected
            assert await _snapshot(engine) == before

    asyncio.run(scenario())


@pytest.mark.db
def test_a_reseed_keeps_a_counter_the_migration_made(fresh_db_url: str) -> None:
    """Catches a re-seed that looks for the ticket counter only by its fixed id:
    the numbers migration gave older databases counters with random ids, so the
    seed would add a second row for the same kind and break the unique key."""

    async def scenario() -> None:
        async with _engine(fresh_db_url) as engine:
            await _create_schema(engine)
            await seed_all(fresh_db_url)

            async def counters(conn: AsyncConnection) -> set[tuple[object, ...]]:
                rows = await conn.execute(
                    select(ReferenceCounter.tenant_id, ReferenceCounter.kind, ReferenceCounter.last)
                )
                return {tuple(row) for row in rows}

            async with engine.connect() as conn:
                before = await counters(conn)
            async with engine.begin() as conn:
                for (tenant_id,) in (
                    await conn.execute(select(ReferenceCounter.tenant_id).distinct())
                ).all():
                    await conn.execute(
                        update(ReferenceCounter)
                        .where(ReferenceCounter.tenant_id == tenant_id)
                        .values(id=uuid.uuid4())
                    )

            await seed_all(fresh_db_url)

            async with engine.connect() as conn:
                assert await counters(conn) == before

    asyncio.run(scenario())


def test_unmigrated_db_fails_loudly(fresh_db_url: str) -> None:
    """Seeding a database with no schema raises SeedError with a message that
    points at migrations, and leaves the database completely untouched."""

    async def scenario() -> None:
        with pytest.raises(SeedError, match=r"(?i)migrat|alembic|schema"):
            await seed_all(fresh_db_url)

        # No partial writes and no sneaky schema creation: still zero tables.
        async with _engine(fresh_db_url) as engine, engine.connect() as conn:
            tables = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
            assert tables == [], (
                f"seed against an unmigrated DB must not create or write anything, "
                f"found tables: {tables!r}"
            )

    asyncio.run(scenario())


def test_a_seed_sends_at_most_100_statements(fresh_db_url: str) -> None:
    """One seed on an empty migrated database sends at most 100
    statements. Catches a writer that still reads and flushes one row at a
    time (5,985 statements before the batching)."""

    async def scenario() -> None:
        async with _engine(fresh_db_url) as engine:
            await _create_schema(engine)
        with _count_statements() as sent:
            await seed_all(fresh_db_url)
        assert len(sent) <= MAX_SEED_STATEMENTS, (
            f"one seed sent {len(sent)} statements; at most {MAX_SEED_STATEMENTS} allowed"
        )

    asyncio.run(scenario())


def test_a_reseed_sends_at_most_100_statements(fresh_db_url: str) -> None:
    """A second seed over already-seeded data sends at most 100
    statements. Catches a writer that batches inserts but still reads each
    existing row alone (2,993 statements before the batching)."""

    async def scenario() -> None:
        async with _engine(fresh_db_url) as engine:
            await _create_schema(engine)
        await seed_all(fresh_db_url)
        with _count_statements() as sent:
            await seed_all(fresh_db_url)
        assert len(sent) <= MAX_SEED_STATEMENTS, (
            f"a re-seed sent {len(sent)} statements; at most {MAX_SEED_STATEMENTS} allowed"
        )

    asyncio.run(scenario())


def test_upsert_many_leaves_an_existing_row_untouched(fresh_db_url: str) -> None:
    """A user row already present with a changed display_name keeps
    that name after a re-seed. Catches a batch writer that overwrites existing
    rows (an ON CONFLICT DO UPDATE or a merge) instead of inserting only the
    missing ones."""
    changed_name = "Changed Fakename"

    async def scenario() -> None:
        async with _engine(fresh_db_url) as engine:
            await _create_schema(engine)
            await seed_all(fresh_db_url)

            async with engine.begin() as conn:
                user_id = (
                    await conn.execute(select(User.id).order_by(User.id).limit(1))
                ).scalar_one()
                await conn.execute(
                    update(User).where(User.id == user_id).values(display_name=changed_name)
                )

            await seed_all(fresh_db_url)

            async with engine.connect() as conn:
                name = (
                    await conn.execute(select(User.display_name).where(User.id == user_id))
                ).scalar_one()
            assert name == changed_name, (
                f"re-seed overwrote an existing user row: display_name is {name!r}"
            )

    asyncio.run(scenario())


def test_upsert_many_with_no_rows_sends_no_statement(fresh_db_url: str) -> None:
    """An empty batch sends zero statements and returns an empty map.
    Catches an upsert_many that runs its own existence query (an IN over no
    ids) instead of short-circuiting when it was given nothing to write."""
    tenant_id = uuid.uuid4()

    async def scenario() -> None:
        async with _engine(fresh_db_url) as engine:
            await _create_schema(engine)
            async with engine.begin() as conn:
                await conn.execute(
                    insert(Tenant).values(
                        id=tenant_id,
                        slug="emptybatch",
                        name="Emptybatch Test Co",
                        settings={},
                    )
                )
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant_id) as session:
                repo = seed_module._SeedRepository(session, User)
                with _count_statements() as sent:
                    result = await repo.upsert_many({})
            assert result == {}, f"an empty batch returned {result!r}"
            assert sent == [], f"an empty batch sent {len(sent)} statement(s): {sent!r}"

    asyncio.run(scenario())
