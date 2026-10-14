"""An earlier change §6: one trace per chat turn, one span per node, `tool:{name}`.

Written before the wiring exists. Expected implementation surface (the
implementer must provide exactly this):

``agent_api.graph.build``
    every one of the THIRTEEN registered nodes is wrapped so its execution
        opens a ``helpdesk.obs`` ``span`` named for the node — the existing
        name constants, so the span name IS the node name LangGraph reports.
    ``_NODE_MODEL_ALIASES: dict[str, ModelAlias]`` — exactly the three nodes
        that call the model; their spans carry ``model_alias``.
    ``_NODE_PROMPTS: dict[str, str]`` — exactly the three nodes that load a
        prompt; their spans carry ``prompt_version``, the sha256
        ``agent_api.prompts.load_prompt(name).sha256`` computes.
    ``run_turn``, ``stream_turn`` and ``resume_graph`` each open ONE
        ``turn_trace`` around the whole invocation, bind ``trace_id`` into the
        telemetry context for the turn, and write the trace attributes at
        every terminal through ``set_trace_attributes``.
``agent_api.metrics``
    ``def trace_attributes(state: TurnState) -> dict[str, Any]`` — exactly the
        SIX keys ``tenant_id``, ``session_id``, ``risk_class``, ``decision``,
        ``refused``, ``citations_count``.
``agent_api.mcp_client``
    ``TOOL_SPAN_PREFIX = "tool:"`` and ``class TracingToolCaller`` over an
        inner ``ToolCaller``, opening one span per ``call_tool`` and NONE for
        ``__aenter__``/``__aexit__``. No tool ARGUMENT reaches a span
        (decision G).
``agent_api.chat_routes.build_tool_caller`` returns
    ``TracingToolCaller(McpToolCaller(...))``.

Cost and isolation. The turn cases ride ``test_agent_turn.py``'s stubbed
harness — its session-scoped seeded database, its ``httpx.MockTransport``
gateway and its stub ``ToolCaller`` — so nothing here calls a model, opens a
socket or reaches Qdrant or Redis; the paused/resume case reuses
``test_agent_approval_resume.py``'s checkpointer arrangement, which is the one
case here that needs the LangGraph tables. The runner below is this file's own
only because ``test_agent_turn._run_turn`` is typed to its stub caller, and
the tool-span case must pass a ``TracingToolCaller`` wrapping it. The trace
client is always the recording fake from ``packages/helpdesk/tests`` (imported
with its directory inserted, a project rule), never a real ``Langfuse``.
"""

import asyncio
import json
import uuid
from collections.abc import Iterator, Mapping
from typing import Any

import httpx
import pytest
from langfuse import Langfuse
from pydantic import BaseModel
from test_agent_approval_interrupt import (
    ApprovalEnv,
    ScriptedToolCaller,
    StagedGateway,
    approval_env,  # noqa: F401
)
from test_agent_approval_resume import (
    COMPOSED_APPROVAL,
    _pause,
    _resume,
    _store_request,
)
from test_agent_turn import (
    API_KEYS,
    COMPOSED_REPLY,
    EMPTY_PLAN,
    GATEWAY_URL,
    NOW,
    SeededTurnTenant,
    StubToolCaller,
    TurnEnv,
    _citation,
    _page,
    _payroll_answer,
    pg,  # noqa: F401
    turn_env,  # noqa: F401
)

from helpdesk.agent_api.agent.graph import (
    _NODE_MODEL_ALIASES,
    _NODE_PROMPTS,
    ASK_CLARIFYING,
    ASK_DISAMBIGUATION,
    AWAIT_APPROVAL,
    COMPOSE_REPLY,
    EXECUTE_TOOLS,
    EXTRACT_INTENT,
    FAIL_TURN,
    LOAD_SESSION,
    PLAN_TOOLS,
    REFUSE_AND_TICKET,
    RESOLVE_ENTITIES,
    RETRIEVE_POLICY,
    VALIDATE_TOOL_CALLS,
    TurnDeps,
    build_graph,
    run_turn,
)
from helpdesk.agent_api.agent.nodes.compose_reply import COMPOSITION_PROMPT
from helpdesk.agent_api.agent.nodes.extract_intent import EXTRACTION_PROMPT
from helpdesk.agent_api.agent.nodes.plan_tools import PLANNING_PROMPT
from helpdesk.agent_api.agent.prompts import load_prompt
from helpdesk.agent_api.agent.state import Citation, ToolResult, TurnOutcome, TurnState
from helpdesk.agent_api.agent.tool_args import SEARCH_POLICY
from helpdesk.agent_api.agent.turn_metrics import trace_attributes
from helpdesk.agent_api.api.routes import chat as chat_routes
from helpdesk.agent_api.services.mcp_client import (
    TOOL_SPAN_PREFIX,
    McpToolCaller,
    ToolCaller,
    TracingToolCaller,
)
from helpdesk.agent_api.services.model_gateway.client import ModelAlias, ModelClient
from helpdesk.agent_api.services.tracing import TURN_TRACE_NAME, configure_tracing
from helpdesk.shared.core.config import load_settings
from helpdesk.shared.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.shared.models.access import RequestStatus
from tests.agent_api.services.test_obs_tracing import (
    FakeTraceClient,
    RaisingTraceClient,
    RecordedSpan,
)

NODE_NAMES: tuple[str, ...] = (
    LOAD_SESSION,
    EXTRACT_INTENT,
    RESOLVE_ENTITIES,
    RETRIEVE_POLICY,
    PLAN_TOOLS,
    VALIDATE_TOOL_CALLS,
    EXECUTE_TOOLS,
    AWAIT_APPROVAL,
    COMPOSE_REPLY,
    REFUSE_AND_TICKET,
    ASK_CLARIFYING,
    ASK_DISAMBIGUATION,
    FAIL_TURN,
)
"""The thirteen nodes `build_graph` registers, and so the thirteen span names."""

HAPPY_PATH_NODES: tuple[str, ...] = (
    LOAD_SESSION,
    EXTRACT_INTENT,
    RESOLVE_ENTITIES,
    RETRIEVE_POLICY,
    PLAN_TOOLS,
    VALIDATE_TOOL_CALLS,
    COMPOSE_REPLY,
)
"""The SEVEN of them a `request_access` turn with an empty plan actually runs.

`plan_tools` answering an empty plan records `CONTINUE` (not `DONE`), so the
turn passes through `validate_tool_calls`, whose `gates.execution_outcome`
answers `DONE` for an empty round and routes to the composition. Nothing
executes, so `execute_tools` is NOT in the list — which is what makes this a
statement about the nodes that RAN rather than the nodes that exist.
"""

CITED_DOCUMENT = "fake-access-policy"

PAGE: dict[str, Any] = _page(_citation(CITED_DOCUMENT, score=0.91))
"""One `search_policy` answer, built ONCE.

`_citation` mints a fresh `chunk_id` per call, so two runs staged from two
pages could never return equal states — and "the unconfigured turn returns
the same state" is exactly what two of the cases below compare.
"""

PERSON_MESSAGE_TEMPLATE = "I need payroll access for {name}, our new joiner"
"""A message that names a person, which is what a real one does (decision G)."""


class _Query(BaseModel):
    """A tool argument model, so the protocol call is made as callers make it."""

    query: str


# --- the harness ---------------------------------------------------------------


@pytest.fixture
def traced() -> Iterator[FakeTraceClient]:
    """A recording trace client for one case, removed after it.

    Never autouse and never a real `Langfuse`: an installed client that leaked
    into the next case would make a "records nothing" assertion pass or fail
    on the previous case's arrangement.
    """
    client = FakeTraceClient()
    configure_tracing(client=client)
    try:
        yield client
    finally:
        configure_tracing()


def _run_traced_turn(
    env: TurnEnv,
    tenant: SeededTurnTenant,
    *,
    session_id: uuid.UUID,
    message: str,
    gateway: StagedGateway,
    tools: ToolCaller,
) -> TurnState:
    """One turn through the stubbed harness, over any `ToolCaller`.

    A copy of `test_agent_turn._run_turn` in one respect only: `tools` is
    typed as the PROTOCOL, so a `TracingToolCaller` wrapping the stub can be
    driven. Everything else — the engine per `asyncio.run`, the mock gateway
    transport, the injected clock — is that file's arrangement.
    """

    async def scenario() -> TurnState:
        engine = create_engine(env.database_url)
        try:
            factory = create_session_factory(engine)
            client = ModelClient(
                base_url=GATEWAY_URL,
                api_keys=API_KEYS,
                transport=httpx.MockTransport(gateway.handler),
            )
            try:
                async with tenant_session(factory, tenant.tenant_id) as db:
                    deps = TurnDeps(db=db, model=client, now=lambda: NOW, tools=tools)
                    return await run_turn(
                        deps=deps,
                        tenant_id=tenant.tenant_id,
                        user_id=tenant.user_id,
                        session_id=session_id,
                        tenant_slug=tenant.tenant_slug,
                        message=message,
                    )
            finally:
                await client.aclose()
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _happy_turn(
    env: TurnEnv,
    *,
    message: str = "I need payroll access",
    tools: ToolCaller | None = None,
) -> TurnState:
    """The three-answer happy path of `test_agent_turn.py`, one fresh session.

    Three staged answers, in order: the extraction, the empty planning round
    and the composition — the same three that file's happy path stages, so a
    turn asking for a fourth fails the case where it happens.
    """
    gateway = StagedGateway([_payroll_answer(), EMPTY_PLAN, COMPOSED_REPLY])
    return _run_traced_turn(
        env,
        env.tenant_a,
        session_id=uuid.uuid4(),
        message=message,
        gateway=gateway,
        tools=tools if tools is not None else StubToolCaller(payload=PAGE),
    )


def _flatten(spans: list[RecordedSpan]) -> list[RecordedSpan]:
    """Every recorded span, parents before children, in the order opened."""
    out: list[RecordedSpan] = []
    for recorded in spans:
        out.append(recorded)
        out.extend(_flatten(recorded.children))
    return out


def _as_text(client: FakeTraceClient) -> str:
    """Everything the client was told, as one string to sweep for a leak."""
    return json.dumps(
        [
            {"name": recorded.name, "metadata": recorded.metadata}
            for recorded in _flatten(client.roots)
        ],
        default=str,
    )


def _citation_model(document: str) -> Citation:
    """One obviously fake citation, as `TurnState` holds it."""
    return Citation(
        document=document,
        title=f"Fake {document} Policy",
        version="v1",
        heading_path=("Fake Section",),
        chunk_id=uuid.uuid4(),
        text="Obviously fake policy text.",
        score=0.5,
    )


def _state(
    *,
    outcome: TurnOutcome = TurnOutcome.DONE,
    tool_results: tuple[ToolResult, ...] = (),
    citations: tuple[Citation, ...] = (),
) -> TurnState:
    """A `TurnState` carrying only what `trace_attributes` is allowed to read."""
    return TurnState(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        tenant_slug="fake-tenant",
        user_message="obviously fake message",
        outcome=outcome,
        tool_results=tool_results,
        citations=citations,
    )


def _result(payload: dict[str, Any]) -> ToolResult:
    return ToolResult(tool="classify_access_request", ok=True, payload=payload)


# --- the constants and the two tables ------------------------------------------


def test_the_tool_span_prefix_is_exactly_the_planned_value() -> None:
    """Catches a prefix invented at the call site ("mcp.", "tool_", or the
    bare tool name): the prefix is what separates a tool span from a node span
    by name alone in Langfuse, so a drifting value makes `tool:search_policy`
    unfindable and a bare name collide with a node's."""
    assert TOOL_SPAN_PREFIX == "tool:"


def test_the_turn_trace_name_is_the_one_the_facade_fixes() -> None:
    """Catches a `run_turn` that names its root span itself instead of using
    the facade's constant — the two would then drift apart with nothing else
    in the tree comparing them, and `stream_turn` and `resume_graph` would
    each be free to pick a third name."""
    assert TURN_TRACE_NAME == "chat_turn"


def test_the_node_model_alias_table_is_exactly_the_three_model_calling_nodes() -> None:
    """Catches a table that hands every node an alias (a span claiming
    `load_session` called `agent-default` is a lie a cost dashboard adds up)
    and one that gives `plan_tools` the default alias — planning is the one
    node on `agent-strong` (17c decision G), so a copy-pasted row would report
    every planning round against the wrong model."""
    assert _NODE_MODEL_ALIASES == {
        EXTRACT_INTENT: ModelAlias.AGENT_DEFAULT,
        PLAN_TOOLS: ModelAlias.AGENT_STRONG,
        COMPOSE_REPLY: ModelAlias.AGENT_DEFAULT,
    }


def test_the_node_prompt_table_is_exactly_the_three_prompt_loading_nodes() -> None:
    """Catches a table keyed on the wrong prompt for a node: `prompt_version`
    is the hash a bad turn is tied back to (the design), so a `plan_tools`
    span carrying the extraction prompt's digest points every investigation at
    a file that had nothing to do with the failure."""
    assert _NODE_PROMPTS == {
        EXTRACT_INTENT: EXTRACTION_PROMPT,
        PLAN_TOOLS: PLANNING_PROMPT,
        COMPOSE_REPLY: COMPOSITION_PROMPT,
    }


def test_the_thirteen_node_names_are_the_keys_the_graph_registers() -> None:
    """Catches a span name that drifts from the node key LangGraph reports —
    a wrapper naming its span `"load-session"` or `"nodes.load_session"` while
    the graph registers `load_session`. The spans of a turn are supposed to BE
    the node names §1 lists, so a renamed constant breaks the trace and the
    graph in one move while every existing turn test stays green."""
    registered = set(build_graph().nodes)

    assert len(NODE_NAMES) == 13
    assert len(set(NODE_NAMES)) == 13
    assert set(NODE_NAMES) <= registered


# --- the trace attributes, purely --------------------------------------------


@pytest.mark.parametrize(
    ("payloads", "expected"),
    [
        pytest.param([], None, id="no-tool-named-one"),
        pytest.param([{"risk_class": "low"}], "low", id="one-low"),
        pytest.param(
            [{"risk_class": "low"}, {"risk_class": "critical"}], "critical", id="low-then-critical"
        ),
        pytest.param(
            [{"risk_class": "critical"}, {"risk_class": "elevated"}],
            "critical",
            id="critical-then-elevated",
        ),
        pytest.param(
            [{"risk_class": "privileged"}, {"risk_class": "elevated"}],
            "privileged",
            id="privileged-then-elevated",
        ),
        pytest.param(
            [{"citations": []}, {"risk_class": "elevated"}],
            "elevated",
            id="a-payload-with-no-class",
        ),
        pytest.param([{"citations": []}], None, id="only-payloads-with-no-class"),
    ],
)
def test_the_traces_risk_class_is_the_highest_any_tool_named(
    payloads: list[dict[str, Any]], expected: str | None
) -> None:
    """Catches an implementation reading the FIRST or the LAST tool result's
    class instead of the highest: the two ordered rows disagree under either
    rule, so only a severity comparison answers both. The two rows whose
    payload has no `risk_class` catch a lookup written as `payload["risk_class"]`
    — `search_policy` answers every turn and names no class, so that
    implementation raises inside the trace attributes of an ordinary turn.
    A turn where nothing classified anything answers `None`, which the facade
    then DROPS rather than sending a null."""
    state = _state(tool_results=tuple(_result(payload) for payload in payloads))

    assert trace_attributes(state)["risk_class"] == expected


@pytest.mark.parametrize(
    ("outcome", "refused"),
    [
        pytest.param(TurnOutcome.DONE, False, id="done"),
        pytest.param(TurnOutcome.REFUSE, True, id="refuse"),
        pytest.param(TurnOutcome.FAILED, True, id="failed"),
        pytest.param(TurnOutcome.AWAIT_APPROVAL, False, id="await-approval"),
        pytest.param(TurnOutcome.CLARIFY, False, id="clarify"),
        pytest.param(TurnOutcome.DISAMBIGUATE, False, id="disambiguate"),
        pytest.param(TurnOutcome.CONTINUE, False, id="continue"),
    ],
)
def test_refused_is_the_two_refusing_outcomes_and_decision_is_the_outcome_value(
    outcome: TurnOutcome, refused: bool
) -> None:
    """Catches `refused` written as "anything that is not done": the
    `await_approval`, `clarify` and `disambiguate` rows all pass under that
    rule and are not refusals — a turn paused for an approver would be counted
    against the refusal rate operators watch. `decision` is asserted as the
    enum's VALUE, which catches a trace carrying `TurnOutcome.DONE` (the
    repr) where every other telemetry surface in this repo carries `done`."""
    attributes = trace_attributes(_state(outcome=outcome))

    assert attributes["decision"] == outcome.value
    assert attributes["refused"] is refused


def test_the_attributes_are_exactly_the_six_planned_keys_read_off_the_state() -> None:
    """Catches an implementation that adds a seventh field — the user id, the
    intent, the message — to the map that becomes the trace's attributes, and
    one that hardcodes `citations_count` or counts the proposed calls instead
    of the citations. The ids are asserted as STRINGS of the state's uuids,
    which is what an attribute value has to be."""
    citations = (_citation_model("fake-a"), _citation_model("fake-b"))
    state = _state(citations=citations, tool_results=(_result({"risk_class": "elevated"}),))

    attributes = trace_attributes(state)

    assert set(attributes) == {
        "tenant_id",
        "session_id",
        "risk_class",
        "decision",
        "refused",
        "citations_count",
    }
    assert attributes["tenant_id"] == str(state.tenant_id)
    assert attributes["session_id"] == str(state.session_id)
    assert attributes["citations_count"] == 2
    assert attributes["risk_class"] == "elevated"


# --- one turn, one trace, the nodes that ran ----------------------------------


def test_a_turn_records_one_trace_whose_child_spans_are_the_nodes_it_ran_in_order(
    turn_env: TurnEnv,  # noqa: F811
    traced: FakeTraceClient,
) -> None:
    """Catches a wrapper applied per NODE MODULE rather than where the nodes
    are registered — the four nodes this turn never visits would then appear
    in the trace, or the seven it did visit would appear out of order. It also
    catches a `run_turn` opening a trace per node instead of one per turn: the
    root count is asserted, so seven roots is a red rather than a trace that
    merely looks busy.

    `execute_tools` is the load-bearing absence here: the empty planning round
    means it never runs, so a trace listing it is a trace of the graph rather
    than of the turn."""
    state = _happy_turn(turn_env)

    # Premise, read off the returned state: this really is the happy path the
    # seven-node list describes.
    assert state.outcome is TurnOutcome.DONE

    root = traced.only_root
    assert len(traced.roots) == 1
    assert root.name == TURN_TRACE_NAME
    assert [child.name for child in root.children] == list(HAPPY_PATH_NODES)


def test_the_tool_call_opens_a_span_under_the_node_that_made_it(
    turn_env: TurnEnv,  # noqa: F811
    traced: FakeTraceClient,
) -> None:
    """Catches a `TracingToolCaller` that opens its span outside the node
    (wrapping the whole session, or opening one in `__aenter__`): the span
    would hang off the turn rather than off `retrieve_policy`, and a reader
    could not tell which node made a call — the only question a tool span
    answers. The stub's own `entered` count is the premise that the session
    WAS entered, so "no span for the enter" is an absence with a fact behind
    it rather than a caller that never ran."""
    inner = StubToolCaller(payload=PAGE)

    state = _happy_turn(turn_env, tools=TracingToolCaller(inner))

    # Premise, read off the stub: one call, and the session was opened and
    # closed around it.
    assert inner.names == [SEARCH_POLICY]
    assert (inner.entered, inner.exited) == (1, 1)
    assert state.outcome is TurnOutcome.DONE

    root = traced.only_root
    by_name = {child.name: child for child in root.children}
    assert [child.name for child in by_name[RETRIEVE_POLICY].children] == [
        f"{TOOL_SPAN_PREFIX}{SEARCH_POLICY}"
    ]
    tool_spans = [
        recorded
        for recorded in _flatten(traced.roots)
        if recorded.name.startswith(TOOL_SPAN_PREFIX)
    ]
    assert len(tool_spans) == 1


def test_no_tool_argument_reaches_the_span_that_wraps_the_call(
    turn_env: TurnEnv,  # noqa: F811
    traced: FakeTraceClient,
) -> None:
    """Catches a tool span that captures its arguments (the SDK's own default
    input capture, decision G's rejected option). `search_policy`'s argument
    is the user's query, which is derived from their message — so an
    argument-capturing span publishes the message under a name that looks
    like metadata rather than content."""
    inner = StubToolCaller(payload=PAGE)
    message = "I need payroll access for the fake finance system"

    _happy_turn(turn_env, message=message, tools=TracingToolCaller(inner))

    # Premise, read off the stub: the query really was built from the message,
    # so its absence below is a decision rather than an empty argument.
    assert inner.calls[0][1]["query"]
    recorded = _as_text(traced)
    assert inner.calls[0][1]["query"] not in recorded
    assert message not in recorded


def test_the_traces_attributes_are_the_values_that_turns_state_carries(
    turn_env: TurnEnv,  # noqa: F811
    traced: FakeTraceClient,
) -> None:
    """Catches attributes written from somewhere other than the finished state
    — a `decision` taken from the state as of the first node (`continue`), a
    `citations_count` of zero on a turn that cited a document, or a tenant
    read from the message instead of the token. `risk_class` is ABSENT rather
    than null because no tool of this turn named one and the facade drops a
    `None`; a trace carrying `risk_class: null` here fails, which is the
    difference between "not classified" and "classified as nothing"."""
    state = _happy_turn(turn_env)

    # Premise, read off the returned state.
    assert state.outcome is TurnOutcome.DONE
    assert len(state.citations) == 1

    metadata = traced.only_root.metadata
    assert metadata == {
        "tenant_id": str(turn_env.tenant_a.tenant_id),
        "session_id": str(state.session_id),
        "decision": TurnOutcome.DONE.value,
        "refused": False,
        "citations_count": 1,
    }


def test_the_model_and_prompt_attributes_ride_only_the_nodes_that_use_them(
    turn_env: TurnEnv,  # noqa: F811
    traced: FakeTraceClient,
) -> None:
    """Catches a wrapper that puts the model alias and a prompt version on
    EVERY node span: `load_session` and `resolve_entities` call no model and
    load no prompt, so a trace claiming they did makes the per-node cost and
    the prompt attribution meaningless. It also catches a `prompt_version`
    hand-written as a version string rather than the digest `prompts.py`
    computes over the file — the whole point of the field is that it is
    derived from the bytes that were sent."""
    expected: dict[str, dict[str, Any]] = {
        LOAD_SESSION: {},
        EXTRACT_INTENT: {
            "model_alias": ModelAlias.AGENT_DEFAULT.value,
            "prompt_version": load_prompt(EXTRACTION_PROMPT).sha256,
        },
        RESOLVE_ENTITIES: {},
        RETRIEVE_POLICY: {},
        PLAN_TOOLS: {
            "model_alias": ModelAlias.AGENT_STRONG.value,
            "prompt_version": load_prompt(PLANNING_PROMPT).sha256,
        },
        VALIDATE_TOOL_CALLS: {},
        COMPOSE_REPLY: {
            "model_alias": ModelAlias.AGENT_DEFAULT.value,
            "prompt_version": load_prompt(COMPOSITION_PROMPT).sha256,
        },
    }

    _happy_turn(turn_env)

    recorded = {child.name: child.metadata for child in traced.only_root.children}
    assert recorded == expected


def test_no_span_of_a_turn_carries_a_person_the_user_id_or_the_message(
    turn_env: TurnEnv,  # noqa: F811
    traced: FakeTraceClient,
) -> None:
    """The §5 risk case. Catches a trace that captures the turn's input — the
    SDK's default, and the one thing decision G forbids — or an attribute set
    that carries `user_id` beside the session id. the design permits four
    fields in telemetry and a person's name is not one of them, so a message
    naming a colleague ("grant Anna the finance role") must reach no span name
    and no attribute value.

    The name planted here is the seeded person's own, so a span carrying the
    resolved entity rather than the raw message fails too."""
    tenant = turn_env.tenant_a
    display_name = f"Fake Person 1 ({tenant.tenant_slug})"
    email = f"fake.person1@{tenant.tenant_slug}.example.test"
    message = PERSON_MESSAGE_TEMPLATE.format(name=display_name)

    state = _happy_turn(turn_env, message=message)

    # Premise, read off the returned state: the message really was carried
    # through the turn, so its absence from the spans is the control's doing.
    assert state.user_message == message

    recorded = _as_text(traced)
    for forbidden in (display_name, email, message, str(tenant.user_id)):
        assert forbidden not in recorded


# --- the two fail-open turns ---------------------------------------------------


def test_with_tracing_unconfigured_the_turn_is_unchanged_and_no_client_is_opened(
    turn_env: TurnEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§1's unconfigured criterion, both halves. Catches a `run_turn` that
    depends on the trace for something real — a trace id it then requires, or
    a span whose exit writes state — and a facade that builds an SDK client
    anyway, which an operator with no Langfuse account would discover as an
    exporter thread retrying against a host they never configured.

    The state is compared field by field against the traced run's, minus the
    session id: two turns over one message differ in nothing else, so an
    outcome, a reply, a token count or a citation moved by tracing shows up
    here rather than in production."""
    constructions: list[dict[str, Any]] = []

    def _record(self: Langfuse, **kwargs: Any) -> None:
        constructions.append(dict(kwargs))

    monkeypatch.setattr(Langfuse, "__init__", _record)

    client = FakeTraceClient()
    configure_tracing(client=client)
    try:
        traced_state = _happy_turn(turn_env)
    finally:
        configure_tracing()

    settings = load_settings().model_copy(
        update={"langfuse_public_key": None, "langfuse_secret_key": None}
    )
    configure_tracing(settings=settings)
    try:
        untraced_state = _happy_turn(turn_env)
    finally:
        configure_tracing()

    assert constructions == []
    assert len(client.roots) == 1, "the unconfigured turn recorded a span"
    assert untraced_state.model_dump(exclude={"session_id"}) == traced_state.model_dump(
        exclude={"session_id"}
    )


def test_a_client_whose_every_method_raises_leaves_the_turn_untouched(
    turn_env: TurnEnv,  # noqa: F811
) -> None:
    """§1's broken-SDK criterion. Catches an implementation that lets the SDK
    fail the turn: the client raises on the FIRST call, so a facade that does
    not swallow takes down `run_turn` before `load_session` ever runs, and a
    Langfuse outage becomes an outage of the product. The state comparison is
    what makes "the node spans are skipped, not propagated" a fact rather than
    a caught exception hiding a half-run turn."""
    client = FakeTraceClient()
    configure_tracing(client=client)
    try:
        traced_state = _happy_turn(turn_env)
    finally:
        configure_tracing()

    raising = RaisingTraceClient()
    configure_tracing(client=raising)
    try:
        broken_state = _happy_turn(turn_env)
    finally:
        configure_tracing()

    # Premise, read off the raising client: the facade really did call it.
    assert raising.calls > 0
    assert broken_state.model_dump(exclude={"session_id"}) == traced_state.model_dump(
        exclude={"session_id"}
    )


# --- the pause and the resume are two turns, so two traces --------------------


def test_a_paused_turn_closes_its_trace_and_the_resume_opens_its_own(
    approval_env: ApprovalEnv,  # noqa: F811
    traced: FakeTraceClient,
) -> None:
    """Catches a trace held open across the pause — the resume's spans would
    join a trace whose turn ended when the approver was asked, which may be
    days earlier, and the paused turn would never be exported at all. It also
    catches a paused terminal that writes no attributes: `await_approval` is
    the one decision value a reader looks for to ask "what is waiting", and
    `run_turn`'s paused return is a terminal of that turn exactly as plan
    20b's counter treats it.

    The resume's `decision` is asserted too, because an implementation that
    wrote attributes only in `run_turn` leaves the resumed trace bare while
    still producing two traces."""
    session_id, request_id, paused = _pause(approval_env)
    _store_request(
        approval_env,
        request_id=request_id,
        status=RequestStatus.APPROVED.value,
        session_id=session_id,
    )
    resumed = _resume(
        approval_env,
        request_id=request_id,
        gateway=StagedGateway([COMPOSED_APPROVAL]),
        tools=ScriptedToolCaller(),
    )

    # Premise, read off the two states.
    assert paused.outcome is TurnOutcome.AWAIT_APPROVAL
    assert resumed.outcome is TurnOutcome.DONE

    assert len(traced.roots) == 2
    first, second = traced.roots
    assert [root.name for root in traced.roots] == [TURN_TRACE_NAME, TURN_TRACE_NAME]
    assert first.trace_id != second.trace_id
    assert first.metadata["decision"] == TurnOutcome.AWAIT_APPROVAL.value
    assert first.metadata["refused"] is False
    assert second.metadata["decision"] == TurnOutcome.DONE.value


# --- the route builds the wrapped caller --------------------------------------


def test_the_route_builds_the_tracing_caller_around_the_mcp_one() -> None:
    """Catches a `TracingToolCaller` that exists and is never wired: every
    case above drives the wrapper directly, so without this one the tool spans
    would be green in the suite and absent in the product. Nothing is entered,
    so no socket is opened — the assertion is on what was CONSTRUCTED."""
    settings = load_settings()

    caller = chat_routes.build_tool_caller(settings, "obviously-fake-bearer-token")

    assert isinstance(caller, TracingToolCaller)
    assert isinstance(caller.inner, McpToolCaller)


def test_the_tracing_caller_returns_the_inner_callers_answer(
    traced: FakeTraceClient,
) -> None:
    """Catches a decorator that swallows or reshapes the tool's answer while
    dutifully opening its span: every node reads the mapping the tool
    returned, so a wrapper returning `None` breaks the turn in a way no span
    assertion above would see. No graph and no database here — the protocol
    is the whole surface."""

    async def scenario() -> Mapping[str, Any]:
        inner = StubToolCaller(payload=PAGE)
        caller = TracingToolCaller(inner)
        async with caller as entered:
            return await entered.call_tool(SEARCH_POLICY, _Query(query="fake query"))

    answer = asyncio.run(scenario())

    assert answer == PAGE
    names = [recorded.name for recorded in _flatten(traced.roots)]
    assert names == [f"{TOOL_SPAN_PREFIX}{SEARCH_POLICY}"]
