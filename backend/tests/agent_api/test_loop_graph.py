"""The agent loop runs inside a LangGraph graph with four nodes.

Expected implementation surface:

``helpdesk.agent_api.agent.graph``
    ``LOOP_NODES`` (``held_answer``, ``agent``, ``tools``, ``check_reply``) and
    ``compiled_graph(checkpointer=None)``, built once per saver object.
``helpdesk.agent_api.agent.turn_deps.TurnDeps``
    gains ``graph`` (``None`` means the cached graph with no saver).
``helpdesk.agent_api.agent.loop``
    ``run_loop_turn`` and ``stream_loop_turn`` gain ``turn_id=``; the saver
    thread of a turn is ``f"{session_id}:{turn_id}"``.

The fakes are the ones ``test_agent_loop.py`` uses. Only one test reaches a real
Postgres (marker ``db``); the rest use an in-memory saver. The last tests read
the public page and the two files that link it.
"""

import asyncio
import dataclasses
import json
import re
import uuid
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.agent_api import cli
from helpdesk.agent_api.agent.checkpointer import (
    build_checkpointer,
    build_pool,
    psycopg_dsn,
    setup_checkpointer,
    thread_config,
)
from helpdesk.agent_api.agent.graph import LOOP_NODES, compiled_graph
from helpdesk.agent_api.agent.loop import run_loop_turn
from helpdesk.agent_api.agent.loop_tools import offered_tools
from helpdesk.agent_api.agent.prompts import load_prompt
from helpdesk.agent_api.agent.state import ToolResult, TurnOutcome, TurnState
from helpdesk.agent_api.agent.turn_deps import TurnDeps
from helpdesk.agent_api.services.model_gateway.client import ModelClient
from helpdesk.shared.core.errors import ErrorCode
from tests.agent_api.test_agent_loop import (
    API_KEYS,
    GATEWAY_URL,
    MESSAGE,
    SESSION_ID,
    TENANT_ID,
    TENANT_SLUG,
    USER_ID,
    Clock,
    FakeStore,
    FakeTools,
    ScriptedGateway,
    _create_arguments,
    answer_body,
    wire_call,
)
from tests.support.mcp import TempDatabaseFactory

_REPO_ROOT = Path(__file__).resolve().parents[3]
PAGE = _REPO_ROOT / "docs" / "agent-loop-and-langgraph.md"
PAGE_LINK = "docs/agent-loop-and-langgraph.md"
SCORES = _REPO_ROOT / "backend" / "evals" / "engine-scores.json"

LISTING: dict[str, Any] = {"requests": [{"request_number": "REQ-0001", "status": "rejected"}]}

# The node names of the pre-116 intent pipeline. None may come back.
OLD_PIPELINE_NODES = (
    "load_session",
    "extract_intent",
    "resolve_entities",
    "retrieve_policy",
    "plan_tools",
    "validate_tool_calls",
    "execute_tools",
    "await_approval",
    "compose_reply",
    "refuse_and_ticket",
    "ask_clarifying",
    "ask_disambiguation",
    "ask_confirmation",
    "fail_turn",
    "stop_asking",
)

# --- running one turn over the graph ----------------------------------------------


def _config(thread_id: str) -> Any:
    """The saver config written out by hand, so a reading test does not lean on
    `thread_config`."""
    return {"configurable": {"thread_id": thread_id}}


async def _turn(
    gateway: ScriptedGateway,
    tools: FakeTools,
    store: FakeStore,
    *,
    graph: Any,
    turn_id: uuid.UUID,
    message: str = MESSAGE,
) -> TurnState:
    model = ModelClient(
        base_url=GATEWAY_URL,
        api_keys=API_KEYS,
        transport=httpx.MockTransport(gateway.handler),
    )
    try:
        deps = TurnDeps(
            db=cast(AsyncSession, None),
            model=model,
            tools=tools,
            now=Clock(),
            graph=graph,
        )
        return await run_loop_turn(
            deps=deps,
            tenant_id=TENANT_ID,
            user_id=USER_ID,
            session_id=SESSION_ID,
            tenant_slug=TENANT_SLUG,
            message=message,
            store=store,
            turn_id=turn_id,
        )
    finally:
        await model.aclose()


def _lookup_then_answer() -> ScriptedGateway:
    return ScriptedGateway(
        answer_body(None, wire_call("call_a", "list_my_requests", {})),
        answer_body("You have one request, REQ-0001, and it was turned down."),
    )


# --- the shape of the graph ---------------------------------------------------------


def test_the_graph_has_exactly_the_four_loop_nodes() -> None:
    """Catches a graph that keeps any node of the old intent pipeline, drops or
    renames one of the four loop nodes, or leaves out the arrows back to `agent`
    (so a tool result or a send-back could never reach the model again).

    Reads the compiled graph itself: the node set must equal the four names
    (plus LangGraph's own start and end markers), `LOOP_NODES` must say the
    same, and both return edges must exist.
    """
    drawn = compiled_graph(None).get_graph()

    assert LOOP_NODES == ("held_answer", "agent", "tools", "check_reply")
    assert set(drawn.nodes) - {"__start__", "__end__"} == {
        "held_answer",
        "agent",
        "tools",
        "check_reply",
    }
    assert not set(drawn.nodes) & set(OLD_PIPELINE_NODES)
    edges = {(edge.source, edge.target) for edge in drawn.edges}
    assert ("tools", "agent") in edges
    assert ("check_reply", "agent") in edges


@pytest.mark.parametrize("principals", [["employee"], ["manager"]], ids=["employee", "manager"])
def test_the_agent_node_uses_the_loop_prompt(principals: list[str]) -> None:
    """Catches an `agent` node that sends another prompt (or none) as the system
    message, or offers every tool instead of the ones the person's role allows,
    so the graph would not be the same call the plain loop made.

    The turn runs through the compiled graph. The first model request must carry
    the `agent_loop` prompt text as its system message and exactly the wire form
    of `offered_tools` for these principals; the two roles must differ, so a
    fixed tool list cannot pass both rows.
    """
    gateway = ScriptedGateway(answer_body("Fine."))
    store = FakeStore(principals=principals)

    asyncio.run(
        _turn(
            gateway,
            FakeTools(),
            store,
            graph=compiled_graph(None),
            turn_id=uuid.uuid4(),
        )
    )

    first = gateway.requests[0]
    assert first["messages"][0] == {"role": "system", "content": load_prompt("agent_loop").text}
    assert first["tools"] == [t.wire() for t in offered_tools(principals)]
    assert offered_tools(["employee"]) != offered_tools(["manager"])


def test_the_graph_is_compiled_once_per_checkpointer() -> None:
    """Catches a `compiled_graph` that compiles again on every call (the cost
    the graph keeps out of each turn), or one that hands a saver-less graph to a
    caller that passed a saver (its steps would never be saved).

    Same saver object twice gives the same graph; `None` twice gives one graph;
    a second saver and `None` each get their own.
    """
    first_saver = InMemorySaver()
    second_saver = InMemorySaver()

    assert compiled_graph(None) is compiled_graph(None)
    assert compiled_graph(first_saver) is compiled_graph(first_saver)
    assert compiled_graph(first_saver) is not compiled_graph(second_saver)
    assert compiled_graph(first_saver) is not compiled_graph(None)


# --- what is saved ------------------------------------------------------------------


_FORBIDDEN = (
    AsyncSession,
    ModelClient,
    httpx.AsyncClient,
    httpx.Client,
    FakeTools,
    FakeStore,
    Clock,
    TurnDeps,
)


def _walk(value: object, seen: list[object]) -> Iterator[object]:
    """Every value inside `value`, through containers, models and dataclasses."""
    if id(value) in {id(item) for item in seen}:
        return
    seen.append(value)
    yield value
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield from _walk(key, seen)
            yield from _walk(item, seen)
    elif isinstance(value, list | tuple | set | frozenset):
        for item in value:
            yield from _walk(item, seen)
    elif isinstance(value, BaseModel):
        for name in type(value).model_fields:
            yield from _walk(getattr(value, name), seen)
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        for field in dataclasses.fields(value):
            yield from _walk(getattr(value, field.name), seen)


def test_saved_state_holds_no_session_or_client() -> None:
    """Unhappy path. Catches a graph that keeps the database session, the model
    client, the tool caller, the chat-row store or the clock in its saved state
    (a saver would then try to store a live connection, and a replay would hold
    a dead one), instead of passing them in the run context.

    A two-step turn runs on an in-memory saver. Every saved checkpoint is walked
    through lists, dicts, models and dataclasses; nothing forbidden may be in
    it. The walk must also have met the lookup's ToolResult, so an empty save
    cannot pass.
    """
    saver = InMemorySaver()
    turn_id = uuid.uuid4()
    tools = FakeTools({"list_my_requests": LISTING})

    async def scenario() -> list[Any]:
        await _turn(
            _lookup_then_answer(),
            tools,
            FakeStore(),
            graph=compiled_graph(saver),
            turn_id=turn_id,
        )
        return [item async for item in saver.alist(_config(f"{SESSION_ID}:{turn_id}"))]

    saved = asyncio.run(scenario())

    assert len(saved) >= 2
    met: list[object] = []
    for item in saved:
        for found in _walk(item.checkpoint["channel_values"], met):
            assert not isinstance(found, _FORBIDDEN), type(found).__name__
    assert any(isinstance(found, ToolResult) for found in met)


# --- changes are held, never run -----------------------------------------------------


def test_a_change_never_reaches_the_tools_node_as_a_run() -> None:
    """Catches a `tools` node that runs a proposed `create_access_request` (the
    model would then act with no yes from the person), or that runs it and also
    holds it.

    The model proposes the change. The tool server is scripted no tool, so a
    call fails the run, and the recorded calls must stay empty. The change must
    sit on the chat row and the turn must end on the confirm outcome. The turn
    goes through the compiled graph.
    """
    gateway = ScriptedGateway(
        answer_body(None, wire_call("call_c", "create_access_request", _create_arguments()))
    )
    tools = FakeTools()
    store = FakeStore()

    state = asyncio.run(
        _turn(
            gateway,
            tools,
            store,
            graph=compiled_graph(None),
            turn_id=uuid.uuid4(),
            message="Please file wiki viewer access for me.",
        )
    )

    assert tools.calls == []
    assert [call.tool for call in store.held[0].calls] == ["create_access_request"]
    assert len(store.held) == 1
    assert state.outcome is TurnOutcome.CONFIRM
    assert len(gateway.requests) == 1


@pytest.mark.db
def test_each_step_of_a_turn_is_saved(mcp_pg: TempDatabaseFactory) -> None:
    """Catches a graph run without the saver (nothing saved, so a bad turn could
    not be read back), one that saves only the last state, and one that saves
    every turn under one shared thread (turns would mix).

    A real Postgres temp database gets the checkpoint tables, then a two-step
    turn (a lookup, then an answer) runs on the Postgres saver. At least two
    checkpoints must sit under this turn's thread `<session id>:<turn id>`; a
    different turn id's thread must hold none. The database is dropped after.
    """
    url = mcp_pg.create()
    turn_id = uuid.uuid4()
    other_turn_id = uuid.uuid4()
    thread = f"{SESSION_ID}:{turn_id}"

    async def scenario() -> tuple[int, int]:
        pool = build_pool(psycopg_dsn(url))
        async with pool:
            await setup_checkpointer(pool)
            saver = build_checkpointer(pool)
            await _turn(
                _lookup_then_answer(),
                FakeTools({"list_my_requests": LISTING}),
                FakeStore(),
                graph=compiled_graph(saver),
                turn_id=turn_id,
            )
            mine = [item async for item in saver.alist(thread_config(thread))]
            others = [
                item async for item in saver.alist(thread_config(f"{SESSION_ID}:{other_turn_id}"))
            ]
            return len(mine), len(others)

    try:
        mine, others = asyncio.run(scenario())
    finally:
        mcp_pg.drop(url)

    assert mine >= 2
    assert others == 0


def test_the_turn_thread_id_is_accepted_by_thread_config() -> None:
    """Catches a `thread_config` that refuses the turn's own thread id (a
    session id, a colon and a turn id is 73 characters, and a slug check caps
    names at 64 and refuses the colon), so the saved run would fail at its first
    step in production.

    The id is built exactly as the loop builds it.
    """
    thread = f"{SESSION_ID}:{uuid.uuid4()}"

    assert thread_config(thread) == {"configurable": {"thread_id": thread}}


# --- the table command ---------------------------------------------------------------


def test_the_table_command_refuses_a_non_asyncpg_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unhappy path. Catches a table command that connects wherever a wrong
    `DATABASE_URL` points, or one that reports success for a `mysql://` URL.

    `main()` must return exactly 1, print the typed code, and never build a
    pool nor run the setup (both are replaced by functions that fail the test).
    """

    def must_not_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the command touched the database for a refused URL")

    monkeypatch.setenv("DATABASE_URL", "mysql://app:fake@db.invalid:3306/helpdesk")
    monkeypatch.setattr(cli, "build_pool", must_not_run)
    monkeypatch.setattr(cli, "_setup", must_not_run)

    code = cli.main()

    assert code == 1
    assert ErrorCode.VALIDATION.value in capsys.readouterr().err


# --- the public page -----------------------------------------------------------------


def _page() -> str:
    return PAGE.read_text()


def test_the_page_names_every_node_the_graph_has() -> None:
    """Catches a page that drifts from the code: a loop node left out of it, or
    a node of the old intent pipeline (`extract_intent`, `plan_tools`,
    `compose_reply` and the rest) still drawn as if it ran.

    Every name in `LOOP_NODES` must appear in backticks. No backticked name may
    be one of the fifteen old pipeline nodes.
    """
    text = _page()

    for node in LOOP_NODES:
        assert f"`{node}`" in text, node
    named = set(re.findall(r"`([a-z]+(?:_[a-z]+)+)`", text))
    assert named & set(OLD_PIPELINE_NODES) == set()


def test_the_page_shows_the_recorded_scores() -> None:
    """Catches a page whose scores were typed by hand and no longer match the
    recorded run, or one that leaves an engine out (the claim "backed by real
    scores" would then be empty).

    For `pipeline`, `loop` and `graph`, the numbers in `engine-scores.json` must
    appear as `<cases_passed> of <cases>` in a paragraph that names the engine.
    """
    scores: dict[str, Any] = json.loads(SCORES.read_text())
    paragraphs = [part.lower() for part in re.split(r"\n\s*\n", _page())]

    for engine in ("pipeline", "loop", "graph"):
        record = scores[engine]
        shown = f"{record['cases_passed']} of {record['cases']}"
        assert any(engine in part and shown in part for part in paragraphs), engine


def test_the_readme_and_architecture_link_the_page() -> None:
    """Catches a page nobody can reach (no link from the README or the
    architecture page), and a README that still describes the old paused turn or
    resume, which LangGraph's saved steps no longer do here.

    Both files must hold the page path. The README must hold neither "paused
    turn" nor "resume" in any case.
    """
    readme = (_REPO_ROOT / "README.md").read_text()
    architecture = (_REPO_ROOT / "docs" / "diagrams" / "architecture.md").read_text()

    assert PAGE_LINK in readme
    assert PAGE_LINK in architecture
    assert "paused turn" not in readme.lower()
    assert "resume" not in readme.lower()
