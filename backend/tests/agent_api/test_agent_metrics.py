"""`/metrics` on agent-api, and the turn call sites.

Written before the implementation exists. Surface under test:

``agent_api.main.app`` — an unauthenticated ``GET /metrics`` answering
    ``helpdesk.obs.metrics.render()``.
``agent_api.metrics``
    ``def record_turn_state(state: TurnState) -> None`` — the ONE place a
        ``TurnState`` becomes labels: intent from ``state.extraction.intent``
        (``"unknown"`` when there is no extraction), decision from
        ``state.outcome``, tenant from ``str(state.tenant_id)``; a ``REFUSE``
        or ``FAILED`` outcome also records a refusal whose reason is
        ``state.error_code``'s value.
    ``def record_turn_code(code: ErrorCode) -> None`` — the code-only variant
        for a stream that fails before any state exists: intent ``"unknown"``,
        decision ``"failed"``, tenant from
        ``helpdesk.obs.context.current_context()``.
Called at the terminals of ``graph.build.run_turn``, ``graph.build.stream_turn``
(its early returns included) and ``resume.resume_turn`` — so the
two routes cannot drift and a resume is counted at all.

Cost and isolation. The compose Postgres and Redis through the suite's
existing session-scoped fixtures — ``test_chat_stream.py``'s seeded throwaway
database for the two routes, ``test_agent_approval_interrupt.py``'s for the
paused thread a resume needs. Nothing is rebuilt per case:
each case's isolation is its own ``CollectorRegistry`` installed over
``helpdesk.obs.metrics``'s module-level pair, so every number here is an
absolute value and never a delta.
"""

import asyncio
import uuid
from collections.abc import Iterator, Mapping

import httpx
import pytest
from prometheus_client import CollectorRegistry

from helpdesk.agent_api.agent.state import Intent, TurnOutcome
from helpdesk.agent_api.api.sse import ERROR
from helpdesk.shared.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.shared.core.errors import ErrorCode
from helpdesk.shared.core.observability import metrics
from helpdesk.shared.crud import Repository
from helpdesk.shared.models import User
from helpdesk.shared.models.access import RequestStatus
from tests.agent_api.test_agent_approval_interrupt import (
    ApprovalEnv,
    approval_env,  # noqa: F401 - fixture resolved by name
)
from tests.agent_api.test_agent_approval_interrupt import (
    ScriptedToolCaller as ApprovalToolCaller,
)
from tests.agent_api.test_agent_approval_interrupt import (
    StagedGateway as ApprovalGateway,
)
from tests.agent_api.test_agent_approval_resume import (
    COMPOSED_APPROVAL,
    _pause,
    _resume,
    _store_request,
)

# The unreachable-gateway transport is this suite's, not a new one: a turn that
# cannot reach the gateway is how a FAILED terminal is produced without faking
# a node.
from tests.agent_api.test_chat_route import UnreachableGateway
from tests.agent_api.test_chat_stream import (
    MESSAGE,
    ScriptedToolCaller,
    StagedGateway,
    StreamRouteEnv,
    UnreachableToolCaller,
    _drive,
    _of,
    _one_turn,
    _page,
    _token,
    parse_frames,
    stream_env,  # noqa: F401 - fixture resolved by name
    stream_route_db,  # noqa: F401 - fixture resolved by name
)

TURNS = "helpdesk_turns_total"
REFUSALS = "helpdesk_refusals_total"

UNKNOWN_INTENT = "unknown"
"""The sentinel label for a turn with no extraction, verbatim.

A literal rather than a member of `Intent`: the closed set has
no "we never got that far" member, and adding one would put a label the model
may answer with next to one it may not.
"""


@pytest.fixture
def fresh_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[CollectorRegistry]:
    """A registry of this case's own, installed over the module-level pair."""
    registry = CollectorRegistry()
    monkeypatch.setattr(metrics, "REGISTRY", registry)
    monkeypatch.setattr(metrics, "METRICS", metrics.build_metrics(registry))
    yield registry


def _sample(registry: CollectorRegistry, name: str, labels: Mapping[str, str]) -> float | None:
    return registry.get_sample_value(name, dict(labels))


def _sum_of(registry: CollectorRegistry, name: str) -> float:
    """Every sample of `name`, added up across label sets.

    A cell of 1 says the right series moved; this total says no other series
    moved as well, which is the half "exactly one" needs.
    """
    return sum(
        sample.value
        for family in registry.collect()
        for sample in family.samples
        if sample.name == name
    )


def _turn_labels(env: StreamRouteEnv, *, intent: str, decision: str) -> dict[str, str]:
    return {
        "tenant": str(env.tenant_a.tenant_id),
        "intent": intent,
        "decision": decision,
    }


def _seeded_identity(env: StreamRouteEnv) -> tuple[str, ...]:
    """The seeded person's id, email and display name, read off the row.

        Read back from the database rather than rebuilt from the module's literals:
    the identity claim is about the person the turn
        actually ran as, and a value this test composed itself would pass even if
        the row said something else.
    """

    async def scenario() -> tuple[str, ...]:
        engine = create_engine(env.database_url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, env.tenant_a.tenant_id) as db:
                row = await Repository(db, User).get(env.tenant_a.user_id)
                assert row is not None, "premise: the seeded person exists"
                return (str(row.id), row.email, row.display_name)
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


# --- the two routes -----------------------------------------------------------


def test_one_chat_turn_counts_exactly_one(
    stream_env: StreamRouteEnv,  # noqa: F811
    fresh_registry: CollectorRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a turn counted per NODE rather than per turn — the shape a
    counter dropped into the graph's node wrapper takes, which would report
    eight turns for one message and make every rate on this metric wrong by
    the graph's depth — and catches a counter that never fires at all.

    The labels are asserted as the closed values: the tenant from the
    token's uuid, the intent from the extraction the model returned, and the
    decision from `TurnOutcome`. The total pins that no second series moved, so
    a turn counted once under the right labels and once under a fallback set
    fails here.
    """
    session_id = uuid.uuid4()
    gateway = StagedGateway(_one_turn())
    tools = ScriptedToolCaller(_page())

    async def scenario(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post(
            "/chat",
            json={"message": MESSAGE},
            headers={"Authorization": f"Bearer {_token(stream_env.tenant_a, session_id)}"},
        )

    response = _drive(
        monkeypatch=monkeypatch, scenario=scenario, gateway_handler=gateway.handler, tools=tools
    )

    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == TurnOutcome.DONE.value
    labels = _turn_labels(
        stream_env, intent=Intent.REQUEST_ACCESS.value, decision=TurnOutcome.DONE.value
    )
    assert _sample(fresh_registry, TURNS, labels) == 1.0
    assert _sum_of(fresh_registry, TURNS) == 1.0
    assert _sum_of(fresh_registry, REFUSALS) == 0.0


def test_one_streamed_turn_counts_exactly_one(
    stream_env: StreamRouteEnv,  # noqa: F811
    fresh_registry: CollectorRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches `stream_turn` left uninstrumented, which is invisible on any
    dashboard: `/chat` and `/chat/stream` are two views of ONE turn,
    so a counter placed on the plain route only makes every streamed turn —
    which is what the web client sends — disappear from the turn rate.

    Same message, same labels and the same absolute 1 as the `/chat` case, so
    the two cases together are what pins "one turn, one count" for both routes.
    It also catches a stream instrumented per FRAME: a token event per chunk
    would show up here as a count in the dozens.
    """
    session_id = uuid.uuid4()
    gateway = StagedGateway(_one_turn())
    tools = ScriptedToolCaller(_page())

    async def scenario(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post(
            "/chat/stream",
            json={"message": MESSAGE},
            headers={"Authorization": f"Bearer {_token(stream_env.tenant_a, session_id)}"},
        )

    response = _drive(
        monkeypatch=monkeypatch, scenario=scenario, gateway_handler=gateway.handler, tools=tools
    )

    assert response.status_code == 200, response.text
    labels = _turn_labels(
        stream_env, intent=Intent.REQUEST_ACCESS.value, decision=TurnOutcome.DONE.value
    )
    assert _sample(fresh_registry, TURNS, labels) == 1.0
    assert _sum_of(fresh_registry, TURNS) == 1.0
    assert _sum_of(fresh_registry, REFUSALS) == 0.0


# --- the resumed turn ---------------------------------------------------------


def test_a_resumed_turn_counts_exactly_one(
    approval_env: ApprovalEnv,  # noqa: F811
    fresh_registry: CollectorRegistry,
) -> None:
    """Catches a resume counted once per node REPLAYED.

    LangGraph replays the interrupting node from its start on a resume, so a
    counter placed inside a node — or on the graph's update stream — fires
    again for work that already happened, and the turn rate climbs every time
    an approver decides. Only `resume_turn`'s terminal fires once, which is
    what the absolute 1 on the `done` cell says.

    The paused turn's own count is asserted beside it rather than isolated
    away: the pause and the resume are two counts of "a resumed turn adds
    exactly one", and a total of 2 is the only reading in which neither was
    lost and neither was doubled. The two cells differ ONLY in the decision
    label, so an implementation that reported both under one outcome fails
    here.
    """
    tenant = str(approval_env.tenant_id)
    session_id, request_id, paused = _pause(approval_env)

    assert paused.outcome is TurnOutcome.AWAIT_APPROVAL, "premise: the thread is paused"
    paused_labels = {
        "tenant": tenant,
        "intent": Intent.REQUEST_ACCESS.value,
        "decision": TurnOutcome.AWAIT_APPROVAL.value,
    }
    assert _sample(fresh_registry, TURNS, paused_labels) == 1.0

    _store_request(
        approval_env,
        request_id=request_id,
        status=RequestStatus.APPROVED.value,
        session_id=session_id,
    )
    state = _resume(
        approval_env,
        request_id=request_id,
        gateway=ApprovalGateway([COMPOSED_APPROVAL]),
        tools=ApprovalToolCaller(),
    )

    assert state.outcome is TurnOutcome.DONE
    resumed_labels = {
        "tenant": tenant,
        "intent": Intent.REQUEST_ACCESS.value,
        "decision": TurnOutcome.DONE.value,
    }
    assert _sample(fresh_registry, TURNS, resumed_labels) == 1.0
    # The asking turn counts too: ask, pause, resume.
    assert _sum_of(fresh_registry, TURNS) == 3.0
    assert _sum_of(fresh_registry, REFUSALS) == 0.0


# --- the refusal counter ------------------------------------------------------


def test_a_failed_turn_counts_a_refusal_under_its_error_code(
    stream_env: StreamRouteEnv,  # noqa: F811
    fresh_registry: CollectorRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a refusal counter that never fires, and one whose `reason` is
    anything but the turn's typed `ErrorCode` value.

    A FAILED turn is the one case that must move BOTH counters — it is a turn
    that happened and a refusal the user saw — so an implementation that
    recorded the refusal INSTEAD of the turn silently drops it from the turn
    rate, and one that recorded only the turn leaves
    `rate(helpdesk_refusals_total[5m])` flat through an outage. Both are
    asserted, with the total on each pinning that exactly one series moved.

    `reason` is `INTERNAL`, read off `ErrorCode`: a reason built from the
    exception's text would be free-form client-adjacent data in a label, which
    is a second risk, and a reason built from the HTTP status would
    collapse every 500 into one bucket.
    """
    session_id = uuid.uuid4()
    gateway = UnreachableGateway()

    async def scenario(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post(
            "/chat",
            json={"message": MESSAGE},
            headers={"Authorization": f"Bearer {_token(stream_env.tenant_a, session_id)}"},
        )

    response = _drive(
        monkeypatch=monkeypatch,
        scenario=scenario,
        gateway_handler=gateway.handler,
        tools=ScriptedToolCaller(),
    )

    assert response.status_code == 500
    assert response.json() == {"code": ErrorCode.INTERNAL.value}

    turn_labels = _turn_labels(stream_env, intent=UNKNOWN_INTENT, decision=TurnOutcome.FAILED.value)
    assert _sample(fresh_registry, TURNS, turn_labels) == 1.0
    assert _sum_of(fresh_registry, TURNS) == 1.0

    refusal_labels = {
        "tenant": str(stream_env.tenant_a.tenant_id),
        "reason": ErrorCode.INTERNAL.value,
    }
    assert _sample(fresh_registry, REFUSALS, refusal_labels) == 1.0
    assert _sum_of(fresh_registry, REFUSALS) == 1.0


def test_a_stream_that_fails_before_any_state_counts_intent_unknown(
    stream_env: StreamRouteEnv,  # noqa: F811
    fresh_registry: CollectorRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches `stream_turn`'s EARLY returns left uninstrumented.

    The MCP handshake fails outside every node, so no `TurnState` was ever
    finished and `record_turn_state` has nothing to be called with: an
    implementation with only the state-shaped helper counts nothing at all
    here, and the turns that fail hardest are exactly the ones missing from the
    rate. It also catches an intent label left empty or set to `None` on that
    path — `""` and `"None"` are both label values a dashboard cannot group on
    — so the sentinel is asserted as the literal `"unknown"`.

    The tenant still has to be right: there is no state to read it from, so it
    comes from the telemetry context the middleware bound, and an
    implementation that shrugged and wrote an empty tenant fails on the label.
    """
    session_id = uuid.uuid4()
    gateway = StagedGateway([])

    async def scenario(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post(
            "/chat/stream",
            json={"message": MESSAGE},
            headers={"Authorization": f"Bearer {_token(stream_env.tenant_a, session_id)}"},
        )

    response = _drive(
        monkeypatch=monkeypatch,
        scenario=scenario,
        gateway_handler=gateway.handler,
        tools=UnreachableToolCaller(),
    )

    assert response.status_code == 200, response.text
    errors = _of(parse_frames(response.text), ERROR)
    assert len(errors) == 1
    assert errors[0].data == {"code": ErrorCode.INTERNAL.value}, "premise: the stream failed typed"

    labels = _turn_labels(stream_env, intent=UNKNOWN_INTENT, decision=TurnOutcome.FAILED.value)
    assert _sample(fresh_registry, TURNS, labels) == 1.0
    assert _sum_of(fresh_registry, TURNS) == 1.0

    refusal_labels = {
        "tenant": str(stream_env.tenant_a.tenant_id),
        "reason": ErrorCode.INTERNAL.value,
    }
    assert _sample(fresh_registry, REFUSALS, refusal_labels) == 1.0
    assert _sum_of(fresh_registry, REFUSALS) == 1.0


# --- the identity claim -------------------------------------------------------


def test_the_exposition_names_no_user_of_the_seeded_tenant(
    stream_env: StreamRouteEnv,  # noqa: F811
    fresh_registry: CollectorRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a label carrying identity — a `user` label on the turn counter
    is the obvious one, and it is the change most likely to be asked for.

    Telemetry may carry tenant, session, trace and principal kind
    and nothing else, and `/metrics` is an UNAUTHENTICATED endpoint,
    so a user id there is published to anyone who can reach the port. The whole
    exposition is searched for the id, the email and the display name of the
    person whose turn just ran, read off the row, so any of the three reaching
    a label value or an exemplar fails here.

    The tenant is asserted PRESENT in the same body, which is what stops the
    claim being satisfied by an endpoint that returns nothing: the rule
    allows the tenant, and a scrape with no series in it would pass every
    absence check above.
    """
    session_id = uuid.uuid4()
    gateway = StagedGateway(_one_turn())
    tools = ScriptedToolCaller(_page())

    async def scenario(client: httpx.AsyncClient) -> tuple[httpx.Response, httpx.Response]:
        turn = await client.post(
            "/chat",
            json={"message": MESSAGE},
            headers={"Authorization": f"Bearer {_token(stream_env.tenant_a, session_id)}"},
        )
        return turn, await client.get("/metrics")

    turn, scrape = _drive(
        monkeypatch=monkeypatch, scenario=scenario, gateway_handler=gateway.handler, tools=tools
    )

    assert turn.status_code == 200, turn.text
    assert scrape.status_code == 200, scrape.text
    assert scrape.headers["content-type"] == "text/plain; version=1.0.0; charset=utf-8"

    body = scrape.text
    assert str(stream_env.tenant_a.tenant_id) in body, "premise: the turn reached the exposition"
    assert str(session_id) not in body
    for value in _seeded_identity(stream_env):
        assert value not in body, value
