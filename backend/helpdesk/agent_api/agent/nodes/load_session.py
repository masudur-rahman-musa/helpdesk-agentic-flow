"""`load_session`: the conversation this turn belongs to.

Opens or reloads the session, reads the recent history, then stores the new
message. History is read first so it holds only earlier messages. The message
is stored even if the model later fails, so the record stays complete.
Identity comes from the verified token, never from the message.

An open "I'll file this: ..." question is read and cleared on every message, so
it is answered once at most. A yes routes straight to `execute_tools` with
exactly the stored calls; a bare no ends with a fixed reply; anything else
runs the normal flow.

The time of the last clarifying question, what it asked and how often each
required field was asked are read and cleared the same way, so only the very
next message may not ask again.

The saved request draft is read here and kept on the state as loaded; a yes
and a bare no clear it (`test_draft_turns.py`). The yes runs on the stored
extraction without its second request, so that line is never said twice.

The caller's principals are read from their own directory row by the token's
user id, so a message claiming a role changes nothing. No row gives no
principals. The role ids of that row's department template are read the
same way, through the tenant-bound session; no row or no department reads
none (`test_template_asks_nothing.py`).
"""

import uuid

from helpdesk.agent_api.agent.clarify_cap import (
    parse_asked_asks,
    parse_asked_at,
    parse_asked_kind,
)
from helpdesk.agent_api.agent.confirmation import (
    NOTHING_FILED_REPLY,
    Answer,
    parse_open_question,
    read_answer,
)
from helpdesk.agent_api.agent.request_draft import parse_request_draft
from helpdesk.agent_api.agent.state import TurnOutcome, TurnState
from helpdesk.agent_api.agent.turn_deps import TurnDeps
from helpdesk.shared.crud.directory import fetch_role_templates_for_department
from helpdesk.shared.crud.messages import append_message, list_recent_messages
from helpdesk.shared.crud.sessions import (
    get_asked_clarifying,
    get_asked_kind,
    get_open_question,
    get_or_create_session,
    get_request_draft,
    set_asked_clarifying,
    set_asked_kind,
    set_open_question,
    set_request_draft,
)
from helpdesk.shared.crud.users import find_user_by_id
from helpdesk.shared.models.conversation import Message, MessageRole


def render_message(message: Message) -> str:
    """One history line, `"{role}: {content}"`. Text, not ORM rows, because the
    state is saved to a checkpoint."""
    return f"{message.role}: {message.content}"


async def load_session(state: TurnState, deps: TurnDeps) -> TurnState:
    """Load or open the conversation, take the history, store the new message,
    and answer any open question."""
    db = deps.db
    await get_or_create_session(
        db, session_id=state.session_id, user_id=state.user_id, now=deps.now()
    )
    raw = await get_open_question(db, session_id=state.session_id)
    question = parse_open_question(raw, now=deps.now())
    if raw is not None:
        await set_open_question(db, session_id=state.session_id, value=None)
    draft = parse_request_draft(
        await get_request_draft(db, session_id=state.session_id), now=deps.now()
    )
    asked_at = await get_asked_clarifying(db, session_id=state.session_id)
    asked_before = parse_asked_at(asked_at, now=deps.now())
    if asked_at is not None:
        await set_asked_clarifying(db, session_id=state.session_id, value=None)
    raw_kind = await get_asked_kind(db, session_id=state.session_id)
    asked_kind = parse_asked_kind(raw_kind, now=deps.now())
    asks = parse_asked_asks(raw_kind, now=deps.now())
    if raw_kind is not None:
        await set_asked_kind(db, session_id=state.session_id, value=None)
    history = await list_recent_messages(db, state.session_id)
    user = await find_user_by_id(db, state.user_id)
    template: frozenset[uuid.UUID] = frozenset()
    if user is not None and user.department_id is not None:
        template = await fetch_role_templates_for_department(db, user.department_id)
    await append_message(
        db,
        session_id=state.session_id,
        role=MessageRole.USER,
        content=state.user_message,
    )
    loaded = state.model_copy(
        update={
            "history": tuple(render_message(message) for message in history),
            "asked_before": asked_before,
            "asked_kind": asked_kind,
            "asks": asks,
            "request_draft": draft,
            "principals": () if user is None else tuple(user.principals),
            "template_role_ids": template,
        }
    )
    if question is None:
        return loaded
    answer = read_answer(state.user_message)
    if answer is not Answer.OTHER:
        # A request filed or dropped leaves nothing to carry over.
        await set_request_draft(db, session_id=state.session_id, value=None)
        loaded = loaded.model_copy(update={"request_draft": None})
    if answer is Answer.YES:
        return loaded.model_copy(
            update={
                "outcome": TurnOutcome.CONFIRMED,
                "confirmed": True,
                "validated": question.calls,
                "extraction": question.extraction.model_copy(update={"second_request": None}),
                "resolved": question.resolved,
                "citations": question.citations,
            }
        )
    if answer is Answer.NO:
        return loaded.model_copy(
            update={"outcome": TurnOutcome.CLARIFY, "reply": NOTHING_FILED_REPLY}
        )
    return loaded
