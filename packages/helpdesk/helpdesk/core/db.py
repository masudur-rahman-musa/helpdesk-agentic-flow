"""Async engine/session factory and the tenant isolation guard.

Every ORM SELECT, UPDATE, and DELETE that touches a `TenantScoped` model is
intercepted by a `do_orm_execute` hook: with no tenant bound it raises
`UnboundTenantQueryError` (never a silent full-table read); with one bound
it injects `tenant_id = :bound` into the statement via
`with_loader_criteria`. Bind a tenant with `tenant_session`.

The guard covers exactly those three statement shapes. INSERTs (via
`session.add` or `insert()`) and `text()` statements are NOT intercepted;
writes stay tenant-correct by convention: services only touch the DB
through the repository helpers, which stamp the bound tenant themselves.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from weakref import WeakKeyDictionary

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import ORMExecuteState, Session, with_loader_criteria

from helpdesk.models import TenantScoped

_TENANT_INFO_KEY = "helpdesk_tenant_id"


class UnboundTenantQueryError(RuntimeError):
    """An ORM query touched a tenant-scoped model with no tenant bound."""


def create_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


_SHARED_ENGINES: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, AsyncEngine]] = (
    WeakKeyDictionary()
)


def shared_engine(database_url: str) -> AsyncEngine:
    """A process-shared engine for the running event loop.

    Keyed by (event loop, URL) because asyncpg's pooled connections bind to
    the loop that created them: under a server that is one engine per
    process, while a test that drives each request through its own
    `asyncio.run` gets a per-loop engine instead of a poisoned pool. Entries
    drop with their loop (WeakKeyDictionary); nothing needs disposing by hand.
    """
    loop = asyncio.get_running_loop()
    engines = _SHARED_ENGINES.setdefault(loop, {})
    engine = engines.get(database_url)
    if engine is None:
        engine = create_engine(database_url)
        engines[database_url] = engine
    return engine


@asynccontextmanager
async def tenant_session(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> AsyncIterator[AsyncSession]:
    """A session whose every scoped query is filtered to `tenant_id`; commits on clean exit."""
    async with factory() as session:
        session.info[_TENANT_INFO_KEY] = tenant_id
        yield session
        await session.commit()


def bound_tenant_id(session: AsyncSession) -> uuid.UUID:
    """The tenant bound to this session; raises if there is none."""
    tenant_id = session.info.get(_TENANT_INFO_KEY)
    if not isinstance(tenant_id, uuid.UUID):
        raise UnboundTenantQueryError(
            "no tenant bound to this session; use tenant_session(factory, tenant_id)"
        )
    return tenant_id


def _touches_tenant_scoped(state: ORMExecuteState) -> bool:
    return any(issubclass(mapper.class_, TenantScoped) for mapper in state.all_mappers)


@event.listens_for(Session, "do_orm_execute")
def _enforce_tenant_scope(state: ORMExecuteState) -> None:
    if state.is_column_load or state.is_relationship_load:
        return
    if not (state.is_select or state.is_update or state.is_delete):
        return
    if not _touches_tenant_scoped(state):
        return

    tenant_id = state.session.info.get(_TENANT_INFO_KEY)
    if tenant_id is None:
        raise UnboundTenantQueryError(
            "query touches a tenant-scoped model but the session has no tenant bound; "
            "use tenant_session(factory, tenant_id)"
        )

    state.statement = state.statement.options(
        with_loader_criteria(
            TenantScoped,
            lambda cls: cls.tenant_id == tenant_id,
            include_aliases=True,
            track_closure_variables=True,
        )
    )
