"""The admin audit routes: chain verify and log search.

An earlier change merges the two modules that held these — `admin_routes` (verify) and
`audit_routes` (search) — into one file under `api/routes/admin/`.

An earlier change thins this file to HTTP wiring only. The verify logic
(RBAC check, chain walk) lives in `agent_api.services.audit`; the search
proxies through `tool_proxy.proxy_tool`, which raises ToolError on failure
and returns the tool body on success. The app-level handler in `router.py`
renders all ToolErrors as HTTP responses.

GET /admin/audit/verify: is this tenant's audit chain intact?

Integrity is only worth reporting to someone entitled to see it, so the
endpoint is gated from the start rather than left open
until the admin UI lands. The tenant walked is the one in the verified
token, never a parameter: a caller cannot ask about another tenant's chain. An unverifiable token and a verified token without the right
principal are different answers on purpose: 401 says "prove who you are",
403 says "you are known and not permitted". The 401 is `RequireAuthMiddleware`'s
now, which turns away every request with no valid token before this
route runs, so the route reads the verified claims off `request.state` and only
decides the 403. Both carry the typed body, never free text.

GET /admin/audit/search: the audit log, filtered, over HTTP.

`search_audit` is an earlier change's tool and nothing in the browser could reach it.
The search route is the wire and nothing else: the call crosses the MCP
boundary, so RBAC, the kill switch, the per-row scope filter and the audit
event apply to an auditor's search made from a page exactly as they do to one
made in a turn (23a decision A). Everything here is a READ, so a project rule has
nothing to bite on. **Identity comes from the token and from nowhere else**. The caller's own bearer is forwarded verbatim by
`proxy_tool`, and no route here takes a user or a tenant argument. Pinned
by `test_audit_routes.py::test_the_search_route_sends_the_eight_filters_and_no_identity_argument`,
which asserts the arguments as an exact key set, so a field riding along fails
the case too.

**The eight filters are the TOOL's to judge, not this router's** (section 5's
first risk). Every value crosses as the browser sent it and `SearchAuditArgs`
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

from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Query, Request

from helpdesk.agent_api.core.deps import request_claims
from helpdesk.agent_api.schemas.audit import SearchAuditArgs
from helpdesk.agent_api.services import audit as services_audit
from helpdesk.agent_api.services.tool_proxy import proxy_tool
from helpdesk.shared.core.config import load_settings
from helpdesk.shared.audit.chain import VerifyResult

router = APIRouter()

SEARCH_AUDIT_TOOL = "search_audit"


@router.get("/admin/audit/verify", response_model=VerifyResult)
async def verify_audit_chain(request: Request) -> VerifyResult:
    """RBAC check then chain walk; raises ToolError rendered by the one edge."""
    claims = request_claims(request)
    return await services_audit.verify_chain(claims, load_settings().database_url)


@router.get("/admin/audit/search")
async def search_audit_events(
    request: Request,
    event_type: str | None = Query(default=None),
    subject_type: str | None = Query(default=None),
    subject_id: str | None = Query(default=None),
    actor_ref: str | None = Query(default=None),
    occurred_from: str | None = Query(default=None),
    occurred_to: str | None = Query(default=None),
    before_seq: int | None = Query(default=None),
    limit: int | None = Query(default=None),
) -> Mapping[str, Any]:
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
    return await proxy_tool(request, SEARCH_AUDIT_TOOL, arguments)
