"""Resume a paused turn from a decided request, and nothing else.

The caller names only a request id. Everything else is read from Postgres:
the request row (tenant-scoped, so another tenant's request is not found),
its status (so a caller cannot turn a rejection into a grant), its session
(which is the checkpoint thread), and the paused checkpoint, which must be
waiting on this request. Every refusal happens before the run is touched, so
the real decision can still resume it later. No framework imports here.
"""

import uuid
from typing import Any

from helpdesk.agent_api.agent.checkpointer import thread_config
from helpdesk.agent_api.agent.gates import decision_result
from helpdesk.agent_api.agent.graph import Saver, TurnDeps, resume_graph
from helpdesk.agent_api.agent.state import TurnOutcome, TurnState
from helpdesk.agent_api.agent.turn_metrics import record_turn_state
from helpdesk.shared.core.errors import ErrorCode, NotFoundError, ToolError
from helpdesk.shared.crud import Repository
from helpdesk.shared.models.access import AccessRequest

__all__ = ["ResumeRefusedError", "resume_turn"]

_OUTCOME_KEY = "outcome"
_AWAITING_KEY = "awaiting_request_id"


class ResumeRefusedError(ToolError):
    """A resume this module refuses, before anything runs.

    One error for every refusal, so a caller with a wrong id cannot learn whether
    a request is pending, already resumed, or never paused.
    """

    code = ErrorCode.VALIDATION


async def _paused_channel_values(checkpointer: Saver, thread_id: str) -> dict[str, Any] | None:
    """The thread's latest saved values, or `None`. Raw, so a state that fails
    validation for an unrelated reason refuses instead of raising."""
    saved = await checkpointer.aget_tuple(thread_config(thread_id))
    if saved is None:
        return None
    return dict(saved.checkpoint["channel_values"])


async def resume_turn(
    *,
    deps: TurnDeps,
    request_id: uuid.UUID,
    checkpointer: Saver,
) -> TurnState:
    """Resume the run that filed this request, acting on its recorded decision.

    `deps.db` is bound to the caller's tenant, so the read is the tenant check.
    A still-pending request is refused before any checkpoint is read.
    """
    request = await Repository(deps.db, AccessRequest).get(request_id)
    if request is None:
        raise NotFoundError(f"no access request {request_id} in this tenant")

    decision = decision_result(status=request.status, request_id=request_id)
    if decision is None:
        raise ResumeRefusedError(
            f"access request {request_id} is {request.status}, not a recorded decision"
        )

    if request.session_id is None:
        raise ResumeRefusedError(
            f"access request {request_id} was filed outside a turn and has no thread"
        )

    thread_id = str(request.session_id)
    values = await _paused_channel_values(checkpointer, thread_id)
    if values is None or str(values.get(_OUTCOME_KEY)) != TurnOutcome.AWAIT_APPROVAL.value:
        raise ResumeRefusedError(f"thread {thread_id} has no paused checkpoint to resume")

    awaiting = values.get(_AWAITING_KEY)
    if awaiting is None or str(awaiting) != str(request_id):
        raise ResumeRefusedError(
            f"thread {thread_id} is not waiting on access request {request_id}"
        )

    resumed = await resume_graph(
        deps=deps,
        thread_id=thread_id,
        decision=decision,
        checkpointer=checkpointer,
    )
    # Counted here, not in a node: LangGraph reruns the pausing node, so a
    # counter there would count the pause again on every decision.
    record_turn_state(resumed)
    return resumed
