"""The login logic the route calls: tenant/user lookups and the audit emit.

Pulled out of `auth_routes` by an earlier change. `append_event` is a module global
here so `_emit`'s call reads it, which is why a test stubbing the failing
audit write patches `agent_api.services.auth.append_event`.

An earlier change adds `LoginRefusedError` and `login` so the route holds no auth
logic. The route calls `login`, which raises rather than returning responses.
"""

import uuid
from datetime import UTC, datetime

from helpdesk.agent_api.schemas.auth import LoginRequest, LoginResponse
from helpdesk.shared.auth.jwt import issue_token
from helpdesk.shared.auth.passwords import hash_password, verify_password
from helpdesk.shared.core.config import Settings
from helpdesk.shared.core.db import create_session_factory, shared_engine, tenant_session
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.crud.tenants import find_tenant_by_slug
from helpdesk.shared.crud.users import find_user_by_email
from helpdesk.shared.models import Tenant, User
from helpdesk.shared.services.audit.writer import AuditEventInput, append_event

# Verified on the miss paths so their timing matches a real password check.
# Random per process: it only ever has to fail.
_DUMMY_HASH = hash_password(uuid.uuid4().hex)


class LoginRefusedError(Exception):
    """Raised when a login attempt is refused.

    Wrong password, unknown email, unknown tenant, wrong-tenant credentials
    and leaver status all raise this so the response never reveals why.
    Rendered as a literal 401 {"code": "FORBIDDEN"} by the app-level handler
    in `router.py`.
    """


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


async def login(body: LoginRequest, settings: Settings) -> LoginResponse:
    """Verify credentials and return a JWT, or raise.

    Raises LoginRefusedError when the attempt is refused (wrong credentials,
    unknown tenant, leaver). Raises ToolError(INTERNAL) when the audit write
    fails — a login that succeeded unaudited is a hole in the guarantee. Both are rendered by the app-level handlers in
    `router.py`.

    The dummy argon2 hash is verified on the miss paths so timing cannot
    distinguish an unknown email from a wrong password.
    """
    tenant = await _find_tenant(settings.database_url, body.tenant_slug)
    if tenant is None:
        verify_password(body.password, _DUMMY_HASH)
        raise LoginRefusedError

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
        raise ToolError(ErrorCode.INTERNAL)

    if refused or user is None or user.department_id is None:
        raise LoginRefusedError

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
    return LoginResponse(access_token=token)
