"""`retrieve_policy`: search the policy text for the intents that need it.

For other intents the node passes straight through, so the decision stays in
`gates.py` and out of the graph edges. The refusal flag comes from the search
tool, which owns the score threshold. Citations keep the tool's ranking. The
node never raises: a tool failure ends the turn as state.
"""

from helpdesk.agent_api.agent import gates
from helpdesk.agent_api.agent.nodes.extract_intent import FAILURE_REPLY
from helpdesk.agent_api.agent.state import TurnOutcome, TurnState
from helpdesk.agent_api.agent.tool_args import SEARCH_POLICY, SearchPolicyArgs, SearchPolicyResult
from helpdesk.agent_api.agent.turn_deps import TurnDeps
from helpdesk.shared.core.errors import ToolError

MAX_QUERY_CHARS = 1000
"""The longest query sent, inclusive. Longer messages are cut, not refused:
a message up to the message cap is allowed, so refusing it here would break
our own promise. Only the query is cut; the stored message keeps every
character."""


def query_for(message: str) -> str:
    """The message as a search query: whole, or cut to the cap."""
    return message[:MAX_QUERY_CHARS]


async def retrieve_policy(state: TurnState, deps: TurnDeps) -> TurnState:
    """Search when the intent needs it; otherwise leave the state untouched, so
    a password reset pays for no search."""
    extraction = state.extraction
    if extraction is None:  # pragma: no cover - the graph never routes here
        return state
    if not gates.should_retrieve(extraction.intent):
        return state

    try:
        payload = await deps.tools.call_tool(
            SEARCH_POLICY, SearchPolicyArgs(query=query_for(state.user_message))
        )
    except ToolError as exc:
        return state.model_copy(
            update={
                "outcome": TurnOutcome.FAILED,
                "error_code": exc.code,
                "reply": FAILURE_REPLY,
            }
        )

    result = SearchPolicyResult.model_validate(dict(payload))
    outcome = gates.retrieval_outcome(result.refused)
    if outcome is TurnOutcome.REFUSE:
        # Keep citations empty here too, so rejected passages can never reach
        # planning even if the tool changed.
        return state.model_copy(update={"outcome": outcome})

    return state.model_copy(update={"outcome": outcome, "citations": tuple(result.citations)})
