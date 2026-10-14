"""`await_approval`: the turn stops here until a person decides (the design).

The pause the whole of an earlier change exists for. `create_access_request` answered
that the request went to an approval chain, so there is nothing further this
process can do: the node calls LangGraph's `interrupt()`. The state is already
in Postgres by then, so the run can be resumed later — by `agent_api.resume`,
from a different process, after this one is gone.

The node stores NO row. LangGraph replays the
interrupting node from its start on a resume, so an `append_message` here would
store the pending reply a second time — a history telling the user their
request is waiting for approval after the approver has already decided it.
`run_turn` writes it instead, once, where the interrupt is mapped; this module
still owns the WORDS, through `pending_reply`. Pinned by
`test_agent_approval_resume.py::test_a_resumed_turn_leaves_exactly_one_pending_message`.

It DECIDES nothing. `gates.approval_outcome` made the branch and
`execute_tools` recorded it, for a reason the framework forces: `interrupt()`
discards the interrupting node's own state update, so anything written here
would never reach the checkpoint a resume reads. That is why `outcome` and
`awaiting_request_id` are written one node earlier and this node only reads
them.

**The PAUSED reply is rendered from a template, never composed** (18a decision
E). There is nothing to compose: the message says a request is filed and how
many approvals it waits on, both of which the tool already answered, and
routing it through `compose_reply` would spend a round trip to rephrase two
facts. `test_agent_approval_interrupt.py`'s
`::test_a_paused_turn_returns_await_approval_and_the_request_id` pins that from
the other side — it stages no composition answer, so a turn that reached the
gateway a third time fails outright.

The RESUMED reply is composed, and that is not the same claim. The edge out of
this node reaches `compose_reply`, but only a resume ever traverses it: on the
first pass `interrupt()` does not return, so the pause still ends here with the
template. What the composition is given is the recorded decision, appended
below as a tool result.
"""

from langgraph.types import interrupt

from helpdesk.agent_api.agent.gates import approval_seat_count
from helpdesk.agent_api.agent.state import ToolResult, TurnOutcome, TurnState
from helpdesk.agent_api.agent.turn_deps import TurnDeps

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

    Called by `run_turn` alone, once per pause, for both the state it returns
    and the assistant row it stores. This node no longer calls it: `interrupt()`
    discards this node's update, so a reply computed here would never come back
    with the state, and a row written here would be written again on every
    resume (decision C).
    """
    count = approval_seat_count(state.tool_results)
    seats = ONE_SEAT if count == 1 else MANY_SEATS.format(count=count or "the required")
    return PENDING_APPROVAL_REPLY.format(seats=seats)


async def await_approval(state: TurnState, deps: TurnDeps) -> TurnState:
    """Pause the run on the request id; on a resume, record the decision.

    `interrupt()` carries the request id as its value, so the pause a client
    sees names what it is waiting for without reading the checkpoint. It does
    not return on the first pass: everything below it runs only on a RESUME,
    when LangGraph replays this node with the value `resume_graph` sent.

    That value is a `ToolResult` `agent_api.resume` built from the decided
    request ROW, as plain JSON — the resume value crosses
    the checkpointer's serde, so it is validated back here rather than trusted
    as an object. Appending it to `tool_results` is the whole of a design note: the
    approver's decision IS a tool result, `approve_request` or `reject_request`
    having run in another process, and `compose_reply` already quotes
    `tool_results` as data. Nothing is re-executed.

    `DONE` is set here rather than by the composition, which never writes an
    outcome: the edge out of this node is unconditional, so the state reaching
    `compose_reply` must already say the turn is finishing. Pinned by
    `test_agent_approval_resume.py::test_a_recorded_approval_resumes_the_run_from_a_fresh_saver`
    and `::test_a_rejection_resumes_and_composes_the_rejection`.

    `deps` is unused and stays in the signature: every node in this package
    takes the same pair and `build.py`'s adapters call them all identically.
    """
    del deps
    decision = interrupt(str(state.awaiting_request_id))
    return state.model_copy(
        update={
            "tool_results": (*state.tool_results, ToolResult.model_validate(decision)),
            "outcome": TurnOutcome.DONE,
        }
    )
