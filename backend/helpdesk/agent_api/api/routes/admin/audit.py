"""The admin audit routes: chain verify and log search.

An earlier change merges the two modules that held these — `admin_routes` (verify) and
`audit_routes` (search) — into one file under `api/routes/admin/`.

GET /admin/audit/verify: is this tenant's audit chain intact?

Integrity is only worth reporting to someone entitled to see it, so the
endpoint is gated from the start rather than left open
until the admin UI lands. The tenant walked is the one in the verified
token, never a parameter: a caller cannot ask about another tenant's chain. An unverifiable token and a verified token without the right
principal are different answers on purpose: 401 says "prove who you are",
403 says "you are known and not permitted". Both carry the typed body, never
free text. The audit log is not one of the MCP tools in the the design matrix,
so the verify route names its own principal set rather than calling
`authorize`; it reuses the enum so an unrecognised principal string cannot
widen access.

GET /admin/audit/search: the audit log, filtered, over HTTP.

`search_audit` is an earlier change's tool and nothing in the browser could reach it.
The search route is the wire and nothing else: the call crosses the MCP
boundary, so RBAC, the kill switch, the per-row scope filter and the audit
event apply to an auditor's search made from a page exactly as they do to one
made in a turn (23a decision A). Everything here is a READ, so a project rule has
nothing to bite on. **Identity comes from the token and from nowhere else**. The caller's own bearer is forwarded verbatim by
`call_tool_route`, and no route here takes a user or a tenant argument. Pinned
by `test_audit_routes.py::test_the_search_route_sends_the_eight_filters_and_no_identity_argument`,
which asserts the arguments as an exact key set, so a field riding along fails
the case too.

**The eight filters are the TOOL's to judge, not this router's** (section 5's
first risk). Every value crosses as the browser sent it and `SearchAuditInput`
— whose four string caps are read off the `audit_events` columns themselves —
is the only validator. A second opinion here would refuse an over-long filter,
an out-of-range page size or an unparseable date with FastAPI's untyped 422,
where SPEC section 8.1's `VALIDATION` dressed as a 400 by the shared
`STATUS_FOR_CODE` is what an earlier change has a sentence for. Pinned at both ends of
every bound by
`test_audit_routes.py::test_a_string_filter_of_exactly_the_column_cap_is_accepted`,
`::test_a_string_filter_one_character_over_the_cap_is_a_typed_400_with_no_read`,
`::test_the_inclusive_ends_of_the_tools_page_size_are_accepted`,
`::test_a_page_size_outside_the_tools_bounds_is_a_typed_400_with_no_read`,
`::test_a_cursor_of_one_is_accepted`, `::test_a_cursor_of_zero_is_a_typed_400_with_no_read`
and `::test_a_non_iso_occurred_from_is_a_typed_400_and_never_fastapis_422`. That
is also why the two timestamps are `str` here and not `datetime`: a `datetime`
query parameter answers the untyped 422 for a value the tool would have refused
typed. **The body is the tool's structured output, unchanged**, for the same
reason the `/me` reads are. No lifespan resource is touched: these routes read
nothing off `app.state`, which is why their tests enter no lifespan and open no
socket.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from helpdesk.agent_api.schemas.audit import SearchAuditArgs
from helpdesk.agent_api.services.tool_proxy import call_tool_route
from helpdesk.shared.audit.verify import verify_tenant_chain
from helpdesk.shared.auth.jwt import TokenClaims, TokenVerificationError, verify_token
from helpdesk.shared.auth.rbac import Principal
from helpdesk.shared.core.db import create_session_factory, shared_engine, tenant_session
from helpdesk.shared.core.settings import load_settings

router = APIRouter()

# the design: reading the log's integrity is an audit/administration act.
AUDIT_VERIFY_PRINCIPALS = frozenset(
    {Principal.AUDITOR.value, Principal.IT_ADMIN.value, Principal.PLATFORM_ADMIN.value}
)

_REFUSAL_BODY = {"code": "FORBIDDEN"}
_ERROR_BODY = {"code": "INTERNAL"}

SEARCH_AUDIT_TOOL = "search_audit"


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


@router.get("/admin/audit/search")
async def search_audit_events(
    authorization: str | None = Header(default=None),
    event_type: str | None = Query(default=None),
    subject_type: str | None = Query(default=None),
    subject_id: str | None = Query(default=None),
    actor_ref: str | None = Query(default=None),
    occurred_from: str | None = Query(default=None),
    occurred_to: str | None = Query(default=None),
    before_seq: int | None = Query(default=None),
    limit: int | None = Query(default=None),
) -> JSONResponse:
    """This tenant's audit events, newest first, filtered by whatever was asked.

    All eight filters are forwarded, and each only when present (decision D):
    13b put them on the tool because an audit log is unusable without them,
    and this route is the search box's only caller. Which events this caller
    may see is the tool's scope filter, never this router's.
    """
    asked: dict[str, Any] = {
        "event_type": event_type,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "actor_ref": actor_ref,
        "occurred_from": occurred_from,
        "occurred_to": occurred_to,
        "before_seq": before_seq,
        "limit": limit,
    }
    arguments = SearchAuditArgs.model_validate(
        {name: value for name, value in asked.items() if value is not None}
    )
    return await call_tool_route(authorization, SEARCH_AUDIT_TOOL, arguments)
