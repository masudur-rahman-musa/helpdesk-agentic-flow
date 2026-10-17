"""Tenant reads. A tenant is not tenant-scoped, so `Repository` cannot fetch one."""

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.models import Tenant


async def find_tenant_by_slug(session: AsyncSession, slug: str) -> Tenant | None:
    """Used at login, before any tenant is bound."""
    result = await session.execute(select(Tenant).where(Tenant.slug == slug))
    return result.scalars().one_or_none()


async def find_tenant_by_id(session: AsyncSession, tenant_id: uuid.UUID) -> Tenant | None:
    """Callers pass the id from the verified token, never from a tool argument."""
    result = await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    return result.scalars().one_or_none()


async def list_tenant_ids(session: AsyncSession) -> Sequence[uuid.UUID]:
    """Every tenant id, by slug: the one read that spans tenants on purpose.

    The scheduler has no token and visits each tenant in turn. A fixed order
    makes a failure mid-sweep repeatable.
    """
    result = await session.execute(select(Tenant.id).order_by(Tenant.slug))
    return list(result.scalars().all())
