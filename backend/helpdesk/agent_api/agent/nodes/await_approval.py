"""`await_approval`: the turn stops here until a person decides.

The state is already in Postgres, so another process can resume it. This
node decides and stores nothing: `interrupt()` drops its update and LangGraph
reruns it on resume. The paused reply is a template, not a model call.
"""

from langgraph.types import interrupt

from helpdesk.agent_api.agent.gates import approval_seat_count
from helpdesk.agent_api.agent.state import ToolResult, TurnOutcome, TurnState
from helpdesk.agent_api.agent.turn_deps import TurnDeps

PENDING_APPROVAL_REPLY = "Your request is filed and is waiting for {seats}."
"""Promises nothing, since approvers may reject."""

ONE_SEAT = "one approval"
"""The singular, so a one-seat chain does not read "waiting for 1 approvals"."""

MANY_SEATS = "{count} approvals"
"""Also the fallback, so no made-up number is shown."""


def pending_reply(state: TurnState) -> str:
    """The paused turn's message, built from the state alone."""
    count = approval_seat_count(state.tool_results)
    seats = ONE_SEAT if count == 1 else MANY_SEATS.format(count=count or "the required")
    return PENDING_APPROVAL_REPLY.format(seats=seats)


async def await_approval(state: TurnState, deps: TurnDeps) -> TurnState:
    """Pause on the request id; on resume, add the decision as a tool result.

    The decision arrives as JSON built from the stored request, so it is
    validated here. `DONE` is set because the next edge is unconditional.
    """
    del deps
    decision = interrupt(str(state.awaiting_request_id))
    return state.model_copy(
        update={
            "tool_results": (*state.tool_results, ToolResult.model_validate(decision)),
            "outcome": TurnOutcome.DONE,
        }
    )
