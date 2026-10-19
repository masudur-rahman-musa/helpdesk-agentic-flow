"""/admin/tickets: every open ticket in this tenant, and solving one.

Both call MCP tools with the caller's own token, so access checks, the kill
switch and audit apply. The tenant comes from the token. The tool's output is
returned unchanged.
"""

from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import ValidationError

from helpdesk.agent_api.core.deps import InvalidBodyError
from helpdesk.agent_api.schemas.tickets import (
    ListOpenTicketsArgs,
    ListOpenTicketsArgsWithLimit,
    SolveTicketArgs,
)
from helpdesk.agent_api.services.tool_proxy import proxy_tool

router = APIRouter()

TICKETS_PATH = "/admin/tickets"
LIST_OPEN_TICKETS_TOOL = "list_open_tickets"

SOLVE_PATH = "/admin/tickets/solve"
"""Its own path, so a read and a write never share one name."""

SOLVE_TICKET_TOOL = "solve_ticket"


@router.get(TICKETS_PATH)
async def open_tickets_board(
    request: Request, limit: int | None = Query(default=None)
) -> Mapping[str, Any]:
    """Open tickets, oldest first. An unset limit falls to the tool's default."""
    arguments = (
        ListOpenTicketsArgs() if limit is None else ListOpenTicketsArgsWithLimit(limit=limit)
    )
    return await proxy_tool(request, LIST_OPEN_TICKETS_TOOL, arguments)


@router.post(SOLVE_PATH)
async def solve_ticket_route(request: Request) -> Mapping[str, Any]:
    """Mark one open ticket solved.

    The body is checked here by hand so a bad one gets a typed 400, not FastAPI's
    untyped 422.
    """
    try:
        arguments = SolveTicketArgs.model_validate(await request.json())
    except (ValueError, ValidationError):
        raise InvalidBodyError("the solve body did not validate") from None

    return await proxy_tool(request, SOLVE_TICKET_TOOL, arguments)
