"""POST /auth/login: email/password in, HS256 JWT out.

Every refusal (wrong password, unknown email, wrong tenant slug, leaver)
returns the same typed 401 body so the response never reveals whether an
account exists. On the miss paths a dummy argon2 verification runs so
response time does not distinguish unknown email from wrong password.

Settings are loaded per request (same seam as `agent_api.readiness`) so
tests can redirect the route through environment variables alone. Audit
events for login land with the audit-log row.
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from helpdesk.auth.jwt import issue_token
from helpdesk.auth.passwords import hash_password, verify_password
from helpdesk.core.db import create_session_factory, shared_engine, tenant_session
from helpdesk.core.repository import find_tenant_by_slug, find_user_by_email
from helpdesk.core.settings import load_settings
from helpdesk.models import User
from pydantic import BaseModel

router = APIRouter()

# Verified on the miss paths so their timing matches a real password check.
# Random per process: it only ever has to fail.
_DUMMY_HASH = hash_password(uuid.uuid4().hex)

# One typed body for every refusal; free text would leak why login failed.
_REFUSAL_BODY = {"code": "FORBIDDEN"}


class LoginRequest(BaseModel):
    tenant_slug: str
    email: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


def _refuse() -> JSONResponse:
    return JSONResponse(status_code=401, content=_REFUSAL_BODY)


async def _find_user(database_url: str, tenant_slug: str, email: str) -> User | None:
    factory = create_session_factory(shared_engine(database_url))
    async with factory() as session:
        tenant = await find_tenant_by_slug(session, tenant_slug)
    if tenant is None:
        return None
    async with tenant_session(factory, tenant.id) as session:
        return await find_user_by_email(session, email)


@router.post("/auth/login", response_model=LoginResponse)
async def login(body: LoginRequest) -> JSONResponse:
    settings = load_settings()
    user = await _find_user(settings.database_url, body.tenant_slug, body.email)

    if user is None:
        verify_password(body.password, _DUMMY_HASH)
        return _refuse()
    if not verify_password(body.password, user.password_hash):
        return _refuse()
    # Status is checked after the password so the leaver path costs the same
    # as a normal login and its refusal stays indistinguishable.
    if user.status != "active" or user.department_id is None:
        return _refuse()

    token = issue_token(
        {
            "sub": str(user.id),
            "tenant_id": str(user.tenant_id),
            "department_id": str(user.department_id),
            "principals": list(user.principals),
            "sid": str(uuid.uuid4()),  # fresh per login; persisted at the sessions row
        },
        settings.jwt_secret,
        datetime.now(UTC),
    )
    return JSONResponse(content=LoginResponse(access_token=token).model_dump())
