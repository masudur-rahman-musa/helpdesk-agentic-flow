"""GET and POST /admin/killswitch: the board and the toggle.

Both call MCP tools with the caller's own token, so access checks, the
protected-tool guard and audit apply. The toggle tool is kept out of the
agent's tool list, so only a person can stop a tool. The tenant always comes
from the token. The body is checked here by hand so a bad one gets a typed 400,
not FastAPI's untyped 422. The tool's output is returned unchanged.
"""

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from pydantic import ValidationError

from helpdesk.agent_api.agent.keys import derive_kill_switch_key
from helpdesk.agent_api.core.deps import InvalidBodyError, request_claims
from helpdesk.agent_api.schemas.killswitch import (
    ListKillSwitchesArgs,
    SetKillSwitchArgs,
    SetKillSwitchBody,
)
from helpdesk.agent_api.services.tool_proxy import proxy_tool

router = APIRouter()

KILLSWITCH_PATH = "/admin/killswitch"
"""One path for both verbs: the board and the toggle are one page."""

LIST_KILL_SWITCHES_TOOL = "list_kill_switches"
SET_KILL_SWITCH_TOOL = "set_kill_switch"

SCOPE_GLOBAL = "global"
"""Sent on as is. Swapping it for the caller's tenant would leave others running."""

SCOPE_TENANT = "tenant"
"""The browser's word for "my own tenant", swapped for the token's tenant id.

So a tenant id never has to reach the browser at all.
"""


@router.get(KILLSWITCH_PATH)
async def kill_switch_board(request: Request) -> Mapping[str, Any]:
    """Every registered tool and the state of its two kill switches.

    Both layers are shown per row, so an admin who clears the tenant switch can
    see the global one still holds the tool down.
    """
    return await proxy_tool(request, LIST_KILL_SWITCHES_TOOL, ListKillSwitchesArgs())


@router.post(KILLSWITCH_PATH)
async def set_kill_switch_route(request: Request) -> Mapping[str, Any]:
    """Stop one tool, or let it run again, for this tenant or the whole platform.

    `enabled` describes the switch; `stopped` in the answer describes the tool.
    The key comes from the admin and the toggle, so a double click replays. A body
    that will not parse is the same typed 400 as one that fails the checks.
    """
    claims = request_claims(request)

    try:
        payload = await request.json()
        body = SetKillSwitchBody.model_validate(payload)
    except (ValueError, ValidationError):
        raise InvalidBodyError("the kill-switch body did not validate") from None

    scope = SCOPE_GLOBAL if body.scope == SCOPE_GLOBAL else claims.tenant_id
    arguments = SetKillSwitchArgs(
        tool_name=body.tool_name,
        enabled=body.enabled,
        scope=scope,
        reason=body.reason,
        idempotency_key=derive_kill_switch_key(
            admin_user_id=uuid.UUID(claims.sub),
            tool_name=body.tool_name,
            enabled=body.enabled,
            scope=scope,
            reason=body.reason,
            minute=datetime.now(UTC),
        ),
    )
    return await proxy_tool(request, SET_KILL_SWITCH_TOOL, arguments)
