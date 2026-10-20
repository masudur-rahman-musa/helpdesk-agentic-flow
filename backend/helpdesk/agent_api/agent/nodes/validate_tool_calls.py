"""`validate_tool_calls`: accept the whole round, or refuse it whole.

Running the valid part of a round would leave a half-done change, like a
ticket about a request never filed. Three checks per call: the tool is
allowed for this intent (blocks a prompt-injected `offboard_user`), the
arguments fit its input model, and the model did not name its own
idempotency key. The agent derives the key instead. Pure: no database,
gateway or tool calls.

A round holding `create_access_request` is kept on `validated` and sent to
`ask_confirmation` until the person says yes.
"""

from typing import Any

from pydantic import ValidationError

from helpdesk.agent_api.agent import gates
from helpdesk.agent_api.agent.keys import derive_idempotency_key
from helpdesk.agent_api.agent.state import ProposedCall, TurnOutcome, TurnState, ValidatedCall
from helpdesk.agent_api.agent.tool_args import (
    IDEMPOTENCY_KEY,
    TOOL_INPUTS,
    needs_idempotency_key,
)
from helpdesk.agent_api.agent.turn_deps import TurnDeps


def _validated(
    proposal: ProposedCall, index: int, allowed: frozenset[str], state: TurnState
) -> ValidatedCall | None:
    """One proposal as the call it would become, or `None` if it may not be
    made."""
    if proposal.tool not in allowed:
        return None
    if IDEMPOTENCY_KEY in proposal.arguments:
        return None

    arguments: dict[str, Any] = dict(proposal.arguments)
    if needs_idempotency_key(proposal.tool):
        arguments[IDEMPOTENCY_KEY] = derive_idempotency_key(
            session_id=state.session_id,
            user_message=state.user_message,
            round_number=state.round_number,
            index=index,
            tool_name=proposal.tool,
        )

    try:
        model = TOOL_INPUTS[proposal.tool].model_validate(arguments)
    except ValidationError:
        return None

    return ValidatedCall(tool=proposal.tool, arguments=model.model_dump(mode="json"))


async def validate_tool_calls(state: TurnState, deps: TurnDeps) -> TurnState:
    """Judge the planned round; accept all of it or none of it.

    An empty round is accepted and goes to the reply; it is the normal end of a
    turn. A round with calls goes to execution. Order is kept, since it is the
    plan.
    """
    extraction = state.extraction
    if extraction is None:  # pragma: no cover - the graph never routes here
        return state

    allowed = gates.allowed_tools(extraction.intent)
    validated: list[ValidatedCall] = []
    for index, proposal in enumerate(state.proposed):
        call = _validated(proposal, index, allowed, state)
        if call is None:
            return state.model_copy(
                update={"outcome": gates.validation_outcome(False), "validated": ()}
            )
        validated.append(call)

    held = gates.held_round_outcome(
        holds_create=any(call.tool == gates.CREATE_ACCESS_REQUEST for call in validated),
        confirmed=state.confirmed,
        already_requested=gates.found_open_request(state.tool_results),
    )
    if held is TurnOutcome.DONE:
        return state.model_copy(update={"outcome": held, "validated": ()})
    if held is TurnOutcome.CONFIRM:
        return state.model_copy(update={"outcome": held, "validated": tuple(validated)})
    return state.model_copy(
        update={"outcome": gates.execution_outcome(len(validated)), "validated": tuple(validated)}
    )
