"""The compiled turn graph and the one function a route calls.

the design's boundary rule, made structural: LangGraph appears in this file
and nowhere else in the package. The nodes are plain async functions over a
frozen state model, the two gates are pure predicates in `gates.py`, and every
routing function below reads ONE field that a node already wrote — none of
them computes anything. That is why `test_agent_gates.py` and
`test_agent_matching.py` can test every decision this plan makes with no graph
at all, and why both of those files assert statically that no framework import
has crept in.

Compiled with no checkpointer (decision F). An earlier change adds the Postgres
checkpointer together with the approval interrupt that needs one; a
`MemorySaver` here would be a second persistence story, discarded a plan
later, holding every turn's state in the process meanwhile. Pinned by
`test_agent_turn.py::test_the_compiled_graph_carries_no_checkpointer`.

`run_turn` runs both pre-checks BEFORE the graph is invoked and before the MCP
session is opened, so an oversized message leaves no session row, no message
row, no model call and no handshake behind
(a project rule, pinned by
`test_agent_turn.py::test_a_message_one_character_over_the_cap_writes_nothing_at_all`).
Inside a node the same check would fire after `load_session` had already
written two rows.
"""

import uuid
from typing import Any, cast

from helpdesk.models.conversation import check_message_content
from helpdesk.rag.naming import check_slug
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agent_api.graph.deps import TurnDeps
from agent_api.graph.nodes.endings import (
    ask_clarifying,
    ask_disambiguation,
    fail_turn,
    refuse_and_ticket,
)
from agent_api.graph.nodes.extract_intent import FAILURE_REPLY, extract_intent
from agent_api.graph.nodes.load_session import load_session
from agent_api.graph.nodes.resolve_entities import resolve_entities
from agent_api.graph.nodes.retrieve_policy import retrieve_policy
from agent_api.graph.state import TurnOutcome, TurnState

__all__ = ["FAILURE_REPLY", "TurnDeps", "build_graph", "run_turn"]

LOAD_SESSION = "load_session"
EXTRACT_INTENT = "extract_intent"
RESOLVE_ENTITIES = "resolve_entities"
RETRIEVE_POLICY = "retrieve_policy"
REFUSE_AND_TICKET = "refuse_and_ticket"
ASK_CLARIFYING = "ask_clarifying"
ASK_DISAMBIGUATION = "ask_disambiguation"
FAIL_TURN = "fail_turn"

_DEPS_KEY = "deps"
"""Where `TurnDeps` rides on the invocation config.

Deps travel with the CALL, not with the state: the state is checkpointed in
an earlier change, and a database session or an HTTP client is not something to write to
Postgres. It also keeps `build_graph()` argument-free, so the graph is one
compiled object rather than one per turn.
"""


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


async def _refuse_and_ticket(state: TurnState, config: RunnableConfig) -> dict[str, Any]:
    return (await refuse_and_ticket(state, _deps(config))).model_dump()


async def _ask_clarifying(state: TurnState, config: RunnableConfig) -> dict[str, Any]:
    return (await ask_clarifying(state, _deps(config))).model_dump()


async def _ask_disambiguation(state: TurnState, config: RunnableConfig) -> dict[str, Any]:
    return (await ask_disambiguation(state, _deps(config))).model_dump()


async def _fail_turn(state: TurnState, config: RunnableConfig) -> dict[str, Any]:
    return (await fail_turn(state, _deps(config))).model_dump()


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

    `CONTINUE` reaches `END` and that is 17c's handover point: the citations
    ride on the state and no assistant message has been written, so the next
    plan hangs planning, execution and the composed reply off this edge.
    """
    if state.outcome is TurnOutcome.FAILED:
        return FAIL_TURN
    if state.outcome is TurnOutcome.REFUSE:
        return REFUSE_AND_TICKET
    return END


def build_graph() -> CompiledStateGraph:
    """The four working nodes and the four ways the turn can stop early.

    `retrieve_policy` reaching `END` on `CONTINUE` is now the handover point:
    an earlier change hangs planning, execution and the composed reply off exactly that
    edge, which is why a successful retrieval writes no assistant message here
    and carries its citations on the state instead.
    """
    graph = StateGraph(TurnState)
    graph.add_node(LOAD_SESSION, _load_session)
    graph.add_node(EXTRACT_INTENT, _extract_intent)
    graph.add_node(RESOLVE_ENTITIES, _resolve_entities)
    graph.add_node(RETRIEVE_POLICY, _retrieve_policy)
    graph.add_node(REFUSE_AND_TICKET, _refuse_and_ticket)
    graph.add_node(ASK_CLARIFYING, _ask_clarifying)
    graph.add_node(ASK_DISAMBIGUATION, _ask_disambiguation)
    graph.add_node(FAIL_TURN, _fail_turn)

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
            END: END,
        },
    )
    graph.add_edge(REFUSE_AND_TICKET, END)
    graph.add_edge(ASK_CLARIFYING, END)
    graph.add_edge(ASK_DISAMBIGUATION, END)
    graph.add_edge(FAIL_TURN, END)
    return graph.compile()


async def run_turn(
    *,
    deps: TurnDeps,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    tenant_slug: str,
    message: str,
) -> TurnState:
    """One turn, from a verified identity to the state a reply is rendered from.

    Both pre-checks run first and neither writes anything: the message against
    `MAX_MESSAGE_CHARS`, and the session id through `check_slug` before it can
    become the gateway's `X-Session` header. `session_id` is typed as a UUID,
    so that second gate can only ever fail closed — it is here so a change to
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

    Identity is passed in by the route that read the token and is never taken
    from the message or from the extracted entities.
    """
    check_message_content(message)
    check_slug(str(session_id))

    state = TurnState(
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        tenant_slug=tenant_slug,
        user_message=message,
    )
    async with deps.tools:
        result = await build_graph().ainvoke(state, config={"configurable": {_DEPS_KEY: deps}})
    return TurnState.model_validate(result)
