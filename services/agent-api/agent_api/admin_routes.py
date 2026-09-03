"""GET /admin/audit/verify: is this tenant's audit chain intact?

Integrity is only worth reporting to someone entitled to see it, so the
endpoint is gated from the start rather than left open
until the admin UI lands. The tenant walked is the one in the verified
token, never a parameter: a caller cannot ask about another tenant's chain.

An unverifiable token and a verified token without the right principal are
different answers on purpose: 401 says "prove who you are", 403 says "you
are known and not permitted". Both carry the typed body, never free text.

The audit log is not one of the MCP tools in the the design matrix, so this
route names its own principal set rather than calling `authorize`; it reuses
the enum so an unrecognised principal string cannot widen access.
"""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse
from helpdesk.audit.verify import verify_tenant_chain
from helpdesk.auth.jwt import TokenClaims, TokenVerificationError, verify_token
from helpdesk.auth.rbac import Principal
from helpdesk.core.db import create_session_factory, shared_engine, tenant_session
from helpdesk.core.settings import load_settings
from sqlalchemy.exc import SQLAlchemyError

router = APIRouter()

# the design: reading the log's integrity is an audit/administration act.
AUDIT_VERIFY_PRINCIPALS = frozenset(
    {Principal.AUDITOR.value, Principal.IT_ADMIN.value, Principal.PLATFORM_ADMIN.value}
)

_REFUSAL_BODY = {"code": "FORBIDDEN"}
_ERROR_BODY = {"code": "INTERNAL"}


def _bearer(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def _claims(authorization: str | None, secret: str) -> TokenClaims | None:
    token = _bearer(authorization)
    if token is None:
        return None
    try:
        return verify_token(token, secret, datetime.now(UTC))
    except TokenVerificationError:
        return None


def _is_permitted(claims: TokenClaims) -> bool:
    """Principal strings outside the enum are ignored, never a wildcard."""
    return any(principal in AUDIT_VERIFY_PRINCIPALS for principal in claims.principals)


@router.get("/admin/audit/verify")
async def verify_audit_chain(authorization: str | None = Header(default=None)) -> JSONResponse:
    settings = load_settings()

    claims = _claims(authorization, settings.jwt_secret)
    if claims is None:
        return JSONResponse(status_code=401, content=_REFUSAL_BODY)
    if not _is_permitted(claims):
        return JSONResponse(status_code=403, content=_REFUSAL_BODY)

    # Every response on this route carries a typed code, including the
    # failure ones: a malformed tenant claim or an unreachable database
    # would otherwise return FastAPI's untyped 500 body.
    try:
        tenant_id = UUID(claims.tenant_id)
        factory = create_session_factory(shared_engine(settings.database_url))
        async with tenant_session(factory, tenant_id) as session:
            result = await verify_tenant_chain(session, tenant_id)
    # OSError as well as SQLAlchemyError: a refused connection reaches here
    # unwrapped, and that is exactly the outage this branch exists for.
    except (ValueError, OSError, SQLAlchemyError):
        return JSONResponse(status_code=500, content=_ERROR_BODY)
    return JSONResponse(content=result.model_dump())
