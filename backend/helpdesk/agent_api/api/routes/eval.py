"""POST /admin/test/reset and POST /v1/eval/turn: the routes the companion
console's eval runner calls.

Both answer only while `EVAL_ROUTES_ENABLED` is true (tests and CI) and are a
typed 404 otherwise, so production never runs a golden case. The console sends
no token. The turn takes its identity from a seat looked up in the seeded
data and a token minted for it, never from the body (`services/eval_turn.py`).
"""

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from helpdesk.agent_api.schemas.eval import EvalEnvelope, EvalTurnRequest
from helpdesk.agent_api.services import eval_turn
from helpdesk.shared.core.config import Settings, load_settings
from helpdesk.shared.core.errors import ErrorCode, NotFoundError

router = APIRouter()

PROMPT_VERSIONS_HEADER = "X-Prompt-Versions"
"""Carries each step's prompt digest, since the console's envelope allows no extra field."""


def _enabled_settings() -> Settings:
    settings = load_settings()
    if not settings.eval_routes_enabled:
        raise NotFoundError("the eval routes are switched off")
    return settings


@router.post("/admin/test/reset", status_code=204)
async def reset() -> Response:
    """Answer 204 and clear nothing: the audit table allows no partial cleanup,
    so `make eval` restores the database itself before the run."""
    _enabled_settings()
    return Response(status_code=204)


@router.post("/v1/eval/turn", response_model=EvalEnvelope)
async def turn(request: Request, body: EvalTurnRequest) -> Response:
    """Run one golden case and answer in the console's envelope."""
    settings = _enabled_settings()
    state = request.app.state
    seat = await eval_turn.resolve_seat(
        state.session_factory, tenant_slug=body.tenant, principal_user=body.principal_user
    )
    if seat is None:
        return JSONResponse(status_code=422, content={"code": ErrorCode.VALIDATION.value})
    outcome = await eval_turn.run_case(
        body=body,
        seat=seat,
        settings=settings,
        factory=state.session_factory,
        model=state.model,
        checkpointer=state.checkpointer,
    )
    return JSONResponse(
        content=outcome.envelope.model_dump(mode="json"),
        headers={PROMPT_VERSIONS_HEADER: outcome.prompt_versions},
    )
