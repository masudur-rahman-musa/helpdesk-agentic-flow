"""GET /me/requests and GET /me/access: the two reads the web pages render.

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
the tools answer with. `STATUS_FOR_CODE` below is this router's own map and
deliberately not `chat_routes`'.

No lifespan resource is touched: these two routes read nothing off
`app.state`, which is why their tests enter no lifespan and open no socket.
"""

from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse
from helpdesk.core.settings import load_settings
from helpdesk.errors import ErrorCode, ToolError
from pydantic import BaseModel

from agent_api.deps import bearer, build_tool_caller, claims

router = APIRouter()

STATUS_FOR_CODE: Mapping[ErrorCode, int] = {
    ErrorCode.VALIDATION: 400,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.TOOL_DISABLED: 503,
}
"""The HTTP status each typed code these two reads can answer with.

`FORBIDDEN` is **403** here and not the 401 `chat_routes.STATUS_FOR_CODE`
carries, because on this router the two facts are different ones: a caller who
did not prove who they are gets a literal 401 below, and a FORBIDDEN can then
only have come from the RBAC matrix, which is a 403. Pinned, as a pair with
the missing `INTERNAL` row, by
`test_me_routes.py::test_the_status_map_dresses_forbidden_as_403_and_not_as_chats_401`.

`INTERNAL` has no row on purpose: an unreachable MCP server is a failure the
caller cannot act on, so it falls through to 500 carrying the code and nothing
of the client's own message (the host and the path are internal detail).
`TOOL_DISABLED` is 503 because an admin tripping the kill switch is exactly
"this read is unavailable right now", not a permission decision. Everything
else falls through to 500.
"""


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


def _typed(code: ErrorCode) -> JSONResponse:
    """One typed body, dressed in this code's status. Never free text."""
    return JSONResponse(status_code=STATUS_FOR_CODE.get(code, 500), content={"code": code.value})


async def _call(authorization: str | None, tool: str, arguments: BaseModel) -> JSONResponse:
    """Verify the caller, call one read tool under their own bearer, render it.

    The token check runs BEFORE the caller is built, so a request with no
    usable bearer opens no MCP session at all — pinned by the three 401 cases
    in `test_me_routes.py`, each of which scripts a caller with no answers so
    entering it fails the case outright rather than showing up as a count.

    Absent, malformed, forged and expired all answer the same literal 401 with
    `FORBIDDEN`: a caller learns they are not authenticated, never which of the
    four it was. That status is written here rather than read from
    `STATUS_FOR_CODE`, whose `FORBIDDEN` row is the matrix's 403.

    A tool refusal crosses as its own typed code
    (`::test_a_tool_refusal_answers_its_own_typed_code`); an unreachable server
    raises `RemoteInternalError`, which is a `ToolError` too and falls through
    the map to 500 carrying the code alone
    (`::test_an_unreachable_mcp_server_is_one_typed_internal_naming_neither_host_nor_path`).
    """
    settings = load_settings()
    token = bearer(authorization)
    if claims(token, settings.jwt_secret) is None or token is None:
        return JSONResponse(status_code=401, content={"code": ErrorCode.FORBIDDEN.value})

    try:
        async with build_tool_caller(settings, token) as tools:
            payload: Mapping[str, Any] = await tools.call_tool(tool, arguments)
    except ToolError as exc:
        return _typed(exc.code)
    return JSONResponse(status_code=200, content=dict(payload))


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
    return await _call(authorization, "list_my_requests", arguments)


@router.get("/me/access")
async def my_access(authorization: str | None = Header(default=None)) -> JSONResponse:
    """Every grant this caller holds right now.

    Uncapped, deliberately: these rows are one person's live
    grants, bounded by reality rather than by a parameter, so there is no page
    size to ask for and no argument to send.
    """
    return await _call(authorization, "list_my_access", ListMyAccessArgs())
