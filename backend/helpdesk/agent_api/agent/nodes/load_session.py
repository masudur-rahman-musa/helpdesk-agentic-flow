"""`load_session`: the conversation this turn belongs to.

Opens or reloads the session, reads the recent history, then stores the new
message. History is read first so it holds only earlier messages. The message
is stored even if the model later fails, so the record stays complete.
Identity comes from the verified token, never from the message.
"""

from helpdesk.agent_api.agent.state import TurnState
from helpdesk.agent_api.agent.turn_deps import TurnDeps
from helpdesk.shared.crud.messages import append_message, list_recent_messages
from helpdesk.shared.crud.sessions import get_or_create_session
from helpdesk.shared.models.conversation import Message, MessageRole


def render_message(message: Message) -> str:
    """One history line, `"{role}: {content}"`. Text, not ORM rows, because the
    state is saved to a checkpoint."""
    return f"{message.role}: {message.content}"


async def load_session(state: TurnState, deps: TurnDeps) -> TurnState:
    """Load or open the conversation, take the history, store the new message."""
    db = deps.db
    await get_or_create_session(
        db, session_id=state.session_id, user_id=state.user_id, now=deps.now()
    )
    history = await list_recent_messages(db, state.session_id)
    await append_message(
        db,
        session_id=state.session_id,
        role=MessageRole.USER,
        content=state.user_message,
    )
    return state.model_copy(
        update={"history": tuple(render_message(message) for message in history)}
    )
