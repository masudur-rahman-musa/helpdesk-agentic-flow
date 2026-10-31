"""The chat's entry points: one turn in, the reply out.

A turn runs the loop's LangGraph graph (`graph.py`): the model step and the
tool step with the arrow back between them, the held change read by code, and
the grounding check on the reply. This module builds the turn's start state,
gives the graph its run context (the tenant-bound session, the clients and
the chat store) and reads the end state or the stream.

Each turn is its own saver thread, `<session id>:<turn id>`, so a saved turn
never mixes with the next one.
"""

import uuid
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.agent_api.agent.checkpointer import thread_config, turn_thread_id
from helpdesk.agent_api.agent.confirmation import OpenQuestion, parse_open_question
from helpdesk.agent_api.agent.draft import parse_draft
from helpdesk.agent_api.agent.events import (
    ErrorEvent,
    ProgressEvent,
    ReplyEvent,
    StepEvent,
    TurnEvent,
)
from helpdesk.agent_api.agent.graph import (
    BUSY_REPLY,
    CHANGE_FAILED_REPLY,
    FAILURE_REPLY,
    LOOP_PROMPT,
    MAX_SEND_BACKS,
    MAX_STEPS,
    RECURSION_LIMIT,
    TIMEOUT_REPLY,
    LoopContext,
    LoopState,
    LoopStore,
    compiled_graph,
)
from helpdesk.agent_api.agent.loop_tools import PersonRow
from helpdesk.agent_api.agent.prompts import load_prompt
from helpdesk.agent_api.agent.sources import shown_citations
from helpdesk.agent_api.agent.state import RequestDraft, TurnOutcome, TurnState
from helpdesk.agent_api.agent.turn_deps import TurnDeps
from helpdesk.agent_api.agent.turn_metrics import (
    record_turn_code,
    record_turn_state,
    trace_attributes,
)
from helpdesk.agent_api.services.decision_card import decision_card
from helpdesk.agent_api.services.tracing import (
    current_trace_id,
    set_trace_attributes,
    turn_trace,
)
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.core.observability.context import bind_context, current_context
from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.messages import append_message, list_recent_messages
from helpdesk.shared.crud.sessions import (
    get_open_question,
    get_or_create_session,
    get_request_draft,
    set_open_question,
    set_request_draft,
)
from helpdesk.shared.crud.users import find_user_by_id
from helpdesk.shared.models import Department, Role, System, User
from helpdesk.shared.models.conversation import MessageRole, check_message_content

__all__ = [
    "BUSY_REPLY",
    "CHANGE_FAILED_REPLY",
    "FAILURE_REPLY",
    "LOOP_PROMPT",
    "MAX_SEND_BACKS",
    "MAX_STEPS",
    "TIMEOUT_REPLY",
    "DbLoopStore",
    "LoopStore",
    "reply_data",
    "run_loop_turn",
    "stream_loop_turn",
]


@contextmanager
def _turn_telemetry() -> Iterator[None]:
    """Open one turn's trace and tag its log lines with the trace id.
    `bind_context` replaces rather than merges, so all fields are bound again."""
    with turn_trace():
        context = current_context()
        with bind_context(
            tenant_id=context.tenant_id,
            session_id=context.session_id,
            trace_id=current_trace_id(),
            principal_kind=context.principal_kind,
        ):
            yield


def reply_data(state: TurnState) -> dict[str, Any]:
    """The final `reply` event, with the same keys `/chat` returns."""
    card = decision_card(state)
    return {
        "reply": state.reply,
        "outcome": state.outcome.value,
        "citations": [citation.model_dump(mode="json") for citation in shown_citations(state)],
        "request_id": None if state.awaiting_request_id is None else str(state.awaiting_request_id),
        "decision": None if card is None else card.model_dump(mode="json"),
    }


class DbLoopStore:
    """`LoopStore` on the tenant-bound session, through the crud helpers only."""

    def __init__(self, db: AsyncSession, *, session_id: uuid.UUID, user_id: uuid.UUID) -> None:
        self._db = db
        self._session_id = session_id
        self._user_id = user_id
        self._now: datetime | None = None

    async def prepare(self, now: datetime) -> None:
        self._now = now
        await get_or_create_session(
            self._db, session_id=self._session_id, user_id=self._user_id, now=now
        )

    async def principals(self) -> tuple[str, ...]:
        user = await find_user_by_id(self._db, self._user_id)
        return () if user is None else tuple(user.principals)

    async def history(self) -> list[tuple[str, str]]:
        rows = await list_recent_messages(self._db, self._session_id)
        return [(str(row.role), row.content) for row in rows]

    async def open_question(self) -> OpenQuestion | None:
        if self._now is None:  # pragma: no cover - the turn always prepares first
            return None
        raw = await get_open_question(self._db, session_id=self._session_id)
        return parse_open_question(raw, now=self._now)

    async def clear_open_question(self) -> None:
        await set_open_question(self._db, session_id=self._session_id, value=None)

    async def hold(self, question: OpenQuestion) -> None:
        await set_open_question(
            self._db, session_id=self._session_id, value=question.model_dump(mode="json")
        )

    async def request_draft(self) -> RequestDraft | None:
        if self._now is None:  # pragma: no cover - the turn always prepares first
            return None
        raw = await get_request_draft(self._db, session_id=self._session_id)
        return parse_draft(raw, now=self._now)

    async def save_request_draft(self, draft: RequestDraft | None) -> None:
        await set_request_draft(
            self._db,
            session_id=self._session_id,
            value=None if draft is None else draft.model_dump(mode="json"),
        )

    async def add_message(self, role: MessageRole, content: str) -> None:
        await append_message(self._db, session_id=self._session_id, role=role, content=content)

    async def role_ids(self) -> Mapping[tuple[str, str], uuid.UUID]:
        systems = {system.id: system.slug for system in await Repository(self._db, System).list()}
        return {
            (systems[role.system_id], role.slug): role.id
            for role in await Repository(self._db, Role).list()
            if role.system_id in systems
        }

    async def people(self) -> Sequence[PersonRow]:
        departments = {
            department.id: department.name
            for department in await Repository(self._db, Department).list()
        }
        return [
            PersonRow(
                id=user.id,
                display_name=user.display_name,
                department=departments.get(user.department_id, "") if user.department_id else "",
            )
            for user in await Repository(self._db, User).list()
            if user.status == "active"
        ]


async def _turn_items(
    *,
    deps: TurnDeps,
    store: LoopStore,
    start: LoopState,
    thread_id: str,
) -> AsyncIterator[ProgressEvent | StepEvent | TurnState]:
    """The turn's progress and step events, then its end state, from one graph run."""
    graph = deps.graph or compiled_graph(None)
    config = thread_config(thread_id)
    config["recursion_limit"] = RECURSION_LIMIT
    context = LoopContext(deps=deps, store=store, prompt=load_prompt(LOOP_PROMPT))
    final: TurnState | None = None
    async for mode, chunk in graph.astream(
        start, config, context=context, stream_mode=["custom", "values"]
    ):
        if mode == "custom" and isinstance(chunk, ProgressEvent | StepEvent):
            yield chunk
        elif mode == "values" and isinstance(chunk, dict):
            final = chunk.get("final") or final
    if final is None:  # pragma: no cover - every path through the graph ends on a state
        raise ToolError(ErrorCode.INTERNAL)
    yield final


def _start(
    deps: TurnDeps,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    tenant_slug: str,
    gateway_user: str | None,
    message: str,
) -> LoopState:
    """The state a turn starts from."""
    return {
        "turn": TurnState(
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            tenant_slug=tenant_slug,
            gateway_user=gateway_user,
            user_message=message,
            started_at=deps.now(),
        ),
        "started": deps.now(),
    }


async def run_loop_turn(
    *,
    deps: TurnDeps,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    tenant_slug: str,
    message: str,
    store: LoopStore | None = None,
    turn_id: uuid.UUID | None = None,
    gateway_user: str | None = None,
) -> TurnState:
    """One loop turn: the person's message in, the final state out."""
    check_message_content(message)
    store = store or DbLoopStore(deps.db, session_id=session_id, user_id=user_id)
    start = _start(
        deps,
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        tenant_slug=tenant_slug,
        gateway_user=gateway_user,
        message=message,
    )
    thread_id = turn_thread_id(session_id, turn_id or uuid.uuid4())
    final: TurnState | None = None
    with _turn_telemetry():
        async with deps.tools:
            async for item in _turn_items(deps=deps, store=store, start=start, thread_id=thread_id):
                if isinstance(item, TurnState):
                    final = item
        if final is None:  # pragma: no cover - the run always ends on a state
            raise ToolError(ErrorCode.INTERNAL)
        record_turn_state(final)
        set_trace_attributes(trace_attributes(final))
    return final


async def stream_loop_turn(
    *,
    deps: TurnDeps,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    tenant_slug: str,
    message: str,
    store: LoopStore | None = None,
    turn_id: uuid.UUID | None = None,
    gateway_user: str | None = None,
) -> AsyncIterator[TurnEvent]:
    """The same turn as `run_loop_turn`, streamed: per model step that asks for
    tools, a progress event before they run and a step event after, then one
    reply event with the keys `/chat` returns."""
    check_message_content(message)
    store = store or DbLoopStore(deps.db, session_id=session_id, user_id=user_id)
    start = _start(
        deps,
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        tenant_slug=tenant_slug,
        gateway_user=gateway_user,
        message=message,
    )
    thread_id = turn_thread_id(session_id, turn_id or uuid.uuid4())
    final: TurnState | None = None
    with _turn_telemetry():
        try:
            async with deps.tools:
                async for item in _turn_items(
                    deps=deps, store=store, start=start, thread_id=thread_id
                ):
                    if isinstance(item, TurnState):
                        final = item
                    else:
                        yield item
        except ToolError as exc:
            record_turn_code(exc.code)
            yield ErrorEvent(code=exc.code)
            return
        if final is None:  # pragma: no cover - the run always ends on a state
            yield ErrorEvent(code=ErrorCode.INTERNAL)
            return
        record_turn_state(final)
        set_trace_attributes(trace_attributes(final))
        if final.outcome is TurnOutcome.FAILED:
            yield ErrorEvent(code=final.error_code or ErrorCode.INTERNAL)
            return
        yield ReplyEvent(reply=final.reply, reply_data=reply_data(final))
