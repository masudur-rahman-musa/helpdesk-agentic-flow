"""`execute_tools`: the accepted round actually made (the design step 7).

Every `ValidatedCall` on the state, IN ORDER, through `deps.tools`. Order is
the plan: a classification before the request it justifies, a listing before
the thing it identifies, so a node that reordered the round would decide a
request against nothing. Pinned by
`test_agent_execution.py::test_a_round_of_two_makes_both_calls_in_order_and_records_both`.

**Arguments are rebuilt through the catalogue** before they go out. The
`ToolCaller` protocol takes a `BaseModel`, and the state carries the round as
JSON-mode dicts because it is checkpointed in an earlier change; `TOOL_INPUTS` is the
same table `validate_tool_calls` accepted them with, so nothing is re-decided
here and the derived `idempotency_key` travels exactly as written. Pinned by
`::test_the_call_goes_out_as_the_catalogues_own_model_carrying_the_derived_key`.

**`tool_calls_made` counts calls ATTEMPTED** (decision F), the failing one
included. The cap is a bound on work and on blast radius, and a mutation whose
call raised may still have landed on the server: counting only successes would
let a failing loop run without limit. Pinned from a non-zero start by
`::test_tool_calls_made_rises_by_the_number_of_calls_attempted`, which is what
tells adding from assigning.

**A typed failure is an answer, not an outage** (decision B). Seven of the
eight codes are decisions a person can act on, so the call is recorded with
`ok=False` and its code, the REMAINING calls of the round are dropped — a
round is a plan, and its second call usually depends on the first one's effect
— and the turn goes on to compose over what happened. The turn's own
`error_code` and `reply` stay untouched on this path: those are the fields that
say the TURN failed, and it did not. Pinned across all seven by
`::test_a_typed_failure_records_its_code_drops_the_rest_and_continues`, and
from the other side by
`::test_a_typed_failure_keeps_the_results_of_the_calls_that_already_ran`.

`INTERNAL` is the exception, because it is the one code that carries nothing
worth composing: the turn ends as state through `fail_turn`, with the templated
reply and no further call made
(`::test_an_internal_failure_ends_the_turn_and_makes_no_further_call`).

**The approval branch is decided here, one node before the pause**.
`gates.approval_outcome` reads the round's results and answers whether a filed
request went to an approval chain; `awaiting_request_id` is written from the
same payload. Both are written HERE rather than in `await_approval` because
LangGraph discards the interrupting node's own state update, so a value written
beside the `interrupt()` would never reach the checkpoint an earlier change resumes
from. Pinned out of Postgres by
`test_agent_approval_interrupt.py`'s
`::test_the_paused_state_is_readable_from_a_second_checkpointer`.

A decision with no usable request id ends the turn `FAILED` with the templated
reply instead, because a pause nobody can address is worse than a failure
somebody can see, in the same file's
`::test_a_decision_with_no_request_id_ends_the_turn_and_writes_no_paused_checkpoint`.

Nothing else happens here. No message row, no model call and no store of any
kind: every other effect of the turn belongs to a node the graph's edges can
see, and a write from inside the execution CYCLE would happen once per round
with nothing counting it. Pinned statically by
`::test_execute_tools_imports_no_store_and_no_gateway` and at runtime by every
case in that file handing the node a `TurnDeps` whose `db` and `model` are
`None`.
"""

from helpdesk.agent_api.graph.deps import TurnDeps
from helpdesk.agent_api.graph.gates import approval_outcome, approval_request_id
from helpdesk.agent_api.graph.nodes.extract_intent import FAILURE_REPLY
from helpdesk.agent_api.graph.state import ToolResult, TurnOutcome, TurnState
from helpdesk.agent_api.tools.catalogue import TOOL_INPUTS
from helpdesk.shared.errors import ErrorCode, ToolError


async def execute_tools(state: TurnState, deps: TurnDeps) -> TurnState:
    """Make the round's calls, record what each one answered, never raise.

    The results of this round are APPENDED to whatever earlier rounds recorded,
    because the planner is handed "tool results so far" and a round that
    replaced them would let it propose again what it already did.

    The round is CLEARED off `validated` on both paths as it is spent, so a
    call cannot be read twice: the cycle returns to `plan_tools`, and a state
    still carrying a made call is one a later reader could mistake for a call
    still owed.
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
