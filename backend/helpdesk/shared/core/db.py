"""Async engine and session setup, plus the tenant isolation guard.

Every ORM SELECT, UPDATE and DELETE on a `TenantScoped` model is filtered to
the bound tenant, or raises when none is bound, so a query can never read
every tenant by mistake. INSERTs and `text()` are not covered: the repository
helpers stamp the tenant on writes themselves.
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

from helpdesk.shared.models import TenantScoped

_TENANT_INFO_KEY = "helpdesk_tenant_id"


class UnboundTenantQueryError(RuntimeError):
    """An ORM query touched a tenant-scoped model with no tenant bound."""


CONNECT_TIMEOUT_SECONDS = 10
"""Stops a Postgres that drops packets from hanging a request forever.

No statement timeout: it would also cut long reads like a full audit check."""


def create_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, connect_args={"timeout": CONNECT_TIMEOUT_SECONDS})


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


_SHARED_ENGINES: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, AsyncEngine]] = (
    WeakKeyDictionary()
)


def shared_engine(database_url: str) -> AsyncEngine:
    """A shared engine per (event loop, URL).

    asyncpg connections belong to the loop that made them, so each loop needs
    its own pool. A process that retires loops must call
    `dispose_shared_engines` inside each one, or its connections leak.
    """
    loop = asyncio.get_running_loop()
    engines = _SHARED_ENGINES.setdefault(loop, {})
    engine = engines.get(database_url)
    if engine is None:
        engine = create_engine(database_url)
        engines[database_url] = engine
    return engine


async def dispose_shared_engines() -> None:
    """Dispose the running loop's engines before the loop ends, so Postgres is not exhausted."""
    loop = asyncio.get_running_loop()
    for engine in _SHARED_ENGINES.pop(loop, {}).values():
        await engine.dispose()


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
