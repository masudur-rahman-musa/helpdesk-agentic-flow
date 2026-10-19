"""POST /admin/settings: the switch for the chat's tool steps box.

Calls the MCP tool with the caller's own token, so the access check and audit
apply. The tool is kept out of the agent's tool list, so only a person can
change it. The tenant comes from the token. The body is checked here by hand so
a bad one gets a typed 400, not FastAPI's untyped 422.
"""

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from pydantic import ValidationError

from helpdesk.agent_api.agent.keys import derive_tool_steps_key
from helpdesk.agent_api.core.deps import InvalidBodyError, request_claims
from helpdesk.agent_api.schemas.settings import SetToolStepsVisibleArgs, SetToolStepsVisibleBody
from helpdesk.agent_api.services.tool_proxy import proxy_tool

router = APIRouter()

SETTINGS_PATH = "/admin/settings"
SET_TOOL_STEPS_VISIBLE_TOOL = "set_tool_steps_visible"


@router.post(SETTINGS_PATH)
async def set_tool_steps_visible_route(request: Request) -> Mapping[str, Any]:
    """Show or hide the tool steps box for this tenant's chat.

    The key comes from the admin, the value and the minute, so a double click
    replays and a later change is new.
    """
    claims = request_claims(request)

    try:
        payload = await request.json()
        body = SetToolStepsVisibleBody.model_validate(payload)
    except (ValueError, ValidationError):
        raise InvalidBodyError("the settings body did not validate") from None

    arguments = SetToolStepsVisibleArgs(
        show_tool_steps=body.show_tool_steps,
        idempotency_key=derive_tool_steps_key(
            admin_user_id=uuid.UUID(claims.sub),
            visible=body.show_tool_steps,
            minute=datetime.now(UTC),
        ),
    )
    return await proxy_tool(request, SET_TOOL_STEPS_VISIBLE_TOOL, arguments)
