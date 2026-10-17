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
tests can redirect the route through environment variables alone. All auth
logic lives in `agent_api.services.auth`; this file is the HTTP wiring only. Raises `LoginRefusedError` (rendered as 401 by the
app-level handler in `router.py`) or `ToolError` (rendered via STATUS_FOR_CODE).
"""

from fastapi import APIRouter

from helpdesk.agent_api.schemas.auth import LoginRequest, LoginResponse
from helpdesk.agent_api.services import auth as auth_service
from helpdesk.shared.core.config import load_settings

router = APIRouter()


@router.post("/auth/login", response_model=LoginResponse)
async def login(body: LoginRequest) -> LoginResponse:
    return await auth_service.login(body, load_settings())
