"""Resume the paused turn after an approval or rejection decision.

Called by `api/routes/approvals.py` right after the decision tool returns a
final result. The route gates the call on `gates.DECIDED_RESULTS`, so this
function only runs for `approved`, `executed`, `rejected` and `failed`; a
non-final `pending` seat never reaches here.

`request_id` comes from the PATH, not the tool body. The route passes it in so
a wrong or lying body cannot redirect the resume to a different request.

`ResumeRefusedError` is NOT caught here: a turn filed outside a chat has no
paused thread, so `resume_turn` refuses. The route wraps the call and swallows
that error so the approver still gets the decision's own 200 back. Anything
else propagates as a real failure.

Pattern: `services/chat.py` builds `TurnDeps` under `tenant_session` and runs
the graph; the same shape is used here for the resume.
"""

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
    """Resume the paused turn that filed this request, now that a decision is in.

    `request_id` is the PATH parameter, not any id from the tool body. `status`
    is read from the tool body only to decide whether to run; the route already
    checked that it is a decided status before calling here.

    `TurnDeps` is built from the lifespan resources on `request.app.state`,
    exactly as `chat_turn` does it. The token forwarded to the tool caller is
    the approver's own bearer: the only call that might happen during the resumed
    `compose_reply` step is to the LLM via `deps.model`, not to any MCP tool,
    but the `ToolCaller` is required by the graph interface.
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
