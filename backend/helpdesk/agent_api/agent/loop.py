"""The chat as one tool-calling agent loop, the second engine beside the graph.

The model reads the chat as real `user` and `assistant` turns under the
`agent_loop` prompt, with only the tools the caller's role allows
(`loop_tools.offered_tools`). Each step it answers, or calls tools whose
results come back as `tool` turns, which the prompt treats as data. Lookups
run through MCP with the caller's own token; the finders run here.

Limits, the graph's own budget: at most `MAX_STEPS` model steps, 6 tool
calls, 40,000 tokens and 90 seconds a turn. A limit hit with tool results
gets one last step with no tools; with none, the fixed `TIMEOUT_REPLY` and
no model call (`test_deadline_with_no_results_gives_the_fixed_line`).

Changes are never run here. A proposed change is checked against the tool's
input model, given a key by code, and held as the chat's open question; the
reply is the code-written summary. Bad arguments go back to the model once
with the reason, then `CHANGE_FAILED_REPLY` (`test_bad_change_arguments_go_back_once`).
The next message, when it is a yes or a bare no to that question, is handed
to the graph, which files or drops it exactly as before
(`test_yes_after_a_held_change_files_once`).

The decision-word check runs on the final text against this turn's tool
results; a breach goes back once with the reason, then the reply is said
from the tool results by code (`test_decision_word_breach_is_sent_back_once`).
"""

import contextlib
import json
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.agent_api.agent import gates
from helpdesk.agent_api.agent.confirmation import (
    Answer,
    OpenQuestion,
    change_summary,
    parse_open_question,
    read_answer,
)
from helpdesk.agent_api.agent.events import ErrorEvent, ReplyEvent, StepEvent, TurnEvent
from helpdesk.agent_api.agent.graph import (
    _reply_data,
    _turn_telemetry,
    run_turn,
    stream_turn,
)
from helpdesk.agent_api.agent.keys import derive_idempotency_key
from helpdesk.agent_api.agent.loop_tools import (
    FIND_PERSON,
    FIND_ROLE,
    Directory,
    LoopTool,
    PersonRow,
    ToolKind,
    find_person,
    find_role,
    offered_tools,
)
from helpdesk.agent_api.agent.nodes.extract_intent import FAILURE_REPLY
from helpdesk.agent_api.agent.postcheck import (
    contradicts,
    outcome_reply,
    permitted_words,
    words_in,
)
from helpdesk.agent_api.agent.prompts import load_prompt
from helpdesk.agent_api.agent.role_words import with_plain_role_names
from helpdesk.agent_api.agent.state import (
    Citation,
    Intent,
    IntentEntities,
    IntentExtraction,
    PostCheckOutcome,
    ResolvedEntities,
    ToolResult,
    TurnOutcome,
    TurnState,
    ValidatedCall,
)
from helpdesk.agent_api.agent.tool_args import (
    IDEMPOTENCY_KEY,
    SEARCH_POLICY,
    TOOL_INPUTS,
    SearchPolicyResult,
)
from helpdesk.agent_api.agent.turn_deps import TurnDeps
from helpdesk.agent_api.agent.turn_metrics import (
    record_turn_code,
    record_turn_state,
    trace_attributes,
)
from helpdesk.agent_api.services.model_gateway.client import ModelAlias, ToolCall, ToolCallAnswer
from helpdesk.agent_api.services.tracing import set_trace_attributes
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.messages import append_message, list_recent_messages
from helpdesk.shared.crud.sessions import (
    get_open_question,
    get_or_create_session,
    set_open_question,
)
from helpdesk.shared.crud.users import find_user_by_id
from helpdesk.shared.models import Department, Role, System, User
from helpdesk.shared.models.conversation import MessageRole, check_message_content

__all__ = [
    "CHANGE_FAILED_REPLY",
    "LOOP_PROMPT",
    "MAX_STEPS",
    "TIMEOUT_REPLY",
    "DbLoopStore",
    "LoopStore",
    "run_loop_turn",
    "stream_loop_turn",
]

LOOP_PROMPT = "agent_loop"

MAX_STEPS = 4
"""Model steps a turn may take; the last is offered no tools, so it answers."""

STEP_NODE = "agent_step"
"""The `node` a loop step streams as; the web shows it like a graph node."""

MAX_RESULT_CHARACTERS = 20_000
"""A tool result longer than this is cut, so one listing cannot fill the turn."""

TIMEOUT_REPLY = (
    "I ran out of time looking that up, so I have nothing to tell you yet. "
    "Please ask again, maybe in fewer words."
)
"""Said when the deadline passed before any tool answered. No model call."""

CHANGE_FAILED_REPLY = (
    "I could not put that change together correctly, so nothing was prepared. "
    "Tell me again what you need, with the details."
)
"""Said when a proposed change was wrong twice. Nothing is held."""

_CHANGE_INTENTS: Mapping[str, Intent] = {
    "create_access_request": Intent.REQUEST_ACCESS,
    "revoke_access": Intent.REVOKE_ACCESS,
    "onboard_user": Intent.ONBOARD,
    "transfer_user": Intent.REPORT_MOVER,
    "offboard_user": Intent.OFFBOARD,
    "open_access_review": Intent.REVIEW,
    "decide_review_item": Intent.REVIEW,
    "approve_request": Intent.APPROVALS,
    "reject_request": Intent.APPROVALS,
    "reset_own_password": Intent.RESET_PASSWORD,
    "unlock_own_account": Intent.UNLOCK_ACCOUNT,
    "create_ticket": Intent.OPEN_TICKET,
}
"""The intent a held change is stored under, so the graph's yes path, which
reads an intent, files it as it files its own."""

_DECISIONS = frozenset({gates.APPROVE_REQUEST, gates.REJECT_REQUEST})


class LoopStore(Directory, Protocol):
    """The chat row and directory reads one loop turn needs."""

    async def prepare(self, now: datetime) -> None: ...

    async def principals(self) -> tuple[str, ...]: ...

    async def history(self) -> list[tuple[str, str]]: ...

    async def open_question(self) -> OpenQuestion | None: ...

    async def clear_open_question(self) -> None: ...

    async def hold(self, question: OpenQuestion) -> None: ...

    async def add_message(self, role: MessageRole, content: str) -> None: ...


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


class _TurnFailed(Exception):
    """A tool or the model failed in a way no reply can cover."""

    def __init__(self, code: ErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


def _tool_turn(call_id: str, content: Any) -> dict[str, Any]:
    """One `tool` turn: the call's answer, as JSON text, cut if very long."""
    text = json.dumps(content, default=str)
    if len(text) > MAX_RESULT_CHARACTERS:
        text = text[:MAX_RESULT_CHARACTERS] + " ...(cut)"
    return {"role": "tool", "tool_call_id": call_id, "content": text}


def _assistant_turn(text: str | None, calls: Sequence[ToolCall]) -> dict[str, Any]:
    """The model's own step, sent back so it sees what it asked for."""
    return {
        "role": "assistant",
        "content": text,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }
            for call in calls
        ],
    }


def _problems(exc: ValidationError) -> str:
    """A validation error as "field: what is wrong" lines the model can act on."""
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or 'arguments'}: {error['msg']}"
        for error in exc.errors()
    )


class _Turn:
    """One loop turn's working memory. Never saved: the chat row and the
    final state are what outlive it."""

    def __init__(self, state: TurnState, deps: TurnDeps, store: LoopStore) -> None:
        self.state = state
        self.deps = deps
        self.store = store
        self.started = deps.now()
        self.prompt = load_prompt(LOOP_PROMPT)
        self.messages: list[dict[str, Any]] = []
        self.offered: dict[str, LoopTool] = {}
        self.results: list[ToolResult] = []
        self.citations: list[Citation] = []
        self.calls_made = 0
        self.tokens = 0
        self.step = 0
        self.change_sent_back = False
        self.roles_found: dict[str, dict[str, Any]] = {}
        self.people_found: dict[str, str] = {}

    # --- the steps -----------------------------------------------------------

    async def run(self) -> AsyncIterator[StepEvent | TurnState]:
        """Every step event, then the final state as the last item."""
        principals = await self.store.principals()
        history = await self.store.history()
        await self.store.add_message(MessageRole.USER, self.state.user_message)
        self.state = self.state.model_copy(
            update={
                "principals": principals,
                "history": tuple(f"{role}: {text}" for role, text in history),
                "prompt_version": self.prompt.sha256,
            }
        )
        self.offered = {tool.name: tool for tool in offered_tools(principals)}
        self.messages = [
            {"role": "system", "content": self.prompt.text},
            *(
                {"role": role, "content": text}
                for role, text in history
                if role in (MessageRole.USER.value, MessageRole.ASSISTANT.value)
            ),
            {"role": "user", "content": self.state.user_message},
        ]
        tools_on = True
        try:
            while True:
                self.step += 1
                spent = self._spent()
                if spent and not self.results:
                    yield await self._finish(TIMEOUT_REPLY)
                    return
                last = spent or not tools_on or self.step >= MAX_STEPS
                answer = await self._ask(tools=not last)
                if last or not answer.calls:
                    yield await self._answer(answer.text)
                    return
                self.messages.append(_assistant_turn(answer.text, answer.calls))
                ended, ran = await self._run_calls(answer.calls)
                if ran:
                    yield StepEvent(node=STEP_NODE, round=self.step, tools=ran)
                if ended is not None:
                    yield ended
                    return
                if self.calls_made >= gates.MAX_TOOL_CALLS:
                    tools_on = False
        except _TurnFailed as failed:
            yield await self._finish(
                FAILURE_REPLY, outcome=TurnOutcome.FAILED, error_code=failed.code
            )

    def _spent(self) -> bool:
        """Whether the deadline or the token budget is used up. The deadline
        is inclusive: the 90th second is still in time."""
        elapsed = (self.deps.now() - self.started).total_seconds()
        return elapsed > gates.TURN_DEADLINE_SECONDS or self.tokens > gates.MAX_TURN_TOKENS

    async def _ask(self, *, tools: bool) -> ToolCallAnswer:
        """One model step on the strong alias; no tools sends none."""
        try:
            answer = await self.deps.model.complete_with_tools(
                [dict(message) for message in self.messages],
                [tool.wire() for tool in self.offered.values()] if tools else [],
                alias=ModelAlias.AGENT_STRONG,
                tenant_slug=self.state.tenant_slug,
                session_id=str(self.state.session_id),
            )
        except ToolError as exc:
            raise _TurnFailed(exc.code) from exc
        self.tokens += answer.usage.total_tokens
        return answer

    async def _run_calls(
        self, calls: Sequence[ToolCall]
    ) -> tuple[TurnState | None, list[dict[str, Any]]]:
        """Run the step's lookups in order and check its changes. A held change
        ends the turn, which is the first value; the second is the step's
        results as the stream shows them."""
        answers: dict[str, dict[str, Any]] = {}
        changes: list[ToolCall] = []
        ran: list[dict[str, Any]] = []
        for call in calls:
            tool = self.offered.get(call.name)
            if tool is None:
                answers[call.id] = _tool_turn(
                    call.id, {"error": "not_available", "reason": "this tool is not offered"}
                )
            elif tool.kind is ToolKind.CHANGE:
                changes.append(call)
            elif self.calls_made >= gates.MAX_TOOL_CALLS:
                answers[call.id] = _tool_turn(
                    call.id, {"error": "limit", "reason": "no more tool calls this turn"}
                )
            else:
                answers[call.id] = await self._look_up(call, tool, ran)
        if changes:
            held, reasons = self._checked_changes(changes)
            if held:
                return await self._hold(held), ran
            if self.change_sent_back:
                return await self._finish(CHANGE_FAILED_REPLY), ran
            self.change_sent_back = True
            for call in changes:
                answers[call.id] = _tool_turn(
                    call.id, {"error": "bad_arguments", "reason": reasons[call.id]}
                )
        self.messages.extend(answers[call.id] for call in calls)
        return None, ran

    async def _look_up(
        self, call: ToolCall, tool: LoopTool, ran: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Run one lookup or finder and keep its result. A typed refusal is an
        answer the model reads; only `INTERNAL` ends the turn."""
        try:
            if tool.kind is ToolKind.FIND:
                words = call.arguments.get("words")
                if not isinstance(words, str) or set(call.arguments) != {"words"}:
                    return _tool_turn(
                        call.id, {"error": "bad_arguments", "reason": "words: one string"}
                    )
                self.calls_made += 1
                payload = await self._find(call.name, words)
            else:
                try:
                    arguments = TOOL_INPUTS[call.name].model_validate(call.arguments)
                except ValidationError as exc:
                    return _tool_turn(call.id, {"error": "bad_arguments", "reason": _problems(exc)})
                self.calls_made += 1
                payload = dict(await self.deps.tools.call_tool(call.name, arguments))
        except ToolError as exc:
            if exc.code is ErrorCode.INTERNAL:
                raise _TurnFailed(exc.code) from exc
            self.results.append(ToolResult(tool=call.name, ok=False, error_code=exc.code))
            ran.append({"tool": call.name, "ok": False, "error_code": exc.code.value})
            return _tool_turn(call.id, {"error": exc.code.value})
        self.results.append(ToolResult(tool=call.name, ok=True, payload=payload))
        ran.append({"tool": call.name, "ok": True, "error_code": None})
        self._remember(call.name, payload)
        return _tool_turn(call.id, with_plain_role_names(payload))

    async def _find(self, name: str, words: str) -> dict[str, Any]:
        if name == FIND_ROLE:
            return {"roles": await find_role(words, tools=self.deps.tools, directory=self.store)}
        if name == FIND_PERSON:
            return {"people": await find_person(words, directory=self.store)}
        raise _TurnFailed(ErrorCode.INTERNAL)  # pragma: no cover - only two finders exist

    def _remember(self, name: str, payload: Mapping[str, Any]) -> None:
        """Keep found roles and people for the summary, and passages for Sources."""
        if name == FIND_ROLE:
            for row in payload.get("roles") or []:
                self.roles_found[str(row["role_id"])] = row
        elif name == FIND_PERSON:
            for row in payload.get("people") or []:
                self.people_found[str(row["user_id"])] = str(row["name"])
        elif name == SEARCH_POLICY:
            with contextlib.suppress(ValidationError):
                self.citations.extend(SearchPolicyResult.model_validate(payload).citations)

    # --- changes -------------------------------------------------------------

    def _checked_changes(
        self, calls: Sequence[ToolCall]
    ) -> tuple[list[ValidatedCall], dict[str, str]]:
        """The step's changes with their keys, all or none, and the reason per
        call when any is wrong. Code adds the key; a model that names one is wrong."""
        listed = gates.listed_request_ids(tuple(self.results))
        validated: list[ValidatedCall] = []
        reasons: dict[str, str] = {}
        for index, call in enumerate(calls):
            if IDEMPOTENCY_KEY in call.arguments:
                reasons[call.id] = f"{IDEMPOTENCY_KEY}: do not send it, it is added for you"
                continue
            if call.name in _DECISIONS and str(call.arguments.get("request_id")) not in listed:
                reasons[call.id] = (
                    "request_id: not in this turn's list_pending_approvals result; list them first"
                )
                continue
            arguments = {
                **call.arguments,
                IDEMPOTENCY_KEY: derive_idempotency_key(
                    session_id=self.state.session_id,
                    user_message=self.state.user_message,
                    round_number=self.step,
                    index=index,
                    tool_name=call.name,
                ),
            }
            try:
                checked = TOOL_INPUTS[call.name].model_validate(arguments)
            except ValidationError as exc:
                reasons[call.id] = _problems(exc)
                continue
            validated.append(
                ValidatedCall(tool=call.name, arguments=checked.model_dump(mode="json"))
            )
        if reasons:
            for call in calls:
                reasons.setdefault(call.id, "held back: another change in this step was wrong")
            return [], reasons
        return validated, {}

    async def _hold(self, calls: list[ValidatedCall]) -> TurnState:
        """Store the checked changes as the open question and say what a yes runs."""
        first = calls[0]
        target = str(first.arguments.get("target_user_id") or "")
        person = None
        if target and target != str(self.state.user_id):
            person = self.people_found.get(target, "someone else")
        role = self.roles_found.get(str(first.arguments.get("role_id")))
        resolved = ResolvedEntities(
            role_id=uuid.UUID(role["role_id"]) if role else None,
            role_name=role["name"] if role else None,
            system_name=role["system"] if role else None,
        )
        extraction = IntentExtraction(
            intent=_CHANGE_INTENTS[first.tool],
            entities=IntentEntities(
                system=None,
                role=None,
                target_user=person,
                department=None,
                expiry_days=None,
                justification=None,
            ),
            confidence=1.0,
            clarifying_question=None,
            second_request=None,
        )
        await self.store.hold(
            OpenQuestion(
                asked_at=self.deps.now(),
                message=self.state.user_message,
                calls=tuple(calls),
                extraction=extraction,
                resolved=resolved,
                citations=tuple(self.citations),
            )
        )
        self.state = self.state.model_copy(update={"extraction": extraction, "resolved": resolved})
        reply = change_summary(calls, tuple(self.results), resolved, person)
        return await self._finish(reply, outcome=TurnOutcome.CONFIRM)

    # --- the answer ----------------------------------------------------------

    async def _answer(self, text: str | None) -> TurnState:
        """The model's text, held to this turn's tool results: a breach goes
        back once with the reason, then the reply is said from the results."""
        reply = (text or "").strip()
        checked = self._state()
        if not reply:
            return await self._finish(outcome_reply(checked), postcheck=PostCheckOutcome.FALLBACK)
        if not contradicts(reply, checked):
            return await self._finish(reply)
        unbacked = sorted(words_in(reply) - permitted_words(checked))
        self.messages.extend(
            [
                {"role": "assistant", "content": reply},
                {
                    "role": "user",
                    "content": (
                        f"Check failed: your reply says {', '.join(unbacked)}, but no tool "
                        "result in this turn says so. Write the reply again without claiming it."
                    ),
                },
            ]
        )
        again = ((await self._ask(tools=False)).text or "").strip()
        if again and not contradicts(again, self._state()):
            return await self._finish(again, postcheck=PostCheckOutcome.REGENERATED)
        return await self._finish(outcome_reply(self._state()), postcheck=PostCheckOutcome.FALLBACK)

    def _state(self, **update: Any) -> TurnState:
        """The turn as a state, with what it gathered so far."""
        return self.state.model_copy(
            update={
                "tool_results": tuple(self.results),
                "citations": tuple(self.citations),
                "tool_calls_made": self.calls_made,
                "tokens_used": self.tokens,
                "round_number": self.step,
                **update,
            }
        )

    async def _finish(
        self,
        reply: str,
        *,
        outcome: TurnOutcome = TurnOutcome.DONE,
        postcheck: PostCheckOutcome = PostCheckOutcome.PASSED,
        error_code: ErrorCode | None = None,
    ) -> TurnState:
        """Store the reply as the assistant message and end the turn."""
        await self.store.add_message(MessageRole.ASSISTANT, reply)
        return self._state(reply=reply, outcome=outcome, postcheck=postcheck, error_code=error_code)


async def _answers_held_question(store: LoopStore, message: str, now: datetime) -> bool:
    """Whether the message is a yes or a bare no to a held change. Anything
    else drops the question, as the graph does, so it is answered once at most."""
    await store.prepare(now)
    question = await store.open_question()
    if question is None:
        return False
    if read_answer(message) is not Answer.OTHER:
        return True
    await store.clear_open_question()
    return False


async def run_loop_turn(
    *,
    deps: TurnDeps,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    tenant_slug: str,
    message: str,
    checkpointer: Any = None,
    store: LoopStore | None = None,
) -> TurnState:
    """One loop turn, with the same arguments and result as the graph's
    `run_turn`. A yes or no to a held change runs on the graph instead."""
    check_message_content(message)
    store = store or DbLoopStore(deps.db, session_id=session_id, user_id=user_id)
    started = deps.now()
    if await _answers_held_question(store, message, started):
        return await run_turn(
            deps=deps,
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            tenant_slug=tenant_slug,
            message=message,
            checkpointer=checkpointer,
        )
    state = TurnState(
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        tenant_slug=tenant_slug,
        user_message=message,
        started_at=started,
    )
    final: TurnState | None = None
    with _turn_telemetry():
        async with deps.tools:
            async for item in _Turn(state, deps, store).run():
                if isinstance(item, TurnState):
                    final = item
        if final is None:  # pragma: no cover - `run` always ends on a state
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
    checkpointer: Any = None,
    store: LoopStore | None = None,
) -> AsyncIterator[TurnEvent]:
    """The same turn as `run_loop_turn`, streamed: one step event per model
    step that ran a tool, then one reply event with the keys `/chat` returns."""
    check_message_content(message)
    store = store or DbLoopStore(deps.db, session_id=session_id, user_id=user_id)
    started = deps.now()
    if await _answers_held_question(store, message, started):
        async for event in stream_turn(
            deps=deps,
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            tenant_slug=tenant_slug,
            message=message,
            checkpointer=checkpointer,
        ):
            yield event
        return
    state = TurnState(
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        tenant_slug=tenant_slug,
        user_message=message,
        started_at=started,
    )
    final: TurnState | None = None
    with _turn_telemetry():
        try:
            async with deps.tools:
                async for item in _Turn(state, deps, store).run():
                    if isinstance(item, TurnState):
                        final = item
                    else:
                        yield item
        except ToolError as exc:
            record_turn_code(exc.code)
            yield ErrorEvent(code=exc.code)
            return
        if final is None:  # pragma: no cover - `run` always ends on a state
            yield ErrorEvent(code=ErrorCode.INTERNAL)
            return
        record_turn_state(final)
        set_trace_attributes(trace_attributes(final))
        if final.outcome is TurnOutcome.FAILED:
            yield ErrorEvent(code=final.error_code or ErrorCode.INTERNAL)
            return
        yield ReplyEvent(reply=final.reply, reply_data=_reply_data(final))
