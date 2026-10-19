"""Test-only helpers exercising the expected API surface.

Expected implementation surface (the implementer must provide exactly this):

``helpdesk.models``
    ``Base`` - DeclarativeBase with naming convention; id/created_at/updated_at mixin applied
    ``TenantScoped`` - mixin adding ``tenant_id`` + composite index to scoped models
    Models: ``Tenant``, ``Department``, ``User``, ``System``, ``Role``,
    ``RoleTemplate``, ``SodRule``, ``Grant``, ``Ticket``

``helpdesk.core.db``
    ``create_engine(database_url: str) -> AsyncEngine``
    ``create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]``
    ``tenant_session(factory, tenant_id: uuid.UUID)`` - async context manager
        yielding an ``AsyncSession`` bound to that tenant; every ORM query on a
        tenant-scoped model is filtered to it; commits on clean exit
    ``UnboundTenantQueryError`` - raised when an ORM query touches a
        tenant-scoped model through a session with no tenant bound

Seed data is obviously fake (example.test emails, "Fake" names, fake hashes).
``grants.request_id`` stays ``None``: its FK target (``access_requests``)
lands in a later plan row, so the column must be a nullable UUID for now.
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from helpdesk.shared.core.db import create_engine, tenant_session
from helpdesk.shared.crud.numbers import next_number
from helpdesk.shared.models import (
    Base,
    Department,
    Grant,
    Role,
    RoleTemplate,
    SodRule,
    System,
    Tenant,
    Ticket,
    User,
)
from helpdesk.shared.models.numbers import NumberKind

TENANT_SCOPED_TABLES: tuple[str, ...] = (
    "departments",
    "users",
    "systems",
    "roles",
    "role_templates",
    "sod_rules",
    "grants",
    "tickets",
    "audit_events",
    "idempotency_keys",
    "access_requests",
    "access_reviews",
    "review_items",
    "policy_documents",
    "policy_chunks",
    "sessions",
    "messages",  # the child table carries tenant_id too
    "notifications",
    "audit_chain_anchors",
    "reference_counters",
)
ALL_TABLES: tuple[str, ...] = ("tenants", *TENANT_SCOPED_TABLES)


@dataclass(frozen=True)
class SeededTenant:
    """Ids of one fully populated tenant (one row per table, two roles for the SoD rule)."""

    tenant_id: uuid.UUID
    tenant_slug: str
    department_id: uuid.UUID
    user_id: uuid.UUID
    system_id: uuid.UUID
    role_id: uuid.UUID
    role_b_id: uuid.UUID
    role_template_id: uuid.UUID
    sod_rule_id: uuid.UUID
    grant_id: uuid.UUID
    ticket_id: uuid.UUID


@asynccontextmanager
async def schema_engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    """Engine with the full schema created from Base.metadata; drops it afterwards."""
    engine = create_engine(database_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


async def seed_tenant(factory: async_sessionmaker[AsyncSession], slug: str) -> SeededTenant:
    """Insert one row per directory-core table for a new tenant; returns all ids.

    The suite shares one database for the whole run, so the
    globally-unique ``tenants.slug`` gets a uuid suffix to keep every call's
    tenant distinct. ``slug`` stays the label for the human names (display
    name, emails, role slugs), which are unique per tenant and so never clash
    across tenants. Pinned by ``test_shared_db.py`` (a fresh tenant per test)
    and by every converted file that seeds two tenants with the same label.
    """
    tenant_slug = f"{slug}-{uuid.uuid4().hex[:8]}"
    async with factory() as session:
        tenant = Tenant(
            slug=tenant_slug,
            name=f"Fake Tenant {slug}",
            residency="eu",
            settings={},
        )
        session.add(tenant)
        await session.commit()
        tenant_id = tenant.id

    async with tenant_session(factory, tenant_id) as session:
        department = Department(tenant_id=tenant_id, name=f"dept-{slug}", manager_user_id=None)
        session.add(department)
        await session.flush()

        user = User(
            tenant_id=tenant_id,
            department_id=department.id,
            email=f"fake.person@{slug}.example.test",
            display_name=f"Fake Person ({slug})",
            password_hash="argon2id$obviously-fake-test-hash",
            principals=["employee"],
            status="active",
            start_date=date(2026, 1, 5),
            end_date=None,
        )
        session.add(user)
        await session.flush()

        system = System(
            tenant_id=tenant_id,
            slug=f"sys-{slug}",
            name=f"Fake System ({slug})",
            sensitivity="low",
            owner_user_id=user.id,
        )
        session.add(system)
        await session.flush()

        role_a = Role(
            tenant_id=tenant_id,
            system_id=system.id,
            slug=f"role-a-{slug}",
            name=f"Fake Reader ({slug})",
            privilege="read",
            default_expiry_days=90,
        )
        role_b = Role(
            tenant_id=tenant_id,
            system_id=system.id,
            slug=f"role-b-{slug}",
            name=f"Fake Admin ({slug})",
            privilege="admin",
            default_expiry_days=30,
        )
        session.add_all([role_a, role_b])
        await session.flush()

        role_template = RoleTemplate(
            tenant_id=tenant_id, department_id=department.id, role_id=role_a.id
        )
        sod_rule = SodRule(
            tenant_id=tenant_id,
            role_a_id=role_a.id,
            role_b_id=role_b.id,
            rationale="fake rationale for tests",
            policy_ref="POL-TEST-1",
        )
        grant = Grant(
            tenant_id=tenant_id,
            user_id=user.id,
            role_id=role_a.id,
            request_id=None,
            granted_by_user_id=None,
            granted_at=datetime(2026, 2, 1, 12, 0, tzinfo=UTC),
            expires_at=None,
            revoked_at=None,
            revoke_reason=None,
            status="active",
        )
        ticket = Ticket(
            tenant_id=tenant_id,
            user_id=user.id,
            kind="general",
            title=f"fake ticket ({slug})",
            body="fake body",
            status="open",
            session_id=None,
            number=await next_number(session, NumberKind.TICKET),
        )
        session.add_all([role_template, sod_rule, grant, ticket])
        await session.flush()

        seeded = SeededTenant(
            tenant_id=tenant_id,
            tenant_slug=tenant_slug,
            department_id=department.id,
            user_id=user.id,
            system_id=system.id,
            role_id=role_a.id,
            role_b_id=role_b.id,
            role_template_id=role_template.id,
            sod_rule_id=sod_rule.id,
            grant_id=grant.id,
            ticket_id=ticket.id,
        )
    return seeded
