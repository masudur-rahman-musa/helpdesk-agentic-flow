"""`load_session`: the conversation this turn belongs to (the design step 1).

Three effects, in this order, and the order is what the tests pin:

1. the session row for the TOKEN's tenant and user, created or reloaded;
2. the last `HISTORY_LIMIT` messages, oldest first, rendered onto the state;
3. the incoming user message stored.

The history is read BEFORE the new message is stored, so the window is the
twenty messages that came before this turn and not nineteen of them plus the
one being answered. Pinned by
`test_agent_turn.py::test_the_history_window_is_the_last_twenty_messages_oldest_first`.

Storing the user message here rather than after a successful extraction is
deliberate: a turn the model cannot classify still happened, and a transcript
missing the user's own words would replay wrongly and would drop that turn out
of the next turn's history. Pinned by
`test_agent_turn.py::test_the_user_message_row_is_written_before_intent_runs`,
whose model answers are unusable twice.

Identity comes from the state, which `run_turn` filled from the verified token. No node reads a tenant or a user out of the message or out of
the extracted entities.
"""

from helpdesk.agent_api.agent.state import TurnState
from helpdesk.agent_api.agent.turn_deps import TurnDeps
from helpdesk.shared.core.repository import (
    append_message,
    get_or_create_session,
    list_recent_messages,
)
from helpdesk.shared.models.conversation import Message, MessageRole


def render_message(message: Message) -> str:
    """One history line: `"{role}: {content}"`.

    Rendered here rather than carried as ORM rows, because the state crosses a
    checkpoint boundary in an earlier change and a detached SQLAlchemy instance is not
    something to checkpoint.
    """
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
