"""Seed both tenants: `uv run python scripts/seed.py`.

The specs, generator and writer live in `helpdesk.shared.services.seed`; the
names are imported back here so `scripts.seed.<name>` still works.
"""

import asyncio

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from helpdesk.shared.core.config import load_settings
from helpdesk.shared.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.shared.models import Tenant
from helpdesk.shared.services.seed import (
    _FILLER_PASSWORD,
    CUAN,
    NORDLICHT,
    PRINCIPALS,
    TENANTS,
    GrantRow,
    PlantedConflict,
    PlantedRequest,
    PlantedRequestRow,
    RoleSpec,
    SeedError,
    SodRuleSpec,
    TenantRows,
    TenantSpec,
    TicketSpec,
    UserRow,
    _SeedRepository,
    _write_tenant,
    generate_tenant,
)

__all__ = [
    "CUAN",
    "NORDLICHT",
    "PRINCIPALS",
    "TENANTS",
    "_FILLER_PASSWORD",
    "GrantRow",
    "PlantedConflict",
    "PlantedRequest",
    "PlantedRequestRow",
    "RoleSpec",
    "SeedError",
    "SodRuleSpec",
    "TenantRows",
    "TenantSpec",
    "TicketSpec",
    "UserRow",
    "_SeedRepository",
    "generate_tenant",
    "main",
    "seed_all",
]


async def _check_migrated(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        async with engine.connect() as conn:
            tables = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
    finally:
        await engine.dispose()
    required = {"tenants", "departments", "users", "grants"}
    if not required <= tables:
        raise SeedError(
            f"database at {database_url.rsplit('@', 1)[-1]} is missing the helpdesk schema "
            f"(tables absent: {sorted(required - tables)}); run the migrations first: "
            "uv run alembic upgrade head"
        )


async def _write_tenants_table(
    factory: async_sessionmaker[AsyncSession], all_rows: list[TenantRows]
) -> None:
    async with factory() as session:
        for rows in all_rows:
            if await session.get(Tenant, rows.tenant_id) is None:
                session.add(
                    Tenant(
                        id=rows.tenant_id,
                        slug=rows.spec.slug,
                        name=rows.spec.name,
                        settings={},
                    )
                )
        await session.commit()


async def seed_all(database_url: str) -> None:
    """Seed both tenants; deterministic, idempotent, one transaction per tenant."""
    await _check_migrated(database_url)
    all_rows = [generate_tenant(spec) for spec in TENANTS]
    engine = create_engine(database_url)
    try:
        factory = create_session_factory(engine)
        await _write_tenants_table(factory, all_rows)
        for rows in all_rows:
            async with tenant_session(factory, rows.tenant_id) as session:
                await _write_tenant(session, rows)
    finally:
        await engine.dispose()


def main() -> None:
    database_url = load_settings().database_url
    asyncio.run(seed_all(database_url))
    total_users = sum(
        spec.users_per_department[i] for spec in TENANTS for i in range(len(spec.departments))
    )
    print(f"seeded {len(TENANTS)} tenants, {total_users} users (idempotent; safe to re-run)")


if __name__ == "__main__":
    main()
