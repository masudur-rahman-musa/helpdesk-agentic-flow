"""The compiled turn graph and the one function a route calls.

the design's boundary rule, made structural: LangGraph appears in this file
and nowhere else in the package. The nodes are plain async functions over a
frozen state model, the two gates are pure predicates in `gates.py`, and every
routing function below reads ONE field that a node already wrote — none of
them computes anything. That is why `test_agent_gates.py` and
`test_agent_matching.py` can test every decision this plan makes with no graph
at all, and why both of those files assert statically that no framework import
has crept in.

Compiled with the checkpointer it is GIVEN, and with none when it is given
none. The saver is built once per process in `agent_api.checkpointer`
and handed to `run_turn`; nothing here constructs one, so this module still
knows nothing about psycopg and every node and turn test that never asked for
persistence keeps running against a graph that has none. Pinned both ways by
`test_agent_turn.py::test_run_turn_compiles_with_the_checkpointer_it_is_given`.

`run_turn` runs both pre-checks BEFORE the graph is invoked and before the MCP
session is opened, so an oversized message leaves no session row, no message
row, no model call and no handshake behind
(a project rule, pinned by
`test_agent_turn.py::test_a_message_one_character_over_the_cap_writes_nothing_at_all`).
Inside a node the same check would fire after `load_session` had already
written two rows.
"""

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
"""The checkpointer as the rest of the package names it.

An alias, so `resume.py` and `replay.py` can be typed against a saver without
importing LangGraph — which is the whole of the design's boundary rule, and
what `test_agent_approval_resume.py::test_resume_imports_no_framework` parses
this package for. Nothing about the alias is a second abstraction: it IS
`BaseCheckpointSaver`, named where the framework already lives.
"""

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
FAIL_TURN = "fail_turn"

_CachedGraph = CompiledStateGraph[TurnState, None, TurnState, TurnState]

_NO_SAVER_GRAPH: _CachedGraph | None = None
"""The compiled graph for the no-checkpointer case, cached at first call."""

_GRAPHS_BY_SAVER: weakref.WeakKeyDictionary[Saver, _CachedGraph] = weakref.WeakKeyDictionary()
"""Compiled graphs keyed by their checkpointer.

A weak-key dict so the graph is dropped when the saver goes away, rather than
keeping both alive after the process that created the saver is done with it.
Savers live for the lifetime of the process that built them (an earlier change decision
D), so in practice nothing is ever GC'd under normal operation — but the weak
reference removes a correctness trap for test code that creates short-lived savers.
"""

GRAPH_RECURSION_LIMIT = 50
"""The graph's own backstop, passed on every invocation (17d decision G).

the design asks for a hard recursion limit behind the three budgets, and
LangGraph's default 25 is not one: six executed calls is up to seven planning
rounds, which with the four leading nodes and the composition is about
twenty-five node visits — so the default would abort turns every budget in
this plan permits, on a library value nobody chose. Fifty leaves the budgets as
the thing that actually stops the loop and the limit as the thing that stops a
loop the budgets somehow did not.

a project rule, pinned from both sides by
`test_agent_turn.py::test_the_recursion_limit_is_exactly_fifty_and_is_what_run_turn_passes`
(the value, and that the invocation carries it) and by
`::test_a_turn_driven_to_the_call_budget_composes_and_never_trips_the_limit`.
"""

_DEPS_KEY = "deps"
"""Where `TurnDeps` rides on the invocation config.

Deps travel with the CALL, not with the state: the state is checkpointed as of
an earlier change, and a database session or an HTTP client is not something to write to
Postgres — nor something a resume days later could reconstitute. It rides
beside the thread id in the same `configurable` map, which is how one config
carries both the per-call things and the one durable one.
"""


_INTERRUPT_KEY = "__interrupt__"
"""What LangGraph puts on an invocation's answer when a node paused it.

A paused run does NOT raise and does not come back as a finished state: the
value is the state as of the last committed checkpoint, with this key added.
A `run_turn` that ignored it would answer a waiting user as though the turn
were done, which is the plan's first risk and why
`test_agent_approval_interrupt.py::test_a_paused_turn_returns_await_approval_and_the_request_id`
exists.
"""


def _paused_state(result: Mapping[str, Any]) -> TurnState:
    """The interrupted invocation's answer as the turn state it stands for.

    The framework's own keys are dropped rather than filtered by name — every
    one of them is dunder-shaped, and `TurnState` would refuse them anyway —
    and the reply is rendered here because `interrupt()` discards the update of
    the node that raised it, so nothing that node computed comes back with the
    state. Since an earlier change this rendering is the ONLY one: the row is stored
    from it, by `run_turn`, rather than by the node (decision C).
    """
    state = TurnState.model_validate(
        {key: value for key, value in result.items() if not key.startswith("__")}
    )
    return state.model_copy(update={"reply": pending_reply(state)})


def _deps(config: RunnableConfig) -> TurnDeps:
    """Lift `TurnDeps` back off the invocation config.

    A cast because `RunnableConfig["configurable"]` is `dict[str, Any]` by
    definition — LangGraph carries arbitrary per-call values there — so this is
    the one place the type is asserted rather than checked.
    """
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


async def _fail_turn(state: TurnState, config: RunnableConfig) -> dict[str, Any]:
    return (await fail_turn(state, _deps(config))).model_dump()


_NODE_MODEL_ALIASES: dict[str, ModelAlias] = {
    EXTRACT_INTENT: ModelAlias.AGENT_DEFAULT,
    PLAN_TOOLS: ModelAlias.AGENT_STRONG,
    COMPOSE_REPLY: ModelAlias.AGENT_DEFAULT,
}
"""The three nodes that call the model, and which alias each calls (the design).

Here rather than inside the nodes because decision D keeps telemetry out of
them: a node states its alias at the call site, and this is the same fact read
where the spans are opened. The two must agree, which is what
`test_agent_tracing.py::test_the_node_model_alias_table_is_exactly_the_three_model_calling_nodes`
and `::test_the_model_and_prompt_attributes_ride_only_the_nodes_that_use_them`
assert from the constant and from a real turn's spans respectively.
"""

_NODE_PROMPTS: dict[str, str] = {
    EXTRACT_INTENT: EXTRACTION_PROMPT,
    PLAN_TOOLS: PLANNING_PROMPT,
    COMPOSE_REPLY: COMPOSITION_PROMPT,
}
"""The three nodes that load a prompt, and which prompt each loads.

The span carries the DIGEST `prompts.py` computes over the file's bytes, never
a hand-written version string: `prompt_version` is what ties a bad turn back
to the exact text that was sent (the design), and a label somebody typed
answers nothing. Pinned by
`test_agent_tracing.py::test_the_node_prompt_table_is_exactly_the_three_prompt_loading_nodes`.
"""


def _node_attributes(node: str) -> dict[str, Any]:
    """What a node's span carries beyond its name, or an empty map.

    Computed only when a client is installed: `load_prompt` reads a file at
    call time (it is deliberately not cached, so an operator editing a prompt
    is not serving a stale digest), and an unconfigured deployment must pay
    nothing at all for a facility it is not using.
    """
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
    """The node, wrapped in one span named for it (decision D).

    Applied where the nodes are REGISTERED and nowhere else, which is what
    makes thirteen nodes instrumented by one line each and a fourteenth
    instrumented by construction. `wraps` is not decoration: LangGraph reads
    the callable's signature to decide whether to pass the config, and an
    unwrapped closure would be called with the state alone.

    Nothing about the node's answer passes through here — no state, no update,
    no exception (`span` re-raises the body's). A node that raises still
    raises, and its span is closed with the failure.
    """

    @wraps(node)
    async def run(state: TurnState, config: RunnableConfig) -> dict[str, Any]:
        with span(name, attributes=_node_attributes(name)):
            return await node(state, config)

    return run


@contextmanager
def _turn_telemetry() -> Any:
    """Open one turn's trace and bind its id onto every line of that turn.

    The bind REBINDS all four fields rather than the trace id alone:
    `bind_context` replaces rather than merges, so a bind naming
    only the trace would silently unbind the tenant and the session the
    request's middleware put there, for the whole turn. The other three are
    read back off the context that is already bound, which is why this reads
    `current_context()` before it binds. Pinned by
    `test_logging_wiring.py::test_a_record_logged_inside_a_traced_turn_carries_that_turns_trace_id`.

    Used by all three turn drivers, so `run_turn`, `stream_turn` and
    `resume_graph` cannot disagree about what a turn's trace is called or
    about what a line inside one carries — the same reasoning an earlier change
    decision E applied to the counters.
    """
    with turn_trace():
        context = current_context()
        with bind_context(
            tenant_id=context.tenant_id,
            session_id=context.session_id,
            trace_id=current_trace_id(),
            principal_kind=context.principal_kind,
        ):
            yield


def _after_intent(state: TurnState) -> str:
    """Read `outcome`. Nothing else, and no computation (the design)."""
    if state.outcome is TurnOutcome.FAILED:
        return FAIL_TURN
    if state.outcome is TurnOutcome.CLARIFY:
        return ASK_CLARIFYING
    return RESOLVE_ENTITIES


def _after_entities(state: TurnState) -> str:
    """Read `outcome`. Nothing else, and no computation (the design)."""
    if state.outcome is TurnOutcome.CLARIFY:
        return ASK_CLARIFYING
    if state.outcome is TurnOutcome.DISAMBIGUATE:
        return ASK_DISAMBIGUATION
    return RETRIEVE_POLICY


def _after_retrieval(state: TurnState) -> str:
    """Read `outcome`. Nothing else, and no computation (the design).

    `CONTINUE` now reaches `plan_tools`: an earlier change took the handover this edge
    was left open for, so a successful retrieval carries its citations into
    planning rather than ending the turn.
    """
    if state.outcome is TurnOutcome.FAILED:
        return FAIL_TURN
    if state.outcome is TurnOutcome.REFUSE:
        return REFUSE_AND_TICKET
    return PLAN_TOOLS


def _after_planning(state: TurnState) -> str:
    """Read `outcome`. Nothing else, and no computation (the design).

    `REFUSE` here is loop control, not a policy refusal: the turn has spent a
    budget the design fixes and had nothing to show for it, so it offers a
    person rather than planning again. `refuse_and_ticket` is deliberately the
    same ending — from the user's side both are "I would rather not guess at
    this, shall I get somebody".

    `DONE` is the OTHER half of the same budget (17d decision C): a turn that
    already called something composes over what happened, because the refusal
    template says no current policy covers the question and that is a false
    statement to somebody whose request was just filed. Which of the two the
    node wrote was decided by `gates.exhausted_outcome`; this function only
    reads it.
    """
    if state.outcome is TurnOutcome.FAILED:
        return FAIL_TURN
    if state.outcome is TurnOutcome.REFUSE:
        return REFUSE_AND_TICKET
    if state.outcome is TurnOutcome.DONE:
        return COMPOSE_REPLY
    return VALIDATE_TOOL_CALLS


def _after_validation(state: TurnState) -> str:
    """Read `outcome`. Nothing else, and no computation (the design).

    Three branches now that 17d has taken the handover this edge was left open
    for: a refused round offers a person, an accepted round with calls executes
    them, and the accepted EMPTY round — a planner with nothing left to do —
    goes straight to the composition. `gates.execution_outcome` made that last
    distinction; nothing is computed here.
    """
    if state.outcome is TurnOutcome.REFUSE:
        return REFUSE_AND_TICKET
    if state.outcome is TurnOutcome.DONE:
        return COMPOSE_REPLY
    return EXECUTE_TOOLS


def _after_execution(state: TurnState) -> str:
    """Read `outcome`. Nothing else, and no computation (the design).

    The cycle: an executed round goes BACK to `plan_tools`, so its results feed
    the next round (the design steps 6 and 7) and `classify_access_request`
    then `create_access_request` is two rounds rather than one guess. The loop
    ends where the planner proposes nothing, or where a budget is spent, or —
    only if both of those somehow failed — at `GRAPH_RECURSION_LIMIT`.

    `AWAIT_APPROVAL` is an earlier change's branch: the round filed a request that went
    to an approval chain, so the turn pauses instead of planning again. Which
    of the three this is was decided by `gates.approval_outcome` and written by
    the node; this function reads `outcome` and nothing else, exactly as
    criterion 3 requires.

    `FAILED` is the one tool error worth ending on: `execute_tools` writes it
    for an `INTERNAL` alone (17d decision B) and for an approval decision that
    named no usable request (18a criterion 7), and every other code is recorded
    as a result and composed over.
    """
    if state.outcome is TurnOutcome.FAILED:
        return FAIL_TURN
    if state.outcome is TurnOutcome.AWAIT_APPROVAL:
        return AWAIT_APPROVAL
    return PLAN_TOOLS


def _compile_graph(
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> CompiledStateGraph[TurnState, None, TurnState, TurnState]:
    """Build a new compiled graph — not cached. Called only by `build_graph`.

    The cycle is `plan_tools` → `validate_tool_calls` → `execute_tools` →
    `plan_tools`, which is what the design's steps 6 and 7 describe. Three
    independent things end it, each tested alone: the round that proposes
    nothing (`DONE`, straight to `compose_reply`), any of the three budgets
    (17c's predicate, read through `gates.exhausted_outcome`), and
    `GRAPH_RECURSION_LIMIT` as the backstop behind both. An earlier change adds a fourth
    way out of the cycle that is not an ending at all: `await_approval`, which
    pauses the run mid-flight and leaves it in the checkpointer to be resumed.

    That node's edge runs to `compose_reply`, and ONLY a resume ever traverses
    it: on the first pass `interrupt()` does not return, so
    the pause ends inside the node. On a resume the node has the approver's
    recorded decision appended to `tool_results`, and the composition writes it
    down. It does not re-enter `execute_tools`, which is what the design said
    literally and what a design note amends — `approve_request` runs `execute_grant`
    in the same transaction as the last filled seat, so the grant already
    exists and re-entering execution would grant twice.

    The checkpointer is a parameter and never a default: a saver built here
    would be a second persistence story for every test and every process that
    asked for none, and a `MemorySaver` fallback would silently hold each
    paused turn in one process's memory, which is precisely the failure this
    plan exists to remove.
    """
    # An earlier change decision C: every `add_node` carries a named ignore. The nodes are
    # `async (TurnState, RunnableConfig) -> dict[str, Any]`, which LangGraph runs
    # correctly but no `add_node` overload describes; restructuring the builder to
    # satisfy the stubs would change working code to please a type, and these are
    # the 13 sites rule 46 names.
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
    graph.add_node(FAIL_TURN, _traced(FAIL_TURN, _fail_turn))  # type: ignore[call-overload]

    graph.add_edge(START, LOAD_SESSION)
    graph.add_edge(LOAD_SESSION, EXTRACT_INTENT)
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
    graph.add_edge(FAIL_TURN, END)
    return graph.compile(checkpointer=checkpointer)


def build_graph(
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> CompiledStateGraph[TurnState, None, TurnState, TurnState]:
    """The compiled graph, cached by its checkpointer.

    One compiled graph per process per checkpointer: `run_turn`, `stream_turn`,
    `resume_graph` and `replay` all call this and reuse the same object rather
    than each compiling their own. The checkpointer is still compiled IN, not
    switched at runtime — a graph bound to saver A will not write to saver B.

    The no-saver case gets one slot (``_NO_SAVER_GRAPH``), not a slot per
    process or per test: tests that pass ``None`` share the same compiled graph,
    which is correct because the no-saver graph carries no state between calls.

    Real savers live in ``_GRAPHS_BY_SAVER``, a ``WeakKeyDictionary`` so the
    graph is dropped when the saver is GC'd instead of keeping both alive
    forever. In production the saver lives for the lifetime of the process so
    nothing is ever dropped; the weak reference is a correctness guard for
    test code that creates short-lived savers.

    Pinned by
    ``test_agent_graph_built_once.py::test_one_checkpointer_compiles_the_graph_once``
    and ``::test_a_different_checkpointer_gets_its_own_graph``.
    """
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
    """One turn, from a verified identity to the state a reply is rendered from.

    Both pre-checks run first and neither writes anything: the message against
    `MAX_MESSAGE_CHARS`, and the session id through `check_slug` — which is now
    `checkpointer.thread_config`'s own check, because the session id IS the
    checkpoint thread id (decision A1), so the value bounded before the
    gateway's `X-Session` header and the value bounded before a checkpoint
    write are one check in one place. `session_id` is typed as a UUID, so that
    second gate can only ever fail closed — it is here so a change to
    `helpdesk.rag.naming` that stopped admitting a canonical uuid string is
    caught by a test rather than by every turn refusing at once
    (`test_agent_turn.py::test_every_canonical_session_id_passes_the_session_id_pre_check`).

    Both pre-checks also run BEFORE the MCP session is opened, so an oversized
    message pays no network handshake either
    (`::test_the_pre_checks_still_run_before_the_caller_is_opened`).

    The tool caller is entered here and nowhere else:
    one session for the whole turn, closed by the `async with` on the raising
    path as well as the returning one, so a node that explodes cannot leak the
    turn's connection pool
    (`::test_the_caller_is_closed_when_a_node_raises`).

    `started_at` is stamped here from the INJECTED clock, once per turn: it is
    what `TURN_DEADLINE_SECONDS` is measured from, so a node reading a wall
    clock of its own would restart the deadline on every round. Pinned by
    `::test_run_turn_stamps_started_at_from_the_injected_clock`.

    `GRAPH_RECURSION_LIMIT` rides on the same config (17d decision G). It is
    the backstop behind the three budgets, not a budget of its own: LangGraph's
    default 25 would abort turns the budgets permit, so it is passed explicitly
    rather than left to the library
    (`::test_the_recursion_limit_is_exactly_fifty_and_is_what_run_turn_passes`).

    The CHECKPOINTER is optional and is whatever the caller built (an earlier change
    decision D): one pool and one saver per process, not per turn. Given one,
    the graph compiles with it and the invocation carries the thread config, so
    every step of the turn is written to Postgres under the session id and a
    paused turn survives this process
    (`test_agent_approval_interrupt.py::test_the_paused_state_is_readable_from_a_second_checkpointer`).
    Given none, the graph runs exactly as it did before this plan.

    A paused invocation comes back with `__interrupt__` rather than a finished
    state, and `_paused_state` maps it: the returned `TurnState` carries
    `AWAIT_APPROVAL`, the request id the pause is waiting on and the templated
    pending reply. That reply is STORED here, once, and
    not by the node that paused: LangGraph replays the interrupting node from
    its start on a resume, so a row written inside it would be written again
    every time an approver decides
    (`test_agent_approval_resume.py::test_a_resumed_turn_leaves_exactly_one_pending_message`).

    Identity is passed in by the route that read the token and is never taken
    from the message or from the extracted entities.
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
            # A pause is a terminal of THIS turn, not a turn that never
            # happened: the route answers it 200 and the run stops here until
            # an approver decides, so it is counted here and the resume is
            # counted again as its own turn (an earlier change,
            # `test_a_resumed_turn_counts_exactly_one`). The trace closes here
            # for the same reason, and the resume opens its own.
            record_turn_state(paused)
            set_trace_attributes(trace_attributes(paused))
            return paused
        final = TurnState.model_validate(result)
        record_turn_state(final)
        set_trace_attributes(trace_attributes(final))
        return final


def _step_data(node: str, update: Mapping[str, Any], seen: int) -> tuple[dict[str, Any], int]:
    """One `step` event's payload, and how many tool results have been seen.

    Decision C, as a FIXED key set — `node`, `round` and `tools` — so a client
    parses one shape whatever node it came from. `tools` is the results of THIS
    round alone, sliced off the state's accumulated `tool_results` by the
    running count, which is why the count travels with the payload: a step
    reporting the whole accumulation would show a UI the second round making
    three calls when it made one.

    `tools` is a LIST rather than the plan's singular tool name because
    `MAX_CALLS_PER_ROUND` is three: one round can execute three calls, and a
    single `tool` key could only name one of them (recorded in the plan's
    Deviations).

    Nothing else crosses. No arguments, no payload, no resolved entities and no
    history — the same data the design keeps out of telemetry — asserted as the
    exact key set AND as a substring sweep by
    `test_turn_stream.py::test_a_step_carries_no_arguments_no_payload_and_no_entities`,
    which plants all four in the turn's state first so their absence means
    something.
    """
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
    """The terminal `reply` event: the same body `/chat` answers this turn with.

    Deliberately the same five keys as `chat_routes.ChatResponse` (19a decision
    E1), paused turn included, so a streaming client and a non-streaming one
    read one shape and a turn can be replayed through either route. It is built
    here rather than imported from `chat_routes` because the route imports this
    module; the two are held together by
    `test_chat_stream.py::test_both_routes_store_the_same_rows_for_one_message`,
    which runs one message through both and compares what was stored — and
    compares this event's `decision` against the other route's, which is what
    catches the fifth key landing on one shape and not the other.

    `decision` is the card as JSON, not as the model: this dict is encoded
    into an SSE frame, and a uuid or a datetime is not something that survives
    the encoder.
    """
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
    """The same turn as `run_turn`, watched: encoded SSE frames as it happens.

    A second VIEW of one turn and never a second turn (§1). It takes the same
    arguments, runs the same two pre-checks in the same order and before the
    MCP session is opened, enters the same tool caller, and invokes the same
    graph with the same config and the same recursion limit. The one difference
    is `astream(stream_mode="updates")` in place of `ainvoke`: one update per
    node, which IS the step vocabulary a UI needs, so no node writes an event
    and no node learns that a client is watching (decision A1). Pinned against
    `/chat` by
    `test_chat_stream.py::test_both_routes_store_the_same_rows_for_one_message`.

    **What streams as tokens is the reply the post-check has already passed**
    (decision B, a design note). Composition happens inside `compose_reply`, so by
    the time a `token` event exists the regeneration of the design step 8 has
    already happened or not happened; a contradicting first attempt reaches no
    frame at all
    (`test_turn_stream.py::test_no_token_is_emitted_before_the_composition_returns`).

    Each update's value is the node's whole state dump, so the LAST one is the
    finished state — there is no second invocation to read it from. A paused
    run is the exception LangGraph forces: `interrupt()` discards the
    interrupting node's update, so the state as of the pause is the update
    BEFORE it, which is `execute_tools`' — the node that wrote both
    `AWAIT_APPROVAL` and `awaiting_request_id` for exactly this reason. The
    pending reply is rendered and stored here, once, precisely as `run_turn`
    stores it, which is why a paused turn streamed leaves one assistant row and
    not two
    (`::test_a_paused_turn_stores_exactly_one_assistant_row`).

    A `ToolError` raised outside every node — the MCP handshake, or the
    pre-checks on a caller that skipped the route — is caught OUTSIDE the
    `async with`, so the turn's tool session is closed before the `error` frame
    is written and a failed stream cannot leak it
    (`::test_the_turns_session_is_closed_when_the_stream_ends_on_a_failure`).

    Identity is passed in by the route that read the token and is never taken
    from the message or from the extracted entities.
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
            # No state was ever produced, so the turn is counted from the code
            # alone: it happened, and a stream that dies in the handshake is
            # exactly the thing the turn and refusal rates exist to show. There
            # is no state to describe the trace with either, so it closes
            # carrying only the spans that ran.
            record_turn_code(exc.code)
            yield ErrorEvent(code=exc.code)
            return

        if latest is None:
            # A graph that yielded no update at all: nothing composed anything,
            # so there is no reply to stream and no outcome to report.
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

        # Counted once per stream, at whichever of the two remaining terminals
        # it reaches, and never per frame: a token event is part of one turn,
        # not a turn of its own (§1's "one `/chat/stream` turn adds exactly
        # one"). The trace's attributes are written at the same terminals, from
        # the same state, so the two records of one turn cannot disagree.
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
    """Continue a paused run on the same thread, carrying a recorded decision.

    The sibling of `run_turn`, and the reason `Command` — like every other
    LangGraph name — appears in this file and in no other (the design's
    boundary rule): `agent_api.resume` decides WHETHER a run may be resumed and
    with what, and this function is the only thing that knows how a resume is
    spelled.

    No state is passed. The thread's own checkpoint IS the state, and the
    `Command` carries only the value `interrupt()` returns — as plain JSON,
    because it crosses the checkpointer's serde on its way into the replayed
    node. A resume that handed the graph a fresh `TurnState` would start a
    second run over the same thread, re-extract, re-plan and file the request
    again; that is what
    `test_agent_approval_resume.py::test_a_recorded_approval_resumes_the_run_from_a_fresh_saver`
    scripts an empty tool caller to catch.

    The saver is a parameter for the same reason `run_turn`'s is: the process
    that resumes a run is not the one that paused it, so the pool, the saver and
    the deps are all whatever the resuming process built.

    The recursion limit rides on the invocation exactly as it does on a fresh
    turn: a resumed run re-enters the same graph, and the backstop behind the
    budgets is not something a resume opts out of.
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
        # A resume is its own turn, so it is its own trace, closed here with
        # the decision the resumed run reached. `agent_api.resume`
        # counts it separately for the same reason.
        set_trace_attributes(trace_attributes(final))
        return final
