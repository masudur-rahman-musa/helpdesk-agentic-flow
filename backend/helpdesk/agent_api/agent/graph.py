"""The compiled turn graph and the functions a route calls. The only module
here that imports LangGraph; routing functions only read a node's `outcome`."""

import uuid
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import contextmanager
from functools import wraps
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from helpdesk.agent_api.agent.checkpointer import thread_config
from helpdesk.agent_api.agent.events import ErrorEvent, ReplyEvent, StepEvent, TurnEvent
from helpdesk.agent_api.agent.nodes.await_approval import await_approval, pending_reply
from helpdesk.agent_api.agent.nodes.compose_reply import COMPOSITION_PROMPT, compose_reply
from helpdesk.agent_api.agent.nodes.endings import (
    ask_clarifying,
    ask_confirmation,
    ask_disambiguation,
    fail_turn,
    refuse_and_ticket,
)
from helpdesk.agent_api.agent.nodes.execute_tools import execute_tools
from helpdesk.agent_api.agent.nodes.extract_intent import (
    EXTRACTION_PROMPT,
    FAILURE_REPLY,
    extract_intent,
)
from helpdesk.agent_api.agent.nodes.load_session import load_session
from helpdesk.agent_api.agent.nodes.plan_tools import PLANNING_PROMPT, plan_tools
from helpdesk.agent_api.agent.nodes.resolve_entities import resolve_entities
from helpdesk.agent_api.agent.nodes.retrieve_policy import retrieve_policy
from helpdesk.agent_api.agent.nodes.validate_tool_calls import validate_tool_calls
from helpdesk.agent_api.agent.prompts import load_prompt
from helpdesk.agent_api.agent.state import ToolResult, TurnOutcome, TurnState
from helpdesk.agent_api.agent.turn_deps import TurnDeps
from helpdesk.agent_api.agent.turn_metrics import (
    record_turn_code,
    record_turn_state,
    trace_attributes,
)
from helpdesk.agent_api.services.decision_card import decision_card
from helpdesk.agent_api.services.model_gateway.client import ModelAlias
from helpdesk.agent_api.services.tracing import (
    current_trace_id,
    set_trace_attributes,
    span,
    tracing_enabled,
    turn_trace,
)
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.core.observability.context import bind_context, current_context
from helpdesk.shared.crud.messages import append_message
from helpdesk.shared.models.conversation import MessageRole, check_message_content

__all__ = [
    "FAILURE_REPLY",
    "GRAPH_RECURSION_LIMIT",
    "Saver",
    "TurnDeps",
    "TurnEvent",
    "build_graph",
    "resume_graph",
    "run_turn",
    "stream_turn",
]

type Saver = BaseCheckpointSaver[Any]
"""Named here so other modules can type a saver without importing LangGraph."""

LOAD_SESSION = "load_session"
EXTRACT_INTENT = "extract_intent"
RESOLVE_ENTITIES = "resolve_entities"
RETRIEVE_POLICY = "retrieve_policy"
PLAN_TOOLS = "plan_tools"
VALIDATE_TOOL_CALLS = "validate_tool_calls"
EXECUTE_TOOLS = "execute_tools"
AWAIT_APPROVAL = "await_approval"
COMPOSE_REPLY = "compose_reply"
REFUSE_AND_TICKET = "refuse_and_ticket"
ASK_CLARIFYING = "ask_clarifying"
ASK_DISAMBIGUATION = "ask_disambiguation"
ASK_CONFIRMATION = "ask_confirmation"
FAIL_TURN = "fail_turn"

_CachedGraph = CompiledStateGraph[TurnState, None, TurnState, TurnState]

_NO_SAVER_GRAPH: _CachedGraph | None = None
"""The shared graph with no checkpointer."""

_GRAPHS_BY_SAVER: weakref.WeakKeyDictionary[Saver, _CachedGraph] = weakref.WeakKeyDictionary()
"""Weak keys, so a short-lived test saver does not keep its graph alive."""

GRAPH_RECURSION_LIMIT = 50
"""A backstop behind the turn budgets. LangGraph's default of 25 is below
what an allowed turn can visit, so the budgets stop the loop, not this."""

_DEPS_KEY = "deps"
"""Deps ride on the run config, not the state: the state is saved to
Postgres, and a database session cannot be saved or rebuilt on resume."""


_INTERRUPT_KEY = "__interrupt__"
"""Added by LangGraph when a node paused the run. A paused run does not
raise, so ignoring this would treat a waiting turn as done."""


def _paused_state(result: Mapping[str, Any]) -> TurnState:
    """A paused run as a turn state. The reply is built here because
    `interrupt()` drops the pausing node's own update."""
    state = TurnState.model_validate(
        {key: value for key, value in result.items() if not key.startswith("__")}
    )
    return state.model_copy(update={"reply": pending_reply(state)})


def _deps(config: RunnableConfig) -> TurnDeps:
    """`TurnDeps` back off the run config."""
    return cast(TurnDeps, config["configurable"][_DEPS_KEY])


async def _load_session(state: TurnState, config: RunnableConfig) -> dict[str, Any]:
    return (await load_session(state, _deps(config))).model_dump()


async def _extract_intent(state: TurnState, config: RunnableConfig) -> dict[str, Any]:
    return (await extract_intent(state, _deps(config))).model_dump()


async def _resolve_entities(state: TurnState, config: RunnableConfig) -> dict[str, Any]:
    return (await resolve_entities(state, _deps(config))).model_dump()


async def _retrieve_policy(state: TurnState, config: RunnableConfig) -> dict[str, Any]:
    return (await retrieve_policy(state, _deps(config))).model_dump()


async def _plan_tools(state: TurnState, config: RunnableConfig) -> dict[str, Any]:
    return (await plan_tools(state, _deps(config))).model_dump()


async def _validate_tool_calls(state: TurnState, config: RunnableConfig) -> dict[str, Any]:
    return (await validate_tool_calls(state, _deps(config))).model_dump()


async def _execute_tools(state: TurnState, config: RunnableConfig) -> dict[str, Any]:
    return (await execute_tools(state, _deps(config))).model_dump()


async def _await_approval(state: TurnState, config: RunnableConfig) -> dict[str, Any]:
    return (await await_approval(state, _deps(config))).model_dump()


async def _compose_reply(state: TurnState, config: RunnableConfig) -> dict[str, Any]:
    return (await compose_reply(state, _deps(config))).model_dump()


async def _refuse_and_ticket(state: TurnState, config: RunnableConfig) -> dict[str, Any]:
    return (await refuse_and_ticket(state, _deps(config))).model_dump()


async def _ask_clarifying(state: TurnState, config: RunnableConfig) -> dict[str, Any]:
    return (await ask_clarifying(state, _deps(config))).model_dump()


async def _ask_disambiguation(state: TurnState, config: RunnableConfig) -> dict[str, Any]:
    return (await ask_disambiguation(state, _deps(config))).model_dump()


async def _ask_confirmation(state: TurnState, config: RunnableConfig) -> dict[str, Any]:
    return (await ask_confirmation(state, _deps(config))).model_dump()


async def _fail_turn(state: TurnState, config: RunnableConfig) -> dict[str, Any]:
    return (await fail_turn(state, _deps(config))).model_dump()


_NODE_MODEL_ALIASES: dict[str, ModelAlias] = {
    EXTRACT_INTENT: ModelAlias.AGENT_DEFAULT,
    PLAN_TOOLS: ModelAlias.AGENT_STRONG,
    COMPOSE_REPLY: ModelAlias.AGENT_DEFAULT,
}
"""Which alias each model-calling node uses, for its span."""

_NODE_PROMPTS: dict[str, str] = {
    EXTRACT_INTENT: EXTRACTION_PROMPT,
    PLAN_TOOLS: PLANNING_PROMPT,
    COMPOSE_REPLY: COMPOSITION_PROMPT,
}
"""Which prompt each node loads; its digest ties a trace to the exact text."""


def _node_attributes(node: str) -> dict[str, Any]:
    """A node's extra span values, built only when tracing is on, since the
    prompt file is read fresh each time."""
    if not tracing_enabled():
        return {}
    attributes: dict[str, Any] = {}
    alias = _NODE_MODEL_ALIASES.get(node)
    if alias is not None:
        attributes["model_alias"] = alias.value
    prompt = _NODE_PROMPTS.get(node)
    if prompt is not None:
        attributes["prompt_version"] = load_prompt(prompt).sha256
    return attributes


type _Node = Callable[[TurnState, RunnableConfig], Awaitable[dict[str, Any]]]


def _traced(name: str, node: _Node) -> _Node:
    """The node wrapped in one span. `wraps` matters: LangGraph reads the
    signature to decide whether to pass the config."""

    @wraps(node)
    async def run(state: TurnState, config: RunnableConfig) -> dict[str, Any]:
        with span(name, attributes=_node_attributes(name)):
            return await node(state, config)

    return run


@contextmanager
def _turn_telemetry() -> Any:
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


def _after_session(state: TurnState) -> str:
    """Route on `outcome`: a yes runs the stored calls, a bare no ends, and
    anything else is read by the model as usual."""
    if state.outcome is TurnOutcome.CONFIRMED:
        return EXECUTE_TOOLS
    if state.outcome is TurnOutcome.CLARIFY:
        return ASK_CLARIFYING
    return EXTRACT_INTENT


def _after_intent(state: TurnState) -> str:
    """Route on `outcome`."""
    if state.outcome is TurnOutcome.FAILED:
        return FAIL_TURN
    if state.outcome is TurnOutcome.CLARIFY:
        return ASK_CLARIFYING
    return RESOLVE_ENTITIES


def _after_entities(state: TurnState) -> str:
    """Route on `outcome`."""
    if state.outcome is TurnOutcome.CLARIFY:
        return ASK_CLARIFYING
    if state.outcome is TurnOutcome.DISAMBIGUATE:
        return ASK_DISAMBIGUATION
    return RETRIEVE_POLICY


def _after_retrieval(state: TurnState) -> str:
    """Route on `outcome`."""
    if state.outcome is TurnOutcome.FAILED:
        return FAIL_TURN
    if state.outcome is TurnOutcome.REFUSE:
        return REFUSE_AND_TICKET
    return PLAN_TOOLS


def _after_planning(state: TurnState) -> str:
    """Route on `outcome`. A spent budget refuses if nothing was done yet, and
    replies about the results otherwise."""
    if state.outcome is TurnOutcome.FAILED:
        return FAIL_TURN
    if state.outcome is TurnOutcome.REFUSE:
        return REFUSE_AND_TICKET
    if state.outcome is TurnOutcome.DONE:
        return COMPOSE_REPLY
    return VALIDATE_TOOL_CALLS


def _after_validation(state: TurnState) -> str:
    """Route on `outcome`."""
    if state.outcome is TurnOutcome.REFUSE:
        return REFUSE_AND_TICKET
    if state.outcome is TurnOutcome.DONE:
        return COMPOSE_REPLY
    if state.outcome is TurnOutcome.CONFIRM:
        return ASK_CONFIRMATION
    return EXECUTE_TOOLS


def _after_execution(state: TurnState) -> str:
    """Route on `outcome`: loop back to planning, pause for approval, or fail.
    Other tool errors are kept as results and replied about."""
    if state.outcome is TurnOutcome.FAILED:
        return FAIL_TURN
    if state.outcome is TurnOutcome.AWAIT_APPROVAL:
        return AWAIT_APPROVAL
    return PLAN_TOOLS


def _compile_graph(
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> CompiledStateGraph[TurnState, None, TurnState, TurnState]:
    """Build a new compiled graph; only `build_graph` calls this.

    The loop is plan, validate, execute, plan, ended by an empty round, a spent
    budget or the recursion limit. Only a resume walks from `await_approval` to
    `compose_reply`, skipping execution: the last approval already made the
    grant. No default checkpointer, so paused turns never sit in one process.
    """
    # LangGraph runs these nodes fine; no `add_node` overload types them.
    graph = StateGraph(TurnState)
    graph.add_node(LOAD_SESSION, _traced(LOAD_SESSION, _load_session))  # type: ignore[call-overload]
    graph.add_node(EXTRACT_INTENT, _traced(EXTRACT_INTENT, _extract_intent))  # type: ignore[call-overload]
    graph.add_node(RESOLVE_ENTITIES, _traced(RESOLVE_ENTITIES, _resolve_entities))  # type: ignore[call-overload]
    graph.add_node(RETRIEVE_POLICY, _traced(RETRIEVE_POLICY, _retrieve_policy))  # type: ignore[call-overload]
    graph.add_node(PLAN_TOOLS, _traced(PLAN_TOOLS, _plan_tools))  # type: ignore[call-overload]
    graph.add_node(VALIDATE_TOOL_CALLS, _traced(VALIDATE_TOOL_CALLS, _validate_tool_calls))  # type: ignore[call-overload]
    graph.add_node(EXECUTE_TOOLS, _traced(EXECUTE_TOOLS, _execute_tools))  # type: ignore[call-overload]
    graph.add_node(AWAIT_APPROVAL, _traced(AWAIT_APPROVAL, _await_approval))  # type: ignore[call-overload]
    graph.add_node(COMPOSE_REPLY, _traced(COMPOSE_REPLY, _compose_reply))  # type: ignore[call-overload]
    graph.add_node(REFUSE_AND_TICKET, _traced(REFUSE_AND_TICKET, _refuse_and_ticket))  # type: ignore[call-overload]
    graph.add_node(ASK_CLARIFYING, _traced(ASK_CLARIFYING, _ask_clarifying))  # type: ignore[call-overload]
    graph.add_node(ASK_DISAMBIGUATION, _traced(ASK_DISAMBIGUATION, _ask_disambiguation))  # type: ignore[call-overload]
    graph.add_node(ASK_CONFIRMATION, _traced(ASK_CONFIRMATION, _ask_confirmation))  # type: ignore[call-overload]
    graph.add_node(FAIL_TURN, _traced(FAIL_TURN, _fail_turn))  # type: ignore[call-overload]

    graph.add_edge(START, LOAD_SESSION)
    graph.add_conditional_edges(
        LOAD_SESSION,
        _after_session,
        {
            EXECUTE_TOOLS: EXECUTE_TOOLS,
            ASK_CLARIFYING: ASK_CLARIFYING,
            EXTRACT_INTENT: EXTRACT_INTENT,
        },
    )
    graph.add_conditional_edges(
        EXTRACT_INTENT,
        _after_intent,
        {
            RESOLVE_ENTITIES: RESOLVE_ENTITIES,
            ASK_CLARIFYING: ASK_CLARIFYING,
            FAIL_TURN: FAIL_TURN,
        },
    )
    graph.add_conditional_edges(
        RESOLVE_ENTITIES,
        _after_entities,
        {
            ASK_CLARIFYING: ASK_CLARIFYING,
            ASK_DISAMBIGUATION: ASK_DISAMBIGUATION,
            RETRIEVE_POLICY: RETRIEVE_POLICY,
        },
    )
    graph.add_conditional_edges(
        RETRIEVE_POLICY,
        _after_retrieval,
        {
            REFUSE_AND_TICKET: REFUSE_AND_TICKET,
            FAIL_TURN: FAIL_TURN,
            PLAN_TOOLS: PLAN_TOOLS,
        },
    )
    graph.add_conditional_edges(
        PLAN_TOOLS,
        _after_planning,
        {
            REFUSE_AND_TICKET: REFUSE_AND_TICKET,
            FAIL_TURN: FAIL_TURN,
            COMPOSE_REPLY: COMPOSE_REPLY,
            VALIDATE_TOOL_CALLS: VALIDATE_TOOL_CALLS,
        },
    )
    graph.add_conditional_edges(
        VALIDATE_TOOL_CALLS,
        _after_validation,
        {
            REFUSE_AND_TICKET: REFUSE_AND_TICKET,
            COMPOSE_REPLY: COMPOSE_REPLY,
            ASK_CONFIRMATION: ASK_CONFIRMATION,
            EXECUTE_TOOLS: EXECUTE_TOOLS,
        },
    )
    graph.add_conditional_edges(
        EXECUTE_TOOLS,
        _after_execution,
        {
            FAIL_TURN: FAIL_TURN,
            AWAIT_APPROVAL: AWAIT_APPROVAL,
            PLAN_TOOLS: PLAN_TOOLS,
        },
    )
    graph.add_edge(AWAIT_APPROVAL, COMPOSE_REPLY)
    graph.add_edge(COMPOSE_REPLY, END)
    graph.add_edge(REFUSE_AND_TICKET, END)
    graph.add_edge(ASK_CLARIFYING, END)
    graph.add_edge(ASK_DISAMBIGUATION, END)
    graph.add_edge(ASK_CONFIRMATION, END)
    graph.add_edge(FAIL_TURN, END)
    return graph.compile(checkpointer=checkpointer)


def build_graph(
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> CompiledStateGraph[TurnState, None, TurnState, TurnState]:
    """The compiled graph, built once per checkpointer and reused. A graph
    never writes to a saver other than the one compiled in."""
    global _NO_SAVER_GRAPH
    if checkpointer is None:
        if _NO_SAVER_GRAPH is None:
            _NO_SAVER_GRAPH = _compile_graph(None)
        return _NO_SAVER_GRAPH
    cached = _GRAPHS_BY_SAVER.get(checkpointer)
    if cached is None:
        cached = _compile_graph(checkpointer)
        _GRAPHS_BY_SAVER[checkpointer] = cached
    return cached


async def run_turn(
    *,
    deps: TurnDeps,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    tenant_slug: str,
    message: str,
    checkpointer: Saver | None = None,
) -> TurnState:
    """One turn, from a verified identity to the state a reply is built from.

    The size and session id checks run before any write or MCP connection. The
    tool session always closes, even when a node raises. With a checkpointer the
    turn is saved under the session id, so a pause outlives this process. The
    pending reply is stored here, not in the node, which LangGraph reruns on
    resume. Identity comes from the route's token, never the message.
    """
    check_message_content(message)
    thread = thread_config(str(session_id))

    state = TurnState(
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        tenant_slug=tenant_slug,
        user_message=message,
        started_at=deps.now(),
    )
    configurable: dict[str, Any] = {_DEPS_KEY: deps, **thread["configurable"]}
    with _turn_telemetry():
        async with deps.tools:
            result = await build_graph(checkpointer).ainvoke(
                state,
                config={
                    "configurable": configurable,
                    "recursion_limit": GRAPH_RECURSION_LIMIT,
                },
            )
        if _INTERRUPT_KEY in result:
            paused = _paused_state(result)
            await append_message(
                deps.db,
                session_id=paused.session_id,
                role=MessageRole.ASSISTANT,
                content=pending_reply(paused),
            )
            # A pause ends this turn; the resume counts as its own.
            record_turn_state(paused)
            set_trace_attributes(trace_attributes(paused))
            return paused
        final = TurnState.model_validate(result)
        record_turn_state(final)
        set_trace_attributes(trace_attributes(final))
        return final


def _step_data(node: str, update: Mapping[str, Any], seen: int) -> tuple[dict[str, Any], int]:
    """One `step` event with this round's tool results only, and the new
    running count. No arguments, payloads or entities are sent."""
    results = update.get("tool_results") or ()
    tools = [
        {
            "tool": str(result["tool"]),
            "ok": bool(result["ok"]),
            "error_code": None
            if result["error_code"] is None
            else ErrorCode(result["error_code"]).value,
        }
        for result in list(results)[seen:]
    ]
    data = {"node": node, "round": int(update.get("round_number") or 0), "tools": tools}
    return data, len(list(results))


def _reply_data(state: TurnState) -> dict[str, Any]:
    """The final `reply` event, with the same keys `/chat` returns. Built here
    because the route imports this module."""
    card = decision_card(state)
    return {
        "reply": state.reply,
        "outcome": state.outcome.value,
        "citations": [citation.model_dump(mode="json") for citation in state.citations],
        "request_id": None if state.awaiting_request_id is None else str(state.awaiting_request_id),
        "decision": None if card is None else card.model_dump(mode="json"),
    }


async def stream_turn(
    *,
    deps: TurnDeps,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    tenant_slug: str,
    message: str,
    checkpointer: Saver | None = None,
) -> AsyncIterator[TurnEvent]:
    """The same turn as `run_turn`, streamed one event per node.

    Tokens stream only after the reply passed its post-check. On a pause, the
    state is the update before the pausing node, since `interrupt()` drops its
    own. A `ToolError` is caught after the tool session closes, so it cannot leak.
    """
    check_message_content(message)
    thread = thread_config(str(session_id))

    state = TurnState(
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        tenant_slug=tenant_slug,
        user_message=message,
        started_at=deps.now(),
    )
    configurable: dict[str, Any] = {_DEPS_KEY: deps, **thread["configurable"]}

    latest: Mapping[str, Any] | None = None
    paused = False
    seen = 0
    with _turn_telemetry():
        try:
            async with deps.tools:
                updates = build_graph(checkpointer).astream(
                    state,
                    config={
                        "configurable": configurable,
                        "recursion_limit": GRAPH_RECURSION_LIMIT,
                    },
                    stream_mode="updates",
                )
                async for update in updates:
                    for node, value in update.items():
                        if node == _INTERRUPT_KEY:
                            paused = True
                            continue
                        latest = value
                        data, seen = _step_data(node, value, seen)
                        yield StepEvent(
                            node=str(data["node"]),
                            round=int(data["round"]),
                            tools=list(data["tools"]),
                        )
        except ToolError as exc:
            # No state, so count the turn from the code alone.
            record_turn_code(exc.code)
            yield ErrorEvent(code=exc.code)
            return

        if latest is None:
            # No update at all, so there is no reply to stream.
            record_turn_code(ErrorCode.INTERNAL)
            yield ErrorEvent(code=ErrorCode.INTERNAL)
            return

        if paused:
            final = _paused_state(latest)
            await append_message(
                deps.db,
                session_id=final.session_id,
                role=MessageRole.ASSISTANT,
                content=pending_reply(final),
            )
        else:
            final = TurnState.model_validate(latest)

        # Counted once per stream, never per frame.
        record_turn_state(final)
        set_trace_attributes(trace_attributes(final))

        if final.outcome is TurnOutcome.FAILED:
            yield ErrorEvent(code=final.error_code or ErrorCode.INTERNAL)
            return

        yield ReplyEvent(reply=final.reply, reply_data=_reply_data(final))


async def resume_graph(
    *,
    deps: TurnDeps,
    thread_id: str,
    decision: ToolResult,
    checkpointer: Saver,
) -> TurnState:
    """Continue a paused run with a recorded decision.

    No state is passed: the checkpoint is the state, and the `Command` carries
    only what `interrupt()` returns. A fresh state would start a second run.
    """
    thread = thread_config(thread_id)
    configurable: dict[str, Any] = {_DEPS_KEY: deps, **thread["configurable"]}
    with _turn_telemetry():
        async with deps.tools:
            result = await build_graph(checkpointer).ainvoke(
                Command(resume=decision.model_dump(mode="json")),
                config={
                    "configurable": configurable,
                    "recursion_limit": GRAPH_RECURSION_LIMIT,
                },
            )
        final = TurnState.model_validate(
            {key: value for key, value in result.items() if not key.startswith("__")}
        )
        # A resume is its own turn, so it gets its own trace.
        set_trace_attributes(trace_attributes(final))
        return final
