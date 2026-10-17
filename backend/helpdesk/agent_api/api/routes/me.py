"""GET /me, GET /me/requests and GET /me/access: the reads the web shows.

These call the same MCP tools the agent uses, so access checks, the kill
switch and audit apply to the browser exactly as to the model. The caller's
own token is forwarded; no route takes a user argument. The body is the tool's
output, unchanged.
"""

from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Query, Request

from helpdesk.agent_api.schemas.me import (
    GetMeArgs,
    ListMyAccessArgs,
    ListMyRequestsArgs,
    ListMyRequestsArgsWithLimit,
)
from helpdesk.agent_api.services.tool_proxy import proxy_tool
from helpdesk.shared.auth.jwt import TokenClaims
from helpdesk.shared.auth.rbac import tabs_for

router = APIRouter()


@router.get("/me/requests")
async def my_requests(
    request: Request,
    status: str | None = Query(default=None),
    limit: int | None = Query(default=None),
) -> Mapping[str, Any]:
    """Requests this caller filed, newest first.

    An on-behalf request belongs to the manager who filed it. Unset filters
    fall to the tool's defaults.
    """
    arguments = (
        ListMyRequestsArgs(status=status)
        if limit is None
        else ListMyRequestsArgsWithLimit(status=status, limit=limit)
    )
    return await proxy_tool(request, "list_my_requests", arguments)


@router.get("/me/access")
async def my_access(request: Request) -> Mapping[str, Any]:
    """Every grant this caller holds right now. One person's grants, so no paging."""
    return await proxy_tool(request, "list_my_access", ListMyAccessArgs())


def _tabs(claims: TokenClaims) -> dict[str, list[str]]:
    return {"tabs": tabs_for(claims.principals)}


@router.get("/me")
async def me(request: Request) -> Mapping[str, Any]:
    """Who the caller is, and which web tabs they may see.

    Tabs are only for display; each page's tool still checks access.
    """
    return await proxy_tool(request, "get_me", GetMeArgs(), extend=_tabs)
