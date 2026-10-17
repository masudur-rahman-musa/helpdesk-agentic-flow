"""POST /auth/login: email and password in, a signed token out.

Every refusal returns the same 401 body, and a dummy hash check runs on the
miss paths, so neither the answer nor its timing shows whether an account
exists. Under a real tenant slug a refusal also writes an audit row, so timing
can tell a real slug from an invented one; slugs are in login URLs, so that is
accepted. A failed audit write fails the login, since the log is a promise.
"""

from fastapi import APIRouter

from helpdesk.agent_api.schemas.auth import LoginRequest, LoginResponse
from helpdesk.agent_api.services import auth as auth_service
from helpdesk.shared.core.config import load_settings

router = APIRouter()


@router.post("/auth/login", response_model=LoginResponse)
async def login(body: LoginRequest) -> LoginResponse:
    return await auth_service.login(body, load_settings())
