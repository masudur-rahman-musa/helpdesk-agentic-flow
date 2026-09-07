"""The four END nodes: every way this half of the turn can stop early.

Each does exactly one thing — store the reply the deciding node already put on
the state as the assistant message — and none of them decides anything. The
decision was made by a pure predicate in `gates.py` and recorded as `outcome`;
these nodes only write it down.

`fail_turn` is a third END node beyond the two the plan's §1 names. It is what
the same §1's unhappy-path criterion requires: a turn that could not classify
the message must end with a templated reply and EXACTLY ONE assistant message,
and routing that to `ask_clarifying` would store a clarifying question the
system never asked. Recorded in the plan's Deviations.
"""

from helpdesk.core.repository import append_message
from helpdesk.models.conversation import MessageRole

from agent_api.graph.deps import TurnDeps
from agent_api.graph.nodes.extract_intent import FAILURE_REPLY
from agent_api.graph.state import TurnState


async def _store_reply(state: TurnState, deps: TurnDeps, reply: str) -> TurnState:
    """Write the assistant message and carry the reply on the state.

    One row, always: the turn ends here, so a second assistant message could
    only come from a second ending running, which the graph's edges forbid.
    """
    await append_message(
        deps.db,
        session_id=state.session_id,
        role=MessageRole.ASSISTANT,
        content=reply,
    )
    return state.model_copy(update={"reply": reply})


async def ask_clarifying(state: TurnState, deps: TurnDeps) -> TurnState:
    """END: the turn asks its question and does nothing else.

    No retrieval and no tool call happen on this path (the design step 3),
    which `test_agent_turn.py::test_a_confidence_below_the_gate_asks_the_clarifying_question`
    pins by staging exactly one model answer: a turn that continued would make
    a second request and fail outright.
    """
    return await _store_reply(state, deps, state.reply or FAILURE_REPLY)


async def ask_disambiguation(state: TurnState, deps: TurnDeps) -> TurnState:
    """END: the turn offers its options and asks which one was meant.

    The options were capped at three and ordered by `resolve_entities`; this
    node neither ranks nor trims them.
    """
    return await _store_reply(state, deps, state.reply or FAILURE_REPLY)


REFUSAL_REPLY = (
    "I could not find anything in the current policy documents that covers "
    "this, so I would rather not guess. I can open a ticket so a person on "
    "the helpdesk team answers it properly. Say the word and I will."
)
"""What the turn says when the corpus had no good answer (the design step 5).

An OFFER, never an opened ticket (decision E): a ticket nobody asked for is a
mutation the user did not request, and the acceptance arrives as the next
turn's `open_ticket` intent. The wording is asserted for exactly that — it may
not claim a ticket exists — by
`test_agent_turn.py::test_the_refusal_reply_is_a_fixed_string_offering_a_person`.

A fixed template rather than the model's words: nothing composed this reply,
because the whole reason for it is that there was nothing to compose from.
"""


async def refuse_and_ticket(state: TurnState, deps: TurnDeps) -> TurnState:
    """END: no current policy covers the question, so the turn says so and offers.

    It stores no citations and calls no tool. Both matter: the chunks the
    threshold rejected must never reach a reply (requirement #14), and the
    ticket is offered rather than opened. Pinned by
    `test_agent_turn.py::test_a_refused_page_ends_on_the_templated_refusal_and_creates_no_ticket`,
    which asserts the stub caller recorded no `create_ticket`.
    """
    return await _store_reply(state, deps, REFUSAL_REPLY)


async def fail_turn(state: TurnState, deps: TurnDeps) -> TurnState:
    """END: the model could not answer, so the turn says so in fixed words.

    The templated reply, never the model's own output and never the gateway's
    message: what failed here is the model, so anything it produced is exactly
    what must not be shown.
    """
    return await _store_reply(state, deps, FAILURE_REPLY)
