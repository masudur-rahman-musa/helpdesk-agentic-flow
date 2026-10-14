"""POST /auth/login: email/password in, HS256 JWT out.

Every refusal (wrong password, unknown email, wrong tenant slug, leaver)
returns the same typed 401 body so the response never reveals whether an
account exists. On the miss paths a dummy argon2 verification runs so
response time does not distinguish unknown email from wrong password.

Both outcomes are audited on the tenant's chain: `auth.login` on success,
`auth.failed` on a refusal whose tenant slug resolved. An unknown slug has
no chain to append to, so that attempt is deliberately unrecorded (an earlier change
decision E). That leaves one signal the dummy hash does not equalise: a
refusal under a real slug also does a lock, an insert and a commit, so
response time can distinguish an existing tenant from an invented one.
Accepted knowingly — slugs appear in login URLs and are not secret — and
noted here because the refusal bodies alone no longer tell the whole story.

A failing audit write fails the login (decision F): the log is a guarantee,
not telemetry, and a login that succeeded unaudited would be a hole in it.

Settings are loaded per request (same seam as `agent_api.readiness`) so
tests can redirect the route through environment variables alone.
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from helpdesk.shared.audit.writer import AuditEventInput, append_event
from helpdesk.shared.auth.jwt import issue_token
from helpdesk.shared.auth.passwords import hash_password, verify_password
from helpdesk.shared.core.db import create_session_factory, shared_engine, tenant_session
from helpdesk.shared.core.repository import find_tenant_by_slug, find_user_by_email
from helpdesk.shared.core.settings import load_settings
from helpdesk.shared.models import Tenant, User

router = APIRouter()

# Verified on the miss paths so their timing matches a real password check.
# Random per process: it only ever has to fail.
_DUMMY_HASH = hash_password(uuid.uuid4().hex)

# One typed body for every refusal; free text would leak why login failed.
_REFUSAL_BODY = {"code": "FORBIDDEN"}
_ERROR_BODY = {"code": "INTERNAL"}


class LoginRequest(BaseModel):
    tenant_slug: str
    email: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


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


@router.post("/auth/login", response_model=LoginResponse)
async def login(body: LoginRequest) -> JSONResponse:
    settings = load_settings()

    tenant = await _find_tenant(settings.database_url, body.tenant_slug)
    if tenant is None:
        verify_password(body.password, _DUMMY_HASH)
        return _refuse()

    user = await _find_user(settings.database_url, tenant.id, body.email)

    if user is None:
        verify_password(body.password, _DUMMY_HASH)
        refused = True
    else:
        password_ok = verify_password(body.password, user.password_hash)
        # Status is read only after the password check has already run, so
        # the leaver path costs the same as a normal login and its refusal
        # stays indistinguishable from a wrong password.
        refused = not password_ok or user.status != "active" or user.department_id is None

    try:
        await _emit(
            settings.database_url,
            tenant.id,
            "auth.failed" if refused else "auth.login",
            user,
        )
    except Exception:
        # Broad on purpose for now: nothing may reach the caller as an
        # untyped error, and with no logging module yet a narrower catch
        # would turn a programming error into an unhandled 500 instead of
        # this typed one. Row 20 adds the log line and the narrowing.
        return JSONResponse(status_code=500, content=_ERROR_BODY)

    if refused or user is None or user.department_id is None:
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
