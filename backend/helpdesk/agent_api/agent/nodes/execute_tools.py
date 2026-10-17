"""`execute_tools`: run the accepted round's calls, in order.

Order is the plan: a classification comes before the request it supports.
Arguments are rebuilt through the same input models that validated them.
Every attempted call counts toward the cap, failed ones too, since a failed
change may still have landed.

A typed failure is an answer: it is recorded, the rest of the round is
dropped (later calls usually depend on earlier ones), and the turn replies
about it. Only `INTERNAL` ends the turn.

The approval pause is decided here, one node early, because LangGraph drops
the pausing node's own update. No rows and no model calls here: a write in
this loop would happen once per round with nothing counting it.
"""

from helpdesk.agent_api.agent.gates import approval_outcome, approval_request_id
from helpdesk.agent_api.agent.nodes.extract_intent import FAILURE_REPLY
from helpdesk.agent_api.agent.state import ToolResult, TurnOutcome, TurnState
from helpdesk.agent_api.agent.tool_args import TOOL_INPUTS
from helpdesk.agent_api.agent.turn_deps import TurnDeps
from helpdesk.shared.core.errors import ErrorCode, ToolError


async def execute_tools(state: TurnState, deps: TurnDeps) -> TurnState:
    """Make the round's calls and record each answer. Never raises.

    Results are added to earlier rounds' so the planner sees everything done.
    `validated` is cleared so a made call cannot be mistaken for one still owed.
    """
    results: list[ToolResult] = list(state.tool_results)
    attempted = 0

    for call in state.validated:
        arguments = TOOL_INPUTS[call.tool].model_validate(call.arguments)
        attempted += 1
        try:
            payload = await deps.tools.call_tool(call.tool, arguments)
        except ToolError as exc:
            if exc.code is ErrorCode.INTERNAL:
                return state.model_copy(
                    update={
                        "tool_results": tuple(results),
                        "tool_calls_made": state.tool_calls_made + attempted,
                        "outcome": TurnOutcome.FAILED,
                        "error_code": exc.code,
                        "reply": FAILURE_REPLY,
                        "validated": (),
                    }
                )
            results.append(ToolResult(tool=call.tool, ok=False, error_code=exc.code))
            break
        results.append(ToolResult(tool=call.tool, ok=True, payload=dict(payload)))

    update: dict[str, object] = {
        "tool_results": tuple(results),
        "tool_calls_made": state.tool_calls_made + attempted,
        "outcome": approval_outcome(results),
        "validated": (),
    }
    if update["outcome"] is TurnOutcome.AWAIT_APPROVAL:
        update["awaiting_request_id"] = approval_request_id(results)
    elif update["outcome"] is TurnOutcome.FAILED:
        update["error_code"] = ErrorCode.INTERNAL
        update["reply"] = FAILURE_REPLY
    return state.model_copy(update=update)
