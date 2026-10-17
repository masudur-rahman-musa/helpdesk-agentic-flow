"""The audit chain check behind the admin route: access check, then chain walk."""

import uuid

from sqlalchemy.exc import SQLAlchemyError

from helpdesk.shared.auth.jwt import TokenClaims
from helpdesk.shared.auth.rbac import Principal
from helpdesk.shared.core.db import create_session_factory, shared_engine, tenant_session
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.audit.chain import VerifyResult
from helpdesk.shared.audit.reader import verify_tenant_chain as _verify_tenant_chain

verify_tenant_chain = _verify_tenant_chain
"""A module name so tests can stub the chain walk."""

_AUDIT_VERIFY_PRINCIPALS = frozenset(
    {Principal.AUDITOR.value, Principal.IT_ADMIN.value, Principal.PLATFORM_ADMIN.value}
)


async def verify_chain(claims: TokenClaims, database_url: str) -> VerifyResult:
    """Access check, then chain walk.

    Any database, network or id parse failure becomes a typed `INTERNAL`, so no
    untyped 500 leaks out.
    """
    if not any(p in _AUDIT_VERIFY_PRINCIPALS for p in claims.principals):
        exc = ToolError()
        exc.code = ErrorCode.FORBIDDEN
        raise exc

    try:
        tenant_id = uuid.UUID(claims.tenant_id)
        factory = create_session_factory(shared_engine(database_url))
        async with tenant_session(factory, tenant_id) as session:
            result = await verify_tenant_chain(session, tenant_id)
    # OSError as well as SQLAlchemyError: a refused connection arrives unwrapped.
    except (ValueError, OSError, SQLAlchemyError):
        raise ToolError(ErrorCode.INTERNAL)
    return result
