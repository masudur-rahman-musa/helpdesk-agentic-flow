"""`retrieve_policy`: the corpus asked, for the two intents that need it (§10.1 step 5).

A PASS-THROUGH for the other eleven intents rather than a node the graph routes
around (plan §2). Routing around it would put `should_retrieve` inside a
conditional edge, which is exactly what the design's boundary rule forbids: the
predicate is pure and lives in `gates.py`, this node reads it, and the edge
downstream only reads the `outcome` field this node wrote.

**The refusal is not recomputed here** (decision D). `search_policy` already
decided it with `should_refuse` over the fused hits (15a decision D); this node
reads the flag onto the state through `gates.retrieval_outcome`, so the
threshold keeps one home and a tuning ADR changes one number in one place.

**The citations keep the tool's order.** That order is 15a's RRF fusion, and a
re-rank here would quietly replace a ranking two arms of retrieval produced,
pinned by
`test_agent_turn.py::test_a_request_access_turn_calls_search_policy_exactly_once`,
whose payload is deliberately ascending in `score`.

The node does not raise. A tool that failed ends the TURN, as state, with the
typed code and the templated reply — the same shape `extract_intent` uses, and
what lets `run_turn`'s caller render a state rather than translate an
exception.
"""

from helpdesk.errors import ToolError

from agent_api.graph import gates
from agent_api.graph.deps import TurnDeps
from agent_api.graph.nodes.extract_intent import FAILURE_REPLY
from agent_api.graph.state import TurnOutcome, TurnState
from agent_api.tools.catalogue import SEARCH_POLICY, SearchPolicyArgs, SearchPolicyResult

MAX_QUERY_CHARS = 1000
"""The longest query this node will send, INCLUSIVE: exactly 1000 goes whole.

Decision G, and the reason it is a cut rather than a refusal: `MAX_MESSAGE_CHARS`
lets a 4000-character message in, so refusing one here would make two of our
own caps contradict each other and fail a turn for something the user was told
was allowed. The cut is on the QUERY only — the stored message keeps every
character. a project rule and 2, pinned at both sides of the boundary by
`test_agent_turn.py::test_query_for_cuts_at_the_cap_inclusively` and through
the real turn by `::test_a_message_at_the_query_cap_is_sent_whole` and
`::test_a_message_one_character_over_the_query_cap_is_cut_not_refused`.
"""


def query_for(message: str) -> str:
    """The message as a search query: whole, or cut to the cap.

    Pure and separate from the node so the boundary can be asserted without a
    database, a graph or a stub caller.
    """
    return message[:MAX_QUERY_CHARS]


async def retrieve_policy(state: TurnState, deps: TurnDeps) -> TurnState:
    """Search the corpus when the intent needs it; otherwise pass straight on.

    Pass-through means untouched: no tool call, no citations and the outcome
    the previous node left, so a `reset_password` turn pays neither an
    embedding nor a vector search
    (`test_agent_turn.py::test_a_reset_password_turn_makes_no_tool_call_and_carries_no_citations`).
    """
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
        # A refused page carries no citations by the tool's own guarantee; the
        # state is left empty here too, so nothing the threshold rejected can
        # reach 17c even if that guarantee were ever weakened.
        return state.model_copy(update={"outcome": outcome})

    return state.model_copy(update={"outcome": outcome, "citations": tuple(result.citations)})
