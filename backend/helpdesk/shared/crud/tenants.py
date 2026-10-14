"""Tenant-root reads: the scoping root itself, not a tenant-scoped row.

Tenants are the scoping root rather than tenant-scoped rows, so `Repository`
cannot fetch them; these helpers exist so services still never build their
own queries.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.models import Tenant


async def find_tenant_by_slug(session: AsyncSession, slug: str) -> Tenant | None:
    """Resolve a tenant by its slug, or None.

    Tenants are the scoping root, not tenant-scoped rows, so `Repository`
    cannot fetch them; this helper exists so services still never build
    their own queries (login resolves the slug before any tenant is bound).
    """
    result = await session.execute(select(Tenant).where(Tenant.slug == slug))
    return result.scalars().one_or_none()


async def find_tenant_by_id(session: AsyncSession, tenant_id: uuid.UUID) -> Tenant | None:
    """Resolve a tenant by its id, or None.

    The sibling of `find_tenant_by_slug`, and here for the same reason: a
    tenant is the scoping root rather than a tenant-scoped row, so
    `Repository` cannot fetch one and a service would otherwise build the
    statement itself.

    The caller passes the id off a verified token, never off an argument. `search_policy` uses it to turn `identity.tenant_id` into
    the slug its collection name is built from; pinned by
    `test_mcp_search_policy.py::test_the_collection_searched_is_the_tokens_and_an_extra_tenant_id_is_dropped`,
    whose caller supplies the OTHER seeded tenant's id as an argument and
    still searches its own collection.
    """
    result = await session.execute(select(Tenant).where(Tenant.id == tenant_id))
    return result.scalars().one_or_none()


async def list_tenant_ids(session: AsyncSession) -> Sequence[uuid.UUID]:
    """Every tenant id, oldest slug first.

    The third of the tenant-root helpers, here for the same reason as its two
    siblings: a tenant is not a tenant-scoped row, so `Repository` cannot
    reach one and the scheduler would otherwise build the statement itself.
    It is the ONLY read in this codebase that is deliberately not scoped to
    one tenant — the scheduler's tick has no caller and no token, and its
    whole job is to visit every tenant in turn and open a bound session for
    each. Nothing tenant-scoped is read here: the ids alone.

    Ordered by slug so two ticks visit the tenants in the same order, which
    is what makes a failure in the middle of a sweep reproducible.
    """
    result = await session.execute(select(Tenant.id).order_by(Tenant.slug))
    return list(result.scalars().all())
