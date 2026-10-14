"""The login logic the route calls: tenant/user lookups and the audit emit.

Pulled out of `auth_routes` by an earlier change. `append_event` is a module global
here so `_emit`'s call reads it, which is why a test stubbing the failing
audit write patches `agent_api.services.auth.append_event`.
"""

import uuid

from fastapi.responses import JSONResponse

from helpdesk.shared.auth.passwords import hash_password
from helpdesk.shared.core.db import create_session_factory, shared_engine, tenant_session
from helpdesk.shared.crud.tenants import find_tenant_by_slug
from helpdesk.shared.crud.users import find_user_by_email
from helpdesk.shared.models import Tenant, User
from helpdesk.shared.services.audit.writer import AuditEventInput, append_event

# Verified on the miss paths so their timing matches a real password check.
# Random per process: it only ever has to fail.
_DUMMY_HASH = hash_password(uuid.uuid4().hex)

# One typed body for every refusal; free text would leak why login failed.
_REFUSAL_BODY = {"code": "FORBIDDEN"}
_ERROR_BODY = {"code": "INTERNAL"}


def _refuse() -> JSONResponse:
    return JSONResponse(status_code=401, content=_REFUSAL_BODY)


async def _find_tenant(database_url: str, tenant_slug: str) -> Tenant | None:
    factory = create_session_factory(shared_engine(database_url))
    async with factory() as session:
        return await find_tenant_by_slug(session, tenant_slug)


async def _find_user(database_url: str, tenant_id: uuid.UUID, email: str) -> User | None:
    factory = create_session_factory(shared_engine(database_url))
    async with tenant_session(factory, tenant_id) as session:
        return await find_user_by_email(session, email)


async def _emit(
    database_url: str, tenant_id: uuid.UUID, event_type: str, user: User | None
) -> None:
    """Append one auth event to the tenant's chain in its own transaction.

    The payload stays empty: everything worth recording about a login fits
    the row's own columns, and a free-form body is where credentials and
    identity leak into an append-only log.
    """
    factory = create_session_factory(shared_engine(database_url))
    async with tenant_session(factory, tenant_id) as session:
        await append_event(
            session,
            tenant_id,
            AuditEventInput(
                actor_kind="user",
                actor_ref=None if user is None else str(user.id),
                event_type=event_type,
                subject_type="user",
                subject_id=None if user is None else str(user.id),
                payload={},
            ),
        )
