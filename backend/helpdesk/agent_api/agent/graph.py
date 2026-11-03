"""The chat's agent loop, run as a LangGraph graph.

The technique is the loop: a model step with the tools the caller's role
allows, then a tool step whose results go back to the model, until it answers.
LangGraph is the frame: each part is a node, the arrows are edges, and a saver
keeps the state after every node, one thread per turn. Four nodes:

- `held_answer`: reads the chat and, when a change is held, has the model
  read the person's answer into one fixed value (`ReplyReading`). A yes runs
  the stored calls, then the reply is written as an answer-only step; code
  only makes sure it is the stored calls that run. A no drops the change and
  an answer-only step says so. New info drops the change and goes to the
  model as the person's answer: each held call comes back denied, with their
  words. Unclear keeps the change held and asks for a plain yes or no.
- `agent`: one model step under the `agent_loop` prompt. A step that asks for
  tools goes to `tools`; one that answers goes to `check_reply`.
- `tools`: runs lookups and finders through MCP with the caller's own token,
  back to `agent`. The lookups of one step run at the same time; finders and
  the note run after them, one by one, as they share the database session.
  Results go back in the order the model asked, trimmed for the model only.
  A proposed change is checked, given a key by code and held as the chat's
  open question, which ends the turn. The model writes the question the
  person sees, in their language; it must show every fact of the change and
  pass the reply check, with one send-back, else the code-written summary is
  shown. It is held only when every
  person and role id in it came from an ok tool result of this turn, and never
  when it is the very change the person just turned down. Bad arguments go
  back once with the reason, then `CHANGE_FAILED_REPLY`. The model never runs
  a change. `note_request` writes the request draft, shown to the model as
  data at every step.
- `check_reply`: every request number, tenant role name and decision word must
  come from this turn's tool results (role names may also be the person's own
  words), and so must every promise of future work. A breach goes back to
  `agent` with the reason, at most `MAX_SEND_BACKS` times, then code says
  the reply from the results.

Limits, the turn budget: at most `MAX_STEPS` model steps, 6 tool calls,
40,000 tokens and 90 seconds a turn. A limit hit with tool results gets one
last answer-only step; with none, the fixed `TIMEOUT_REPLY` and no model call.

The saved state is plain data. The session, the clients and the chat store
travel in the run's context, so a saved step never holds a connection.
"""

import asyncio
import contextlib
import json
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Hashable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from pydantic import ValidationError

from helpdesk.agent_api.agent import gates
from helpdesk.agent_api.agent.confirmation import (
    CREATE_ACCESS_REQUEST,
    UNCLEAR_REPLY,
    Fact,
    OpenQuestion,
    Reading,
    ReplyReading,
    change_summary,
    confirm_facts,
    missing_facts,
    question_problems,
)
from helpdesk.agent_api.agent.draft import draft_message, merge_draft
from helpdesk.agent_api.agent.events import ProgressEvent, StepEvent
from helpdesk.agent_api.agent.keys import derive_idempotency_key
from helpdesk.agent_api.agent.loop_tools import (
    FIND_DEPARTMENT,
    FIND_PERSON,
    FIND_ROLE,
    LIST_PERSON_ACCESS,
    Directory,
    LoopTool,
    ToolKind,
    find_department,
    find_person,
    find_role,
    offered_tools,
)
from helpdesk.agent_api.agent.postcheck import (
    outcome_reply,
    permitted_words,
    unbacked_numbers,
    unbacked_promises,
    unbacked_roles,
    words_in,
)
from helpdesk.agent_api.agent.progress import progress_words
from helpdesk.agent_api.agent.prompts import LoadedPrompt, load_prompt
from helpdesk.agent_api.agent.role_words import with_plain_role_names
from helpdesk.agent_api.agent.state import (
    Citation,
    Intent,
    IntentEntities,
    IntentExtraction,
    PostCheckOutcome,
    RequestDraft,
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
from helpdesk.agent_api.agent.trim import trim_for_model
from helpdesk.agent_api.agent.turn_deps import TurnDeps
from helpdesk.agent_api.services.model_gateway.client import (
    ModelAlias,
    ModelBudgetExceededError,
    ModelBusyError,
    ModelShapeError,
    ToolCall,
    ToolCallAnswer,
)
from helpdesk.agent_api.services.tracing import span
from helpdesk.shared.access.onboarding import EMAIL_MAX_LENGTH, emails_in
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.models.conversation import MessageRole

__all__ = [
    "BUSY_REPLY",
    "CHANGE_FAILED_REPLY",
    "FAILURE_REPLY",
    "LOOP_NODES",
    "LOOP_PROMPT",
    "MAX_SEND_BACKS",
    "MAX_STEPS",
    "PICK_AGAIN_REPLY",
    "TIMEOUT_REPLY",
    "LoopContext",
    "LoopGraph",
    "LoopState",
    "LoopStore",
    "compiled_graph",
]

LOOP_PROMPT = "agent_loop"
READ_REPLY_PROMPT = "read_reply"
WRITE_CONFIRM_PROMPT = "write_confirm"

MAX_PICK_WORDS = 3
"""A failed turn with nothing held this short is most likely the answer to a
question that asked the person to pick, so it gets `PICK_AGAIN_REPLY`."""

MAX_STEPS = 4
"""Model steps a turn may take; the last is answer-only, so it answers."""

MODEL_STEP_SPAN = "model_step"
"""The span name of one model step, so a speed reading finds them all."""

STEP_NODE = "agent_step"
"""The `node` a loop step streams as; the web shows it as one step."""

MAX_RESULT_CHARACTERS = 20_000
"""A tool result longer than this is cut, so one listing cannot fill the turn."""

TIMEOUT_REPLY = (
    "I ran out of time looking that up, so I have nothing to tell you yet. "
    "Please ask again, maybe in fewer words."
)
"""Said when the deadline passed before any tool answered. No model call."""

FAILURE_REPLY = (
    "Sorry, I could not work out what you need just now. Please try saying it "
    "again, or ask me to open a ticket so a person can pick it up."
)
"""Said when the model or a tool failed in a way no reply can cover. None of
the model's output is shown; the code rides on the state as `error_code`."""

PICK_AGAIN_REPLY = (
    "I am not sure which one you mean yet. Please tell me which one, by its name or number."
)
"""Said instead of `FAILURE_REPLY` when a short message with nothing held,
most likely the answer to a question that asked the person to pick, ends on a
failure (`MAX_PICK_WORDS`). The
outcome and error code stay those of the failure."""

BUSY_REPLY = (
    "I'm getting a lot of requests right now, so I could not answer that. "
    "Please wait a moment and send it again."
)
"""Said when the gateway answers 429 for any reason but a spent budget. Not a
failure: the turn ends as a finished reply, with no retry, since waiting here
would hold the request open and add load to a gateway that asked for less.
The gateway's own text is not shown."""

MAX_SEND_BACKS = 2
"""How many times a reply that names something no result backs goes back to
the model. After that, code says the reply from the results."""

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
    "cancel_request": Intent.REQUEST_ACCESS,
    "reset_own_password": Intent.RESET_PASSWORD,
    "unlock_own_account": Intent.UNLOCK_ACCOUNT,
    "create_ticket": Intent.OPEN_TICKET,
}
"""The intent a held change is stored under, for telemetry and the decision card."""

_DECISIONS = frozenset({gates.APPROVE_REQUEST, gates.REJECT_REQUEST})

ID_FIELDS = ("role_id", "target_user_id", "user_id", "manager_user_id")
"""The person and role ids a change may carry only when a tool result of this
turn returned them. Identity, tenant and session are filled by code."""

REFUSED_AGAIN = "the person refused this exact change"
"""The reason a proposal equal to a just-denied call goes back with."""

ONBOARD_USER = "onboard_user"

_EMAIL_STAND_IN = "pending@example.invalid"
"""Put in an onboarding's `email` only so the schema check can read the rest
of the call when the person typed no usable address. The call then goes back,
so it is never held."""

NO_EMAIL = (
    "email: the person has not typed the new joiner's email address in this chat; ask them for it"
)
TWO_EMAILS = (
    "email: the person's latest message holds more than one email address; "
    "ask them which address is the new joiner's"
)
LONG_EMAIL = (
    f"email: the address the person typed is longer than {EMAIL_MAX_LENGTH} characters; "
    "ask them for the right one"
)


class LoopStore(Directory, Protocol):
    """The chat row and directory reads one loop turn needs."""

    async def prepare(self, now: datetime) -> None: ...

    async def principals(self) -> tuple[str, ...]: ...

    async def history(self) -> list[tuple[str, str]]: ...

    async def open_question(self) -> OpenQuestion | None: ...

    async def clear_open_question(self) -> None: ...

    async def hold(self, question: OpenQuestion) -> None: ...

    async def add_message(self, role: MessageRole, content: str) -> None: ...

    async def request_draft(self) -> RequestDraft | None: ...

    async def save_request_draft(self, draft: RequestDraft | None) -> None: ...


LOOP_NODES = ("held_answer", "agent", "tools", "check_reply")
"""The graph's nodes, in the order a turn meets them."""

RECURSION_LIMIT = 40
"""Node runs a turn may take. The turn budget ends a turn long before this:
one `held_answer`, at most 4 steps with tools, 2 send-backs and their checks."""


class LoopState(TypedDict, total=False):
    """What the saver keeps after every node. Plain data only."""

    turn: TurnState
    started: datetime
    messages: list[dict[str, Any]]
    results: list[ToolResult]
    citations: list[Citation]
    calls: list[dict[str, Any]]
    """The tool calls the last `agent` step asked for, for `tools` to run."""
    pending: str | None
    """A reply the model wrote, for `check_reply`."""
    calls_made: int
    tokens: int
    step: int
    send_backs: int
    tools_on: bool
    resend: bool
    """Set by a send-back: the next `agent` step only rewrites the reply."""
    change_sent_back: bool
    person_said: list[str]
    roles_found: dict[str, dict[str, Any]]
    people_found: dict[str, str]
    grant_holders: dict[str, str]
    """Whose grant each id is, from `list_person_access`, so a held revoke names them."""
    denied: list[dict[str, Any]]
    corrections: dict[str, Any]
    """The held calls the person just turned down, keys left out."""
    bare_yes: bool
    """The person sent a short message with nothing held, likely answering a
    pick question."""
    final: TurnState | None
    """The turn's end state; once set, the graph stops."""


@dataclass(frozen=True)
class LoopContext:
    """What a run reaches outside itself. Passed as LangGraph's run context,
    so it is never saved."""

    deps: TurnDeps
    store: "LoopStore"
    prompt: LoadedPrompt


type LoopGraph = CompiledStateGraph[LoopState, LoopContext, LoopState, LoopState]


type _Fetched = dict[str, Any] | ToolError
"""A tool's answer, or the typed error it raised, kept to be read in order."""


def _emit(event: ProgressEvent | StepEvent) -> None:
    """Stream a progress or step event. With no stream reading it, it goes nowhere."""
    get_stream_writer()(event)


class _TurnFailed(Exception):
    """A tool or the model failed in a way no reply can cover."""

    def __init__(self, code: ErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


class _BudgetSpent(Exception):
    """The gateway refused the step because a budget is spent. The turn ends on
    its message, word for word, as a finished reply."""

    def __init__(self, reply: str) -> None:
        super().__init__("budget spent")
        self.reply = reply


class _GatewayBusy(Exception):
    """The gateway was too busy to answer the step. The turn ends on `BUSY_REPLY`."""


_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
"""Fixed English names, so the line never follows the server's locale."""


def today_line(now: datetime) -> str:
    """ "Today is 2026-11-03 (Tuesday)." It rides on the person's newest
    message, never the system prompt, so the prompt and tools stay byte for
    byte the same each day and the gateway can cache them. Pinned by
    `test_the_user_turn_starts_with_todays_date` and
    `test_the_system_prompt_is_the_same_on_two_days`."""
    today = now.date()
    return f"Today is {today.isoformat()} ({_WEEKDAYS[today.weekday()]})."


def _tool_turn(call_id: str, content: Any) -> dict[str, Any]:
    """One `tool` turn: the call's answer, as JSON text, cut if very long."""
    text = json.dumps(content, default=str)
    if len(text) > MAX_RESULT_CHARACTERS:
        text = text[:MAX_RESULT_CHARACTERS] + " ...(cut)"
    return {"role": "tool", "tool_call_id": call_id, "content": text}


async def _fetch(answer: Awaitable[Mapping[str, Any]]) -> _Fetched:
    """Wait for one tool's answer, holding a typed error as a value, so the
    step's other lookups still finish when one fails."""
    try:
        return dict(await answer)
    except ToolError as exc:
        return exc


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


def _without_key(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """A change's arguments without its idempotency key, to compare two calls."""
    return {name: value for name, value in arguments.items() if name != IDEMPOTENCY_KEY}


def _unfound_id(arguments: Mapping[str, Any], found: str) -> str | None:
    """The first id field whose value no ok result of this turn holds, or
    `None`."""
    for name in ID_FIELDS:
        value = arguments.get(name)
        if value is not None and str(value) not in found:
            return name
    return None


def _problems(exc: ValidationError) -> str:
    """A validation error as "field: what is wrong" lines the model can act on."""
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or 'arguments'}: {error['msg']}"
        for error in exc.errors()
    )


class _Turn:
    """One node's view of the turn: the saved state's working data, loaded as
    fields, plus the context's session, clients and store, which are never
    saved. A node changes the fields and hands `saved()` back as its update."""

    def __init__(self, state: LoopState, context: LoopContext) -> None:
        self.deps = context.deps
        self.store = context.store
        self.prompt = context.prompt
        self.state = state["turn"]
        self.started = state["started"]
        self.messages: list[dict[str, Any]] = list(state.get("messages", []))
        self.offered: dict[str, LoopTool] = {
            tool.name: tool for tool in offered_tools(self.state.principals)
        }
        self.results: list[ToolResult] = list(state.get("results", []))
        self.citations: list[Citation] = list(state.get("citations", []))
        self.calls: list[ToolCall] = [ToolCall(**call) for call in state.get("calls", [])]
        self.pending: str | None = state.get("pending")
        self.calls_made = state.get("calls_made", 0)
        self.tokens = state.get("tokens", 0)
        self.step = state.get("step", 0)
        self.send_backs = state.get("send_backs", 0)
        self.tools_on = state.get("tools_on", True)
        self.resend = state.get("resend", False)
        self.change_sent_back = state.get("change_sent_back", False)
        self.person_said: list[str] = list(state.get("person_said", []))
        """The person's own messages, this turn's last; never the check's notes."""
        self.roles_found: dict[str, dict[str, Any]] = dict(state.get("roles_found", {}))
        self.people_found: dict[str, str] = dict(state.get("people_found", {}))
        self.grant_holders: dict[str, str] = dict(state.get("grant_holders", {}))
        self.denied: list[dict[str, Any]] = list(state.get("denied", []))
        self.corrections: dict[str, Any] = dict(state.get("corrections", {}))
        """The filing arguments the person corrected in their answer to a held
        request; a new filing this turn carries them (`_checked_changes`)."""
        self.bare_yes = state.get("bare_yes", False)
        self.final: TurnState | None = None

    def saved(self) -> LoopState:
        """The node's update: every working field, as plain data."""
        return {
            "turn": self.state,
            "started": self.started,
            "messages": self.messages,
            "results": self.results,
            "citations": self.citations,
            "calls": [
                {"id": call.id, "name": call.name, "arguments": call.arguments}
                for call in self.calls
            ],
            "pending": self.pending,
            "calls_made": self.calls_made,
            "tokens": self.tokens,
            "step": self.step,
            "send_backs": self.send_backs,
            "tools_on": self.tools_on,
            "resend": self.resend,
            "change_sent_back": self.change_sent_back,
            "person_said": self.person_said,
            "roles_found": self.roles_found,
            "people_found": self.people_found,
            "grant_holders": self.grant_holders,
            "denied": self.denied,
            "corrections": self.corrections,
            "bare_yes": self.bare_yes,
            "final": self.final,
        }

    # --- the steps -----------------------------------------------------------

    async def _start(self) -> None:
        """Read the chat and its draft, save the person's message and set up
        the model's turns."""
        principals = await self.store.principals()
        history = await self.store.history()
        draft = await self.store.request_draft()
        await self.store.add_message(MessageRole.USER, self.state.user_message)
        self.state = self.state.model_copy(
            update={
                "principals": principals,
                "history": tuple(f"{role}: {text}" for role, text in history),
                "prompt_version": self.prompt.sha256,
                "request_draft": draft,
            }
        )
        self.person_said = [text for role, text in history if role == MessageRole.USER.value] + [
            self.state.user_message
        ]
        self.offered = {tool.name: tool for tool in offered_tools(principals)}
        self.messages = [
            {"role": "system", "content": self.prompt.text},
            *(
                {"role": role, "content": text}
                for role, text in history
                if role in (MessageRole.USER.value, MessageRole.ASSISTANT.value)
            ),
            {"role": "user", "content": f"{today_line(self.started)}\n{self.state.user_message}"},
        ]

    async def held_answer(self) -> None:
        """The model reads the answer to a held change (`_read_reply`). An
        unclear reading, or a gateway failure, keeps the question as it was
        and runs nothing. Any other reading drops the question, so it is
        answered once at most. A yes runs the stored calls as they were
        checked, keys included, never anything the model names; then one
        answer-only step writes the reply. A no runs nothing and an
        answer-only step says so. New info runs nothing and goes to the
        model as the person's answer (`_deny`)."""
        await self.store.prepare(self.started)
        question = await self.store.open_question()
        await self._start()
        if question is None:
            self.bare_yes = len(self.state.user_message.split()) <= MAX_PICK_WORDS
            return
        read = await self._read_reply(question)
        reading = read.answer
        if reading is Reading.UNCLEAR:
            self.final = await self._finish(UNCLEAR_REPLY, outcome=TurnOutcome.CONFIRM)
            return
        await self.store.clear_open_question()
        if reading is not Reading.YES:
            self._deny(question)
            if reading is Reading.NEW_INFO:
                await self._correct(question, read)
            if reading is Reading.NO:
                self.step += 1
                self.pending = (await self._ask(answer_only=True)).text or ""
            return
        self.state = self.state.model_copy(
            update={"extraction": question.extraction, "resolved": question.resolved}
        )
        self.citations = list(question.citations)
        ran: list[dict[str, Any]] = []
        calls = [
            ToolCall(id=f"held_{index}", name=call.tool, arguments=call.arguments)
            for index, call in enumerate(question.calls)
        ]
        self.messages.append(_assistant_turn(None, calls))
        for call in calls:
            self.messages.append(await self._run_one(call, ran))
        await self._read_after_refused_cancel(ran)
        await self._clear_draft_once_filed()
        _emit(StepEvent(node=STEP_NODE, round=self.step, tools=ran))
        self.step += 1
        self.pending = (await self._ask(answer_only=True)).text or ""

    async def agent(self) -> None:
        """One model step. After a send-back it only rewrites the reply, as an
        answer-only step. Otherwise a spent budget with no results ends on `TIMEOUT_REPLY`
        and no model call, and the last step is answer-only, so it answers."""
        if self.resend:
            self.resend = False
            self.pending = (await self._ask(answer_only=True)).text or ""
            return
        self.step += 1
        spent = self._spent()
        if spent and not self.results:
            self.final = await self._finish(TIMEOUT_REPLY)
            return
        last = spent or not self.tools_on or self.step >= MAX_STEPS
        answer = await self._ask(answer_only=last)
        if last or not answer.calls:
            self.pending = answer.text or ""
            return
        self.messages.append(_assistant_turn(answer.text, answer.calls))
        self.calls = list(answer.calls)

    async def tools(self) -> None:
        """Run the step's lookups and check its changes. A held change ends
        the turn; a change is never run here."""
        calls, self.calls = self.calls, []
        _emit(ProgressEvent(round=self.step, text=progress_words([call.name for call in calls])))
        ended, ran = await self._run_calls(calls)
        if ran:
            _emit(StepEvent(node=STEP_NODE, round=self.step, tools=ran))
        if ended is not None:
            self.final = ended
        elif self.calls_made >= gates.MAX_TOOL_CALLS:
            self.tools_on = False

    async def check_reply(self) -> None:
        """Hold the reply to this turn's tool results. A breach goes back with
        the reason at most `MAX_SEND_BACKS` times, then the reply is said from
        the results by code."""
        reply = (self.pending or "").strip()
        self.pending = None
        if reply:
            unbacked = await self._unbacked(reply)
            if not unbacked:
                passed = (
                    PostCheckOutcome.REGENERATED if self.send_backs else PostCheckOutcome.PASSED
                )
                self.final = await self._finish(reply, postcheck=passed)
                return
            if self.send_backs < MAX_SEND_BACKS:
                self.send_backs += 1
                self.resend = True
                self.messages.extend(
                    [
                        {"role": "assistant", "content": reply},
                        {
                            "role": "user",
                            "content": (
                                f"Check failed: your reply names {', '.join(unbacked)}, but no "
                                "tool result in this turn says so. Write the reply again "
                                "without claiming it."
                            ),
                        },
                    ]
                )
                return
        self.final = await self._finish(
            outcome_reply(self._state()), postcheck=PostCheckOutcome.FALLBACK
        )

    async def fail(self, code: ErrorCode) -> None:
        """End the turn on `FAILURE_REPLY`, or `PICK_AGAIN_REPLY` after a short
        message with nothing held; none of the model's output is shown."""
        reply = PICK_AGAIN_REPLY if self.bare_yes else FAILURE_REPLY
        self.final = await self._finish(reply, outcome=TurnOutcome.FAILED, error_code=code)

    async def budget_spent(self, reply: str) -> None:
        """End the turn on the gateway's budget message as a finished reply."""
        self.final = await self._finish(reply)

    async def busy(self) -> None:
        """End the turn on `BUSY_REPLY` as a finished reply, outcome `BUSY`."""
        self.final = await self._finish(BUSY_REPLY, outcome=TurnOutcome.BUSY)

    async def _read_reply(self, question: OpenQuestion) -> ReplyReading:
        """One strict schema call on the default alias: the shown question and
        the person's message go as data, and the answer is one fixed value. A
        reply that fails the shape twice reads as unclear, never as yes."""
        prompt = load_prompt(READ_REPLY_PROMPT)
        said = json.dumps({"question": question.asked, "message": self.state.user_message})
        try:
            with span(
                MODEL_STEP_SPAN,
                attributes={"model_alias": ModelAlias.AGENT_DEFAULT.value, "step": 0},
            ):
                answer = await self.deps.model.complete_json(
                    ReplyReading,
                    [{"role": "system", "content": prompt.text}, {"role": "user", "content": said}],
                    alias=ModelAlias.AGENT_DEFAULT,
                    tenant_slug=self.state.tenant_slug,
                    session_id=str(self.state.session_id),
                    user=self.state.gateway_user,
                    prompt_version=prompt.sha256,
                )
        except ModelShapeError:
            return ReplyReading(answer=Reading.UNCLEAR)
        except ModelBudgetExceededError as exc:
            raise _BudgetSpent(exc.gateway_message) from exc
        except ModelBusyError as exc:
            raise _GatewayBusy from exc
        except ToolError as exc:
            raise _TurnFailed(exc.code) from exc
        self.tokens += answer.usage.total_tokens
        return answer.value

    async def _correct(self, question: OpenQuestion, read: ReplyReading) -> None:
        """A correction to a held filing goes into the request draft before
        the model plans again: the held role, reason and days, with what the
        person changed laid over them. The corrected days and reason are also
        forced onto the next filing this turn, so the old value cannot be
        asked back. A correction outside the draft's limits is dropped; the
        model still reads the person's words. Pinned by
        `test_a_new_length_updates_the_held_request`."""
        create = next(
            (call.arguments for call in question.calls if call.tool == CREATE_ACCESS_REQUEST),
            None,
        )
        if create is None:
            return
        held = {
            "role": question.resolved.role_name,
            "reason": create.get("justification"),
            "days": create.get("requested_expiry_days"),
        }
        changed = {
            name: value
            for name, value in (("role", read.role), ("reason", read.reason), ("days", read.days))
            if value is not None
        }
        try:
            draft = merge_draft(
                self.state.request_draft,
                {**{k: v for k, v in held.items() if v is not None}, **changed},
                now=self.deps.now(),
            )
        except ValidationError:
            return
        await self.store.save_request_draft(draft)
        self.state = self.state.model_copy(update={"request_draft": draft})
        if read.days is not None:
            self.corrections["requested_expiry_days"] = read.days
        if read.reason is not None:
            self.corrections["justification"] = read.reason

    def _deny(self, question: OpenQuestion) -> None:
        """Give the model the held calls answered as denied, with the person's
        own words as the reason, right before their message, so it re-plans.
        The calls are kept so this turn cannot hold one of them again."""
        calls = [
            ToolCall(id=f"held_{index}", name=call.tool, arguments=call.arguments)
            for index, call in enumerate(question.calls)
        ]
        denial = {"denied_by_person": True, "reason": self.state.user_message}
        self.messages[-1:-1] = [
            _assistant_turn(None, calls),
            *(_tool_turn(call.id, denial) for call in calls),
        ]
        self.denied = [
            {"tool": call.tool, "arguments": _without_key(call.arguments)}
            for call in question.calls
        ]

    async def _clear_draft_once_filed(self) -> None:
        """A filing that ran ok ends the draft, so the next request starts blank."""
        filed = any(result.ok and result.tool == "create_access_request" for result in self.results)
        if filed and self.state.request_draft is not None:
            await self.store.save_request_draft(None)
            self.state = self.state.model_copy(update={"request_draft": None})

    async def _read_after_refused_cancel(self, ran: list[dict[str, Any]]) -> None:
        """A held cancel the server found already decided gets one fresh
        `list_my_requests`, since the reply step runs no tools. The reply then
        reads the real status, and offers removal only when access was given."""
        refused = any(
            step["tool"] == gates.CANCEL_REQUEST
            and step["error_code"] == ErrorCode.REQUEST_NOT_PENDING.value
            for step in ran
        )
        if not refused:
            return
        call = ToolCall(id="held_read", name=gates.LIST_MY_REQUESTS, arguments={})
        self.messages.append(_assistant_turn(None, [call]))
        self.messages.append(await self._run_one(call, ran))

    async def _run_one(self, call: ToolCall, ran: list[dict[str, Any]]) -> dict[str, Any]:
        """Run one call code already checked, a stored change or a fresh read,
        through MCP with the caller's own token, and keep its result."""
        arguments = TOOL_INPUTS[call.name].model_validate(call.arguments)
        self.calls_made += 1
        return self._keep(call, await _fetch(self.deps.tools.call_tool(call.name, arguments)), ran)

    def _spent(self) -> bool:
        """Whether the deadline or the token budget is used up. The deadline
        is inclusive: the 90th second is still in time."""
        elapsed = (self.deps.now() - self.started).total_seconds()
        return elapsed > gates.TURN_DEADLINE_SECONDS or self.tokens > gates.MAX_TURN_TOKENS

    async def _ask(self, *, answer_only: bool) -> ToolCallAnswer:
        """One model step on the default alias. Every step sends the same
        offered tools; an answer-only step adds `tool_choice: "none"`, and only
        its text is read, so a tool call sent back anyway runs nothing. The draft,
        when there is one, goes just before the person's newest message, so the
        prompt and the earlier chat stay the same from step to step and the
        gateway can cache them."""
        messages = [dict(message) for message in self.messages]
        if self.state.request_draft is not None:
            newest = max(
                index for index, message in enumerate(messages) if message["role"] == "user"
            )
            messages.insert(
                newest, {"role": "system", "content": draft_message(self.state.request_draft)}
            )
        try:
            with span(
                MODEL_STEP_SPAN,
                attributes={"model_alias": ModelAlias.AGENT_DEFAULT.value, "step": self.step or 1},
            ):
                answer = await self.deps.model.complete_with_tools(
                    messages,
                    [tool.wire() for tool in self.offered.values()],
                    alias=ModelAlias.AGENT_DEFAULT,
                    tenant_slug=self.state.tenant_slug,
                    session_id=str(self.state.session_id),
                    user=self.state.gateway_user,
                    prompt_version=self.prompt.sha256,
                    answer_only=answer_only,
                )
        except ModelBudgetExceededError as exc:
            raise _BudgetSpent(exc.gateway_message) from exc
        except ModelBusyError as exc:
            raise _GatewayBusy from exc
        except ToolError as exc:
            raise _TurnFailed(exc.code) from exc
        self.tokens += answer.usage.total_tokens
        return answer

    async def _run_calls(
        self, calls: Sequence[ToolCall]
    ) -> tuple[TurnState | None, list[dict[str, Any]]]:
        """Run the step's lookups and check its changes. A held change ends
        the turn, which is the first value; the second is the step's results
        as the stream shows them.

        Every call's arguments and the call limit are checked in the order the
        model asked, before any lookup starts. Then the lookups run at the same
        time on the turn's MCP session, and the finders and the note one by one
        after them, since those share the database session. Results are kept
        in the order asked, whatever order they finish in, and an `INTERNAL`
        failure ends the turn only once every lookup has finished."""
        answers: dict[str, dict[str, Any]] = {}
        changes: list[ToolCall] = []
        lookups: dict[str, Awaitable[_Fetched]] = {}
        one_by_one: list[ToolCall] = []
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
            elif tool.kind is ToolKind.NOTE:
                self.calls_made += 1
                one_by_one.append(call)
            elif tool.kind is ToolKind.FIND:
                words = call.arguments.get("words")
                if not isinstance(words, str) or set(call.arguments) != {"words"}:
                    answers[call.id] = _tool_turn(
                        call.id, {"error": "bad_arguments", "reason": "words: one string"}
                    )
                    continue
                self.calls_made += 1
                one_by_one.append(call)
            else:
                try:
                    arguments = TOOL_INPUTS[call.name].model_validate(call.arguments)
                except ValidationError as exc:
                    answers[call.id] = _tool_turn(
                        call.id, {"error": "bad_arguments", "reason": _problems(exc)}
                    )
                    continue
                self.calls_made += 1
                lookups[call.id] = _fetch(self.deps.tools.call_tool(call.name, arguments))
        fetched = dict(zip(lookups, await asyncio.gather(*lookups.values()), strict=True))
        noted: dict[str, list[dict[str, Any]]] = {}
        for call in one_by_one:
            if self.offered[call.name].kind is ToolKind.NOTE:
                noted[call.id] = []
                answers[call.id] = await self._note(call, noted[call.id])
            else:
                fetched[call.id] = await _fetch(self._find(call.name, call.arguments["words"]))
        ran: list[dict[str, Any]] = []
        for call in calls:
            if call.id in fetched:
                answers[call.id] = self._keep(call, fetched[call.id], ran)
            ran.extend(noted.get(call.id, []))
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

    def _keep(self, call: ToolCall, fetched: _Fetched, ran: list[dict[str, Any]]) -> dict[str, Any]:
        """Keep one tool's answer whole as a result, and give the model its
        trimmed copy. A typed refusal is an answer the model reads; only
        `INTERNAL` ends the turn."""
        if isinstance(fetched, ToolError):
            if fetched.code is ErrorCode.INTERNAL:
                raise _TurnFailed(fetched.code) from fetched
            self.results.append(ToolResult(tool=call.name, ok=False, error_code=fetched.code))
            ran.append({"tool": call.name, "ok": False, "error_code": fetched.code.value})
            return _tool_turn(call.id, {"error": fetched.code.value})
        self.results.append(ToolResult(tool=call.name, ok=True, payload=fetched))
        ran.append({"tool": call.name, "ok": True, "error_code": None})
        self._remember(call.name, fetched, call.arguments)
        return _tool_turn(call.id, trim_for_model(with_plain_role_names(fetched)))

    async def _note(self, call: ToolCall, ran: list[dict[str, Any]]) -> dict[str, Any]:
        """Lay the call over the request draft and save it. A bad value goes
        back with the reason and the old draft stays. Not kept as a result, so
        nothing the model notes can back an id or a claim."""
        try:
            draft = merge_draft(self.state.request_draft, call.arguments, now=self.deps.now())
        except ValidationError as exc:
            ran.append({"tool": call.name, "ok": False, "error_code": None})
            return _tool_turn(call.id, {"error": "bad_arguments", "reason": _problems(exc)})
        await self.store.save_request_draft(draft)
        self.state = self.state.model_copy(update={"request_draft": draft})
        ran.append({"tool": call.name, "ok": True, "error_code": None})
        shown = draft.model_dump(mode="json", exclude={"saved_at"}, exclude_none=True)
        return _tool_turn(call.id, {"ok": True, "draft": shown})

    async def _find(self, name: str, words: str) -> dict[str, Any]:
        if name == FIND_ROLE:
            return {"roles": await find_role(words, tools=self.deps.tools, directory=self.store)}
        if name == FIND_PERSON:
            return {"people": await find_person(words, directory=self.store)}
        if name == FIND_DEPARTMENT:
            return {"departments": await find_department(words, directory=self.store)}
        raise _TurnFailed(ErrorCode.INTERNAL)  # pragma: no cover - only three finders exist

    def _remember(
        self, name: str, payload: Mapping[str, Any], arguments: Mapping[str, Any]
    ) -> None:
        """Keep found roles, people and whose grants were listed for the
        summary, and passages for Sources."""
        if name == FIND_ROLE:
            for row in payload.get("roles") or []:
                self.roles_found[str(row["role_id"])] = row
        elif name == FIND_PERSON:
            for row in payload.get("people") or []:
                self.people_found[str(row["user_id"])] = str(row["name"])
        elif name == LIST_PERSON_ACCESS:
            for row in payload.get("grants") or []:
                self.grant_holders[str(row["grant_id"])] = str(arguments.get("user_id"))
        elif name == SEARCH_POLICY:
            with contextlib.suppress(ValidationError):
                self.citations.extend(SearchPolicyResult.model_validate(payload).citations)

    # --- changes -------------------------------------------------------------

    def _checked_changes(
        self, calls: Sequence[ToolCall]
    ) -> tuple[list[ValidatedCall], dict[str, str]]:
        """The step's changes with their keys, all or none, and the reason per
        call when any is wrong. Code adds the key; a model that names one is
        wrong. After the schema, every id must come from this turn's ok results
        (`_unfound_id`), an onboarding's email is the one the person typed
        (`_typed_email`), and a just-denied call is refused again."""
        listed = gates.listed_request_ids(tuple(self.results))
        found = " ".join(
            json.dumps(result.payload, default=str) for result in self.results if result.ok
        )
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
            email_problem = None
            if call.name == ONBOARD_USER:
                email, email_problem = self._typed_email()
                call_arguments = {**call.arguments, "email": email or _EMAIL_STAND_IN}
            elif call.name == CREATE_ACCESS_REQUEST:
                call_arguments = {**call.arguments, **self.corrections}
            else:
                call_arguments = dict(call.arguments)
            arguments = {
                **call_arguments,
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
            dumped = checked.model_dump(mode="json")
            unfound = _unfound_id(dumped, found)
            if unfound is not None:
                reasons[call.id] = (
                    f"{unfound}: not returned by a lookup this turn; look it up first"
                )
                continue
            if email_problem is not None:
                reasons[call.id] = email_problem
                continue
            if {"tool": call.name, "arguments": _without_key(dumped)} in self.denied:
                reasons[call.id] = REFUSED_AGAIN
                continue
            validated.append(ValidatedCall(tool=call.name, arguments=dumped))
        if reasons:
            for call in calls:
                reasons.setdefault(call.id, "held back: another change in this step was wrong")
            return [], reasons
        return validated, {}

    def _typed_email(self) -> tuple[str | None, str | None]:
        """The new joiner's address from the person's own latest message that
        holds one, and `None`; or `None` and the reason it cannot be used.
        Code fills it, never the model: the gateway masks addresses, so the
        model only sees a placeholder. No reason holds an address."""
        for said in reversed(self.person_said):
            found = emails_in(said)
            if not found:
                continue
            if len(found) > 1:
                return None, TWO_EMAILS
            if len(found[0]) > EMAIL_MAX_LENGTH:
                return None, LONG_EMAIL
            return found[0], None
        return None, NO_EMAIL

    async def _hold(self, calls: list[ValidatedCall]) -> TurnState:
        """Store the checked changes as the open question and say what a yes runs."""
        first = calls[0]
        target = str(first.arguments.get("target_user_id") or "")
        if first.tool == "revoke_access":
            target = self.grant_holders.get(str(first.arguments.get("grant_id")), "")
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
        summary = change_summary(calls, tuple(self.results), resolved, person)
        reply = await self._written_question(
            summary, confirm_facts(calls, self.results, resolved, person)
        )
        await self.store.hold(
            OpenQuestion(
                asked_at=self.deps.now(),
                message=self.state.user_message,
                calls=tuple(calls),
                extraction=extraction,
                resolved=resolved,
                citations=tuple(self.citations),
                asked=reply,
            )
        )
        self.state = self.state.model_copy(update={"extraction": extraction, "resolved": resolved})
        return await self._finish(reply, outcome=TurnOutcome.CONFIRM)

    async def _written_question(self, summary: str, facts: Sequence[Fact]) -> str:
        """The model's question for a held change, in the person's language.
        It must show every fact word for word and pass the reply check; a miss
        goes back once with what is wrong. A second miss, or any gateway
        failure, gives the code-written `summary`, so a change is never shown
        without its facts. The calls are stored already, so the question
        cannot change what a yes runs."""
        prompt = load_prompt(WRITE_CONFIRM_PROMPT)
        given = json.dumps(
            {
                "summary": summary,
                "facts": [fact.text for fact in facts],
                "message": self.state.user_message,
                "first_message": self.person_said[0],
            },
            ensure_ascii=False,
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": prompt.text},
            {"role": "user", "content": given},
        ]
        for attempt in range(2):
            try:
                with span(
                    MODEL_STEP_SPAN,
                    attributes={"model_alias": ModelAlias.AGENT_DEFAULT.value, "step": self.step},
                ):
                    answer = await self.deps.model.complete_with_tools(
                        messages,
                        [],
                        alias=ModelAlias.AGENT_DEFAULT,
                        tenant_slug=self.state.tenant_slug,
                        session_id=str(self.state.session_id),
                        user=self.state.gateway_user,
                        prompt_version=prompt.sha256,
                    )
            except ToolError:
                return summary
            self.tokens += answer.usage.total_tokens
            question = (answer.text or "").strip()
            problems = [
                *(f'"{fact}" (missing)' for fact in missing_facts(question, facts)),
                *question_problems(question),
                *(await self._unbacked(question) if question else ["nothing"]),
            ]
            if question and not problems:
                return question
            if attempt == 0:
                messages.extend(
                    [
                        {"role": "assistant", "content": question},
                        {
                            "role": "user",
                            "content": (
                                f"Check failed: {', '.join(problems)}. Write the question "
                                "again, copying every fact exactly, claiming nothing "
                                "is done, and writing only the one question."
                            ),
                        },
                    ]
                )
        return summary

    # --- the answer ----------------------------------------------------------

    async def _unbacked(self, reply: str) -> list[str]:
        """Every decision word, request number, tenant role name and promise
        of future work in `reply` that this turn's results do not back. Role
        names may also be the person's own words, this turn or earlier."""
        checked = self._state()
        roles = list(await self.store.role_ids())
        return [
            *sorted(words_in(reply) - permitted_words(checked)),
            *unbacked_numbers(reply, checked.tool_results),
            *unbacked_roles(reply, roles, checked.tool_results, self.person_said),
            *(
                f'the promise "{promise}"'
                for promise in unbacked_promises(reply, checked.tool_results)
            ),
        ]

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


type _Step = Callable[[_Turn], Coroutine[Any, Any, None]]


class _NodeFn(Protocol):
    """A node as LangGraph calls it: the state, and the run's context by keyword."""

    def __call__(
        self, state: LoopState, *, runtime: Runtime[LoopContext]
    ) -> Coroutine[Any, Any, LoopState]: ...


def _node(step: _Step) -> _NodeFn:
    """A `_Turn` step as a node: load the state, run the step, hand back the
    update. A spent budget ends the turn on the gateway's message, word for
    word. A busy gateway ends it on `BUSY_REPLY`. A failure no reply can
    cover ends the turn on `FAILURE_REPLY`."""

    async def run(state: LoopState, *, runtime: Runtime[LoopContext]) -> LoopState:
        turn = _Turn(state, runtime.context)
        try:
            await step(turn)
        except _BudgetSpent as spent:
            await turn.budget_spent(spent.reply)
        except _GatewayBusy:
            await turn.busy()
        except _TurnFailed as failed:
            await turn.fail(failed.code)
        return turn.saved()

    return run


_END: Literal["__end__"] = "__end__"
"""LangGraph's `END`, spelled as the literal the routes return."""


def _after_held_answer(state: LoopState) -> Literal["agent", "check_reply", "__end__"]:
    if state.get("final") is not None:
        return _END
    return "check_reply" if state.get("pending") is not None else "agent"


def _after_agent(state: LoopState) -> Literal["tools", "check_reply", "__end__"]:
    if state.get("final") is not None:
        return _END
    return "check_reply" if state.get("pending") is not None else "tools"


def _after_tools(state: LoopState) -> Literal["agent", "__end__"]:
    return _END if state.get("final") is not None else "agent"


def _after_check(state: LoopState) -> Literal["agent", "__end__"]:
    return _END if state.get("final") is not None else "agent"


def _build(checkpointer: BaseCheckpointSaver[Any] | None) -> LoopGraph:
    graph = StateGraph(LoopState, context_schema=LoopContext)
    graph.add_node("held_answer", _node(_Turn.held_answer))
    graph.add_node("agent", _node(_Turn.agent))
    graph.add_node("tools", _node(_Turn.tools))
    graph.add_node("check_reply", _node(_Turn.check_reply))
    graph.add_edge(START, "held_answer")
    graph.add_conditional_edges("held_answer", _after_held_answer, ["agent", "check_reply", END])
    graph.add_conditional_edges("agent", _after_agent, ["tools", "check_reply", END])
    graph.add_conditional_edges("tools", _after_tools, ["agent", END])
    graph.add_conditional_edges("check_reply", _after_check, ["agent", END])
    return graph.compile(checkpointer=checkpointer)


_COMPILED: dict[int, tuple[Hashable | None, LoopGraph]] = {}


def compiled_graph(checkpointer: BaseCheckpointSaver[Any] | None = None) -> LoopGraph:
    """The loop's graph, compiled once per saver. `None` is a graph that saves
    nothing, for tests and the eval route. The saver is kept beside its graph,
    so its id is never reused while the entry lives."""
    key = id(checkpointer)
    found = _COMPILED.get(key)
    if found is None:
        found = (checkpointer, _build(checkpointer))
        _COMPILED[key] = found
    return found[1]
