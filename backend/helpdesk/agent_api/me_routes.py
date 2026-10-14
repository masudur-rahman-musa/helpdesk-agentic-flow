"""GET /me, GET /me/requests and GET /me/access: the reads the web renders.

The same two MCP tools the agent already calls, reached over HTTP instead of
over a turn. Nothing here reads the database: the call
goes through the MCP boundary so RBAC, the kill switch and the audit event
apply to a read the browser makes exactly as they do to one the model makes,
and there is one read model rather than two.

**Identity comes from the token and from nowhere else**. The
caller's own bearer is forwarded verbatim to the MCP server, which decides
what that person may see; neither route takes a user argument, and the two
tools behind them declare none. Pinned by
`test_me_routes.py::test_the_access_route_answers_the_tools_output_unchanged`,
which asserts the scripted caller was handed the request's own token and that
no identity argument reached the tool.

**The body is the tool's structured output, unchanged.** A route that filtered
or renamed on the way out would be a second contract for 23b's pages to track,
and the widening in `mcp_server.tools.access` would stop being the one place
the shape is decided.

**Every refusal is a typed body from `helpdesk.errors`**, the same eight codes
the tools answer with. The map, the typed body and the call itself are
`agent_api.deps`' since an earlier change (decision C), shared with every other proxied
route and deliberately not `chat_routes`'. `STATUS_FOR_CODE` is re-exported
below so this module's own name still resolves.

No lifespan resource is touched: these two routes read nothing off
`app.state`, which is why their tests enter no lifespan and open no socket.
"""

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# `STATUS_FOR_CODE` is re-exported rather than used here: 23c moved the map
# into `deps`, and `me_routes.STATUS_FOR_CODE` is still the name
# `test_me_routes.py::test_the_status_map_dresses_forbidden_as_403_and_not_as_chats_401`
# reads it under.
from helpdesk.agent_api.deps import STATUS_FOR_CODE as STATUS_FOR_CODE
from helpdesk.agent_api.deps import call_tool_route
from helpdesk.shared.auth.jwt import TokenClaims
from helpdesk.shared.auth.rbac import tabs_for

router = APIRouter()


class ListMyRequestsArgs(BaseModel):
    """`list_my_requests` with no page size asked for.

    The tool's own `REQUESTS_LIMIT_DEFAULT` then applies. Repeating that number
    here would be a second place for it to drift from (a project rule wants
    one constant, pinned once), which is why the limited call is a SUBCLASS
    below rather than a nullable field: `model_dump` carries every declared
    field, so a `limit: int | None = None` would send `limit: null` to a tool
    whose bound does not accept it.
    """

    status: str | None = None
    """Passed through as the string it arrived as, so the TOOL's input model
    is the one validator that decides enum membership and answers the typed
    `VALIDATION` this router dresses as a 400. A second enum here would refuse
    the same value with FastAPI's own untyped 422."""


class ListMyRequestsArgsWithLimit(ListMyRequestsArgs):
    """`list_my_requests` with the caller's own page size."""

    limit: int


class ListMyAccessArgs(BaseModel):
    """No arguments: the grants listed are the token user's own."""


@router.get("/me/requests")
async def my_requests(
    authorization: str | None = Header(default=None),
    status: str | None = Query(default=None),
    limit: int | None = Query(default=None),
) -> JSONResponse:
    """Every request this caller has FILED, newest page of `limit` of them.

    Filtered by requester rather than by target, which is the tool's own rule:
    an on-behalf request belongs to the manager who filed it. Both query
    parameters are optional and neither is defaulted here — an absent `status`
    means every status, and an absent `limit` means the tool's own default, so
    a page cannot silently show one status as if it were all of them
    (`::test_the_requests_route_passes_no_status_and_no_filter_when_none_is_asked_for`).
    """
    arguments = (
        ListMyRequestsArgs(status=status)
        if limit is None
        else ListMyRequestsArgsWithLimit(status=status, limit=limit)
    )
    return await call_tool_route(authorization, "list_my_requests", arguments)


@router.get("/me/access")
async def my_access(authorization: str | None = Header(default=None)) -> JSONResponse:
    """Every grant this caller holds right now.

    Uncapped, deliberately: these rows are one person's live
    grants, bounded by reality rather than by a parameter, so there is no page
    size to ask for and no argument to send.
    """
    return await call_tool_route(authorization, "list_my_access", ListMyAccessArgs())


class GetMeArgs(BaseModel):
    """No arguments: the person is the token's own."""


def _tabs(claims: TokenClaims) -> dict[str, list[str]]:
    return {"tabs": tabs_for(claims.principals)}


@router.get("/me")
async def me(authorization: str | None = Header(default=None)) -> JSONResponse:
    """Who the caller is, and which web tabs their principals may see.

    The `get_me` tool's body plus `tabs`, computed from the token's own
    principals by `helpdesk.auth.rbac.tabs_for`, so the web holds no copy of
    the matrix. Cosmetic only: each page's tool still refuses. Pinned by
    `test_me_routes.py::test_the_me_route_answers_name_principals_and_the_tabs_of_the_token`.
    """
    return await call_tool_route(authorization, "get_me", GetMeArgs(), extend=_tabs)
