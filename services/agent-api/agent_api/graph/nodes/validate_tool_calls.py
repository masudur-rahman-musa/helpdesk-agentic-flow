"""`validate_tool_calls`: the round accepted whole, or refused whole (§10.1 step 6).

Decision C, and the reason this node has no third answer. A round of three
proposals where one is malformed is a plan the model did not think through,
and running its valid two would leave a half-done mutation nobody asked for —
a ticket filed about a request that was never created, or a revoke with no
listing behind it. Refusing the round costs one turn and offers a person;
running two thirds of it costs a wrong record. Pinned by
`test_agent_planning.py::test_one_malformed_proposal_takes_the_whole_round_with_it`,
whose two survivors are legal on their own, so a filtering validator returns
two where this one returns none.

Three checks, and each has a case only it can decide:

1. the tool is in the intent's `INTENT_TOOLS` row — the only guard that can
   see `offboard_user` proposed for a password reset, which is the
   prompt-injection case decision A exists to remove
   (`::test_a_registered_tool_outside_the_intents_row_is_rejected`);
2. the arguments validate against that tool's catalogue model — the only
   guard that can see `onboard_user` missing its manager
   (`::test_a_proposal_missing_a_required_argument_is_rejected`);
3. the model named no `idempotency_key` — the only guard that can see the
   field the WIRE model does declare and require
   (`::test_a_proposal_that_names_its_own_idempotency_key_is_rejected`).

A tool that is not catalogued at all is refused by check 1, because a row can
only ever contain catalogued names; no kill claim is made for a separate
registration check.

**The key is the agent's** (decision D). Every accepted mutating call goes out
with a `uuid5` derived from the turn's own values, checked against the store's
column before it can reach the wire. Nothing here is executed and nothing is
said: 17d owns both.

Pure but for the state it is handed — no database, no gateway, no tool caller.
`test_agent_planning.py` hands it a `TurnDeps` whose members are all `None`,
so an implementation that reached for one raises rather than passing quietly.
"""

from typing import Any

from pydantic import ValidationError

from agent_api.graph import gates
from agent_api.graph.deps import TurnDeps
from agent_api.graph.keys import derive_idempotency_key
from agent_api.graph.state import ProposedCall, TurnState, ValidatedCall
from agent_api.tools.catalogue import (
    IDEMPOTENCY_KEY,
    TOOL_INPUTS,
    needs_idempotency_key,
)


def _validated(
    proposal: ProposedCall, index: int, allowed: frozenset[str], state: TurnState
) -> ValidatedCall | None:
    """One proposal as the call it would become, or `None` if it may not be made.

    `None` rather than a raise: the caller refuses the whole round on the
    first one, and a control-flow exception would make "which check failed"
    the interesting thing when the only decision is whether the round stands.
    """
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
    """Judge the round the planner proposed; accept all of it or none of it.

    An EMPTY round is accepted and validates nothing: a planner with nothing
    left to call is the ordinary end of a turn, and refusing it would send
    every clean `request_access` turn to the refusal template
    (`test_agent_planning.py::test_an_empty_round_is_accepted_and_validates_nothing`).

    Order is preserved, because order is the plan: a classification before the
    request it justifies, a listing before the thing it identifies. Pinned by
    `::test_a_valid_round_of_two_yields_two_validated_calls_in_the_proposed_order`.
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

    return state.model_copy(
        update={"outcome": gates.validation_outcome(True), "validated": tuple(validated)}
    )
