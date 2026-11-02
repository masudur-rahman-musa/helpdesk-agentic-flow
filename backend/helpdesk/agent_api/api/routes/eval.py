"""POST /admin/test/reset and POST /v1/eval/conversation: the routes the companion
console's eval engine calls.

Both answer only while `EVAL_ROUTES_ENABLED` is true (tests and CI) and are a
typed 404 otherwise, so production never runs a conversation case. The console
sends no token. A conversation takes its identity from a seat looked up in the
seeded data and a token minted for it, never from the body
(`services/eval_turn.py`).
"""

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from helpdesk.agent_api.schemas.eval import (
    EvalConversationAnswer,
    EvalConversationRequest,
)
from helpdesk.agent_api.services import eval_turn
from helpdesk.shared.core.config import Settings, load_settings
from helpdesk.shared.core.errors import ErrorCode, NotFoundError

router = APIRouter()

PROMPT_VERSIONS_HEADER = "X-Prompt-Versions"
"""Carries each step's prompt digest, since the console's envelope allows no extra field."""

CHAT_ENGINE_HEADER = "X-Chat-Engine"
"""Names the engine that played the case, so a score is filed under the right one."""


def _enabled_settings() -> Settings:
    settings = load_settings()
    if not settings.eval_routes_enabled:
        raise NotFoundError("the eval routes are switched off")
    return settings


@router.post("/admin/test/reset", status_code=204)
async def reset() -> Response:
    """Answer 204 and clear nothing: the audit table allows no partial cleanup,
    so `make eval-chat` restores the database itself before the run."""
    _enabled_settings()
    return Response(status_code=204)


@router.post("/v1/eval/conversation", response_model=EvalConversationAnswer)
async def conversation(request: Request, body: EvalConversationRequest) -> Response:
    """Play one turn of a conversation case and answer the chat id, the reply,
    the turn's tool calls with their results, and the world before and after.
    A chat the seat did not start is refused, and nothing is played."""
    settings = _enabled_settings()
    state = request.app.state
    seat = await eval_turn.resolve_seat(
        state.session_factory, tenant_slug=body.tenant, principal_user=body.principal_user
    )
    if seat is None:
        return JSONResponse(status_code=422, content={"code": ErrorCode.VALIDATION.value})
    outcome = await eval_turn.run_conversation(
        body=body,
        seat=seat,
        settings=settings,
        factory=state.session_factory,
        model=state.model,
    )
    if outcome is None:
        return JSONResponse(status_code=422, content={"code": ErrorCode.VALIDATION.value})
    return JSONResponse(
        content=outcome.answer.model_dump(mode="json"),
        headers={
            PROMPT_VERSIONS_HEADER: outcome.prompt_versions,
            CHAT_ENGINE_HEADER: outcome.engine,
        },
    )
