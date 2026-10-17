"""Resume the paused chat turn once a request gets a final decision."""

import uuid
from datetime import UTC, datetime

from fastapi import Request

from helpdesk.agent_api.agent.resume import resume_turn
from helpdesk.agent_api.agent.turn_deps import TurnDeps
from helpdesk.agent_api.core.deps import request_bearer, request_claims
from helpdesk.agent_api.services.tool_proxy import build_tool_caller
from helpdesk.shared.core.config import load_settings
from helpdesk.shared.core.db import tenant_session


async def resume_after_decision(
    request: Request,
    request_id: uuid.UUID,
    status: str,
) -> None:
    """Resume the paused turn that filed this request.

    `request_id` comes from the path, never the tool body, so a wrong body cannot
    point the resume at another request. A request filed outside a chat has no
    paused turn; that refusal is left for the route to swallow.
    """
    claims = request_claims(request)
    tenant_id = uuid.UUID(claims.tenant_id)
    token = request_bearer(request)

    async with tenant_session(request.app.state.session_factory, tenant_id) as db:
        await resume_turn(
            deps=TurnDeps(
                db=db,
                model=request.app.state.model,
                tools=build_tool_caller(load_settings(), token),
                now=lambda: datetime.now(UTC),
            ),
            request_id=request_id,
            checkpointer=request.app.state.checkpointer,
        )
