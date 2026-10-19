"""GET /admin/tickets: every open ticket in this tenant.

Calls the `list_open_tickets` MCP tool with the caller's own token, so access
checks, the kill switch and audit apply. The tenant comes from the token. The
tool's output is returned unchanged.
"""

from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Query, Request

from helpdesk.agent_api.schemas.tickets import ListOpenTicketsArgs, ListOpenTicketsArgsWithLimit
from helpdesk.agent_api.services.tool_proxy import proxy_tool

router = APIRouter()

TICKETS_PATH = "/admin/tickets"
LIST_OPEN_TICKETS_TOOL = "list_open_tickets"


@router.get(TICKETS_PATH)
async def open_tickets_board(
    request: Request, limit: int | None = Query(default=None)
) -> Mapping[str, Any]:
    """Open tickets, oldest first. An unset limit falls to the tool's default."""
    arguments = (
        ListOpenTicketsArgs() if limit is None else ListOpenTicketsArgsWithLimit(limit=limit)
    )
    return await proxy_tool(request, LIST_OPEN_TICKETS_TOOL, arguments)
