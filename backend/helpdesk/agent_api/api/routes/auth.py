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
tests can redirect the route through environment variables alone. The lookups
and the audit emit live in `agent_api.services.auth`.
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from helpdesk.agent_api.schemas.auth import LoginRequest, LoginResponse
from helpdesk.agent_api.services.auth import (
    _DUMMY_HASH,
    _ERROR_BODY,
    _emit,
    _find_tenant,
    _find_user,
    _refuse,
)
from helpdesk.shared.auth.jwt import issue_token
from helpdesk.shared.auth.passwords import verify_password
from helpdesk.shared.core.config import load_settings

router = APIRouter()


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
