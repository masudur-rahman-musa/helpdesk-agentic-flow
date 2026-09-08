"""`await_approval`: the turn stops here until a person decides (the design).

The pause the whole of an earlier change exists for. `create_access_request` answered
that the request went to an approval chain, so there is nothing further this
process can do: the node tells the user what is waiting, stores that as the
turn's assistant message, and calls LangGraph's `interrupt()`. The state is
already in Postgres by then, so the run can be resumed later — by an earlier change,
from a different process, after this one is gone.

It DECIDES nothing. `gates.approval_outcome` made the branch and
`execute_tools` recorded it, for a reason the framework forces: `interrupt()`
discards the interrupting node's own state update, so anything written here
would never reach the checkpoint a resume reads. That is why `outcome` and
`awaiting_request_id` are written one node earlier and this node only reads
them.

**The reply is rendered from a template, never composed** (decision E). There
is nothing to compose: the message says a request is filed and how many
approvals it waits on, both of which the tool already answered, and routing it
through `compose_reply` would spend a strong-alias round trip to rephrase two
facts. `test_agent_approval_interrupt.py`'s
`::test_a_paused_turn_returns_await_approval_and_the_request_id` pins that from
the other side — it stages no composition answer, so a turn that reached the
gateway a third time fails outright.
"""

from helpdesk.core.repository import append_message
from helpdesk.models.conversation import MessageRole
from langgraph.types import interrupt

from agent_api.graph.deps import TurnDeps
from agent_api.graph.gates import approval_seat_count
from agent_api.graph.state import TurnState

PENDING_APPROVAL_REPLY = "Your request is filed and is waiting for {seats}."
"""What the turn says while an approval chain has it.

A statement of fact and no promise: it does not say the access will be
granted, because the approvers may reject it, and it does not offer a ticket,
because the request already exists. The prefix before the placeholder is what
`test_agent_approval_interrupt.py::test_a_paused_turn_returns_await_approval_and_the_request_id`
asserts the paused reply starts with, which is what tells this template from a
model-composed sentence.
"""

ONE_SEAT = "one approval"
"""The singular, so a one-seat chain does not read "waiting for 1 approvals"."""

MANY_SEATS = "{count} approvals"
"""The plural, and also the fallback: a chain whose seats could not be counted
renders as "approvals" rather than claiming a number nobody supplied."""


def pending_reply(state: TurnState) -> str:
    """The paused turn's message, rendered from the state and nothing else.

    Called twice on purpose and by design: once here, to store the assistant
    message, and once by `run_turn` when it maps the interrupt to the state it
    returns. The second call is not a duplicate of the first — `interrupt()`
    discards this node's update, so the reply this node computed never comes
    back with it — and sharing the function is what keeps the message stored in
    the conversation identical to the one the route answers with.
    """
    count = approval_seat_count(state.tool_results)
    seats = ONE_SEAT if count == 1 else MANY_SEATS.format(count=count or "the required")
    return PENDING_APPROVAL_REPLY.format(seats=seats)


async def await_approval(state: TurnState, deps: TurnDeps) -> TurnState:
    """Store the pending reply, then pause the run on the request id.

    The message is written BEFORE the interrupt, because the interrupt does not
    return: a user whose turn paused must still see what happened, and a row
    written after `interrupt()` would never be written at all.

    `interrupt()` carries the request id as its value, so the pause a client
    sees names what it is waiting for without reading the checkpoint. The
    return below is reached only on a RESUME, when LangGraph replays this node
    with a decision supplied; an earlier change is the plan that sends one.
    """
    reply = pending_reply(state)
    await append_message(
        deps.db,
        session_id=state.session_id,
        role=MessageRole.ASSISTANT,
        content=reply,
    )
    interrupt(str(state.awaiting_request_id))
    return state.model_copy(update={"reply": reply})
