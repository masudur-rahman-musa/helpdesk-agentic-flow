"""The audit verify logic the route calls: RBAC check and chain walk.

Extracted from `api/routes/admin/audit.py` by an earlier change so the route holds no
database or login code. `verify_tenant_chain` is a module global here so a test
can stub it without patching the shared library directly — pinned by
`test_audit_verify_route.py::test_a_database_failure_still_answers_with_a_typed_code`.
"""

import uuid

from sqlalchemy.exc import SQLAlchemyError

from helpdesk.shared.auth.jwt import TokenClaims
from helpdesk.shared.auth.rbac import Principal
from helpdesk.shared.core.db import create_session_factory, shared_engine, tenant_session
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.audit.chain import VerifyResult
from helpdesk.shared.audit.reader import verify_tenant_chain as _verify_tenant_chain

verify_tenant_chain = _verify_tenant_chain
"""The chain walk, reached through this module-level name so tests can stub it.

Every call inside this module goes through this name, so
`monkeypatch.setattr(services_audit, "verify_tenant_chain", boom)` replaces
every call without touching the shared library.
"""

_AUDIT_VERIFY_PRINCIPALS = frozenset(
    {Principal.AUDITOR.value, Principal.IT_ADMIN.value, Principal.PLATFORM_ADMIN.value}
)


async def verify_chain(claims: TokenClaims, database_url: str) -> VerifyResult:
    """RBAC check, then chain walk. Returns VerifyResult or raises ToolError.

    Raises ToolError(FORBIDDEN) when the caller holds no audit principal.
    Raises ToolError(INTERNAL) on a DB, OS or UUID parse failure so no
    untyped 500 escapes the one-edge handler in `router.py`.
    Pinned by `test_api_layering.py::test_a_forbidden_audit_verify_answers_403`.
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
