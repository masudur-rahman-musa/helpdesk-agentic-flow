"""The end nodes: every way the turn can stop early.

Each only stores the reply already on the state as the assistant message;
the decision was made earlier. `fail_turn` exists so a turn the model could
not read ends with one fixed reply, not a clarifying question nobody asked.
"""

from helpdesk.agent_api.agent.confirmation import OpenQuestion, question_reply
from helpdesk.agent_api.agent.nodes.extract_intent import FAILURE_REPLY
from helpdesk.agent_api.agent.state import ResolvedEntities, TurnState
from helpdesk.agent_api.agent.turn_deps import TurnDeps
from helpdesk.shared.crud.messages import append_message
from helpdesk.shared.crud.sessions import set_open_question
from helpdesk.shared.models.conversation import MessageRole


async def _store_reply(state: TurnState, deps: TurnDeps, reply: str) -> TurnState:
    """Write the assistant message and carry the reply on the state."""
    await append_message(
        deps.db,
        session_id=state.session_id,
        role=MessageRole.ASSISTANT,
        content=reply,
    )
    return state.model_copy(update={"reply": reply})


async def ask_clarifying(state: TurnState, deps: TurnDeps) -> TurnState:
    """End: ask the question. No search and no tool call on this path."""
    return await _store_reply(state, deps, state.reply or FAILURE_REPLY)


async def ask_disambiguation(state: TurnState, deps: TurnDeps) -> TurnState:
    """End: offer the options, already capped and ranked, and ask which."""
    return await _store_reply(state, deps, state.reply or FAILURE_REPLY)


async def ask_confirmation(state: TurnState, deps: TurnDeps) -> TurnState:
    """End: ask "Do you mean ...?" and store the held round for the next
    message. Nothing is filed; `validated` is cleared once stored, so the
    calls live only on the chat row until a yes."""
    extraction = state.extraction
    if extraction is None:  # pragma: no cover - the graph never routes here
        return state
    question = OpenQuestion(
        asked_at=deps.now(),
        message=state.user_message,
        calls=state.validated,
        extraction=extraction,
        resolved=state.resolved or ResolvedEntities(),
        citations=state.citations,
    )
    await set_open_question(
        deps.db, session_id=state.session_id, value=question.model_dump(mode="json")
    )
    stored = await _store_reply(state, deps, question_reply(state.resolved, state.tool_results))
    return stored.model_copy(update={"validated": ()})


REFUSAL_REPLY = (
    "I could not find anything in the current policy documents that covers "
    "this, so I would rather not guess. I can open a ticket so a person on "
    "the helpdesk team answers it properly. Say the word and I will."
)
"""What the turn says when no policy text answered the question.

It offers a ticket and never opens one: a ticket nobody asked for is a change
the user did not request. A yes arrives as the next turn's `open_ticket`.
"""


async def refuse_and_ticket(state: TurnState, deps: TurnDeps) -> TurnState:
    """End: no policy covers the question, so say so and offer a person.

    No citations are stored, so rejected passages never reach a reply.
    """
    return await _store_reply(state, deps, REFUSAL_REPLY)


async def fail_turn(state: TurnState, deps: TurnDeps) -> TurnState:
    """End: the model failed, so reply in fixed words, never its own output."""
    return await _store_reply(state, deps, FAILURE_REPLY)
