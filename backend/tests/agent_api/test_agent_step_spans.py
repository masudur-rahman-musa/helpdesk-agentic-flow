"""Each model step of a turn opens its own `model_step` span.

Expected implementation surface: in `helpdesk/agent_api/agent/graph.py`, the
model call inside `_ask` runs inside
``span("model_step", attributes={"model_alias": "agent-default", "step": n})``
where ``n`` counts the model steps of the turn (1, 2, ...). The attributes hold
nothing from the messages. An error raised by the model call passes out through
the span, so the span is closed with that error.

The trace client is a recording fake (no network). The gateway is the scripted
`httpx.MockTransport` of the loop tests. No database, no real waiting.
"""

import asyncio
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from helpdesk.agent_api.agent.loop import run_loop_turn
from helpdesk.agent_api.agent.state import TurnOutcome
from helpdesk.agent_api.services.model_gateway.client import ModelClient
from helpdesk.agent_api.services.tracing import TURN_TRACE_NAME, configure_tracing, span
from helpdesk.shared.core.config import Settings
from tests.agent_api.test_agent_loop import (
    API_KEYS,
    GATEWAY_URL,
    SESSION_ID,
    TENANT_ID,
    TENANT_SLUG,
    USER_ID,
    Clock,
    FakeStore,
    FakeTools,
    ScriptedGateway,
    _deps,
    answer_body,
    run,
    wire_call,
)

MESSAGE = "Which requests of mine are still open, zebra-quartz?"
REPLY = "You have one request, REQ-0001, and it was turned down, marmot-felt."


@dataclass
class Recorded:
    """One span the fake saw: name, attributes, children, and how it ended."""

    name: str
    metadata: dict[str, Any] = field(default_factory=dict)
    children: list["Recorded"] = field(default_factory=list)
    closed: bool = False
    error: BaseException | None = None


class RecordingClient:
    """A trace client that keeps the tree and the error each span was closed with."""

    def __init__(self) -> None:
        self.roots: list[Recorded] = []
        self._stack: list[Recorded] = []

    @contextmanager
    def start_as_current_observation(
        self, *, name: str, as_type: str = "span"
    ) -> Iterator[Recorded]:
        node = Recorded(name=name)
        (self._stack[-1].children if self._stack else self.roots).append(node)
        self._stack.append(node)
        try:
            yield node
        except BaseException as exc:
            node.error = exc
            raise
        finally:
            node.closed = True
            self._stack.pop()

    def update_current_span(self, *, metadata: Mapping[str, Any] | None = None) -> None:
        if metadata is not None and self._stack:
            self._stack[-1].metadata.update(metadata)

    def set_current_trace_as_public(self) -> None:
        return None

    def get_current_trace_id(self) -> str | None:
        return "0" * 32 if self._stack else None

    def get_current_observation_id(self) -> str | None:
        return "0" * 16 if self._stack else None

    def flush(self) -> None:
        return None

    def all(self) -> list[Recorded]:
        found: list[Recorded] = []
        pending = list(self.roots)
        while pending:
            node = pending.pop(0)
            found.append(node)
            pending.extend(node.children)
        return found


@pytest.fixture
def client() -> Iterator[RecordingClient]:
    """Install the fake for one case and switch tracing off after it."""
    fake = RecordingClient()
    configure_tracing(client=fake)
    yield fake
    configure_tracing()


def _lookup_then_answer() -> tuple[ScriptedGateway, FakeTools]:
    gateway = ScriptedGateway(
        answer_body(None, wire_call("call_a", "list_my_requests", {})),
        answer_body(REPLY),
    )
    tools = FakeTools({"list_my_requests": {"requests": [{"request_number": "REQ-0001"}]}})
    return gateway, tools


def test_each_model_step_opens_one_model_step_span(client: RecordingClient) -> None:
    """Catches an implementation that opens no span, opens one span for the
    whole turn, opens one per tool call instead of per model step, starts the
    step count at 0, never increments it, or hangs the span outside the turn's
    root instead of under it."""
    gateway, tools = _lookup_then_answer()

    run(gateway, tools, FakeStore())

    assert len(client.roots) == 1
    root = client.roots[0]
    assert root.name == TURN_TRACE_NAME
    steps = [node for node in root.children if node.name == "model_step"]
    assert len(steps) == 2
    assert len([node for node in client.all() if node.name == "model_step"]) == 2
    assert [node.metadata["step"] for node in steps] == [1, 2]
    assert [node.metadata["model_alias"] for node in steps] == ["agent-default"] * 2
    assert all(node.closed for node in steps)


def test_a_model_step_span_records_no_message_text(client: RecordingClient) -> None:
    """Catches an implementation that puts the person's message, the model's
    reply or a tool result into a span attribute, which would copy what people
    typed into the traces."""
    gateway, tools = _lookup_then_answer()

    run(gateway, tools, FakeStore(), message=MESSAGE)

    # Premise: the step spans exist, so "no text anywhere" is not an empty claim.
    assert [node.name for node in client.all()].count("model_step") == 2
    everything = " ".join(
        f"{key}={value}" for node in client.all() for key, value in node.metadata.items()
    )
    for text in ("zebra-quartz", "marmot-felt", "REQ-0001"):
        assert text not in everything


def test_a_failed_model_step_still_closes_its_span(client: RecordingClient) -> None:
    """Catches an implementation that opens the span by hand and never closes
    it when the gateway fails, or one that swallows the error inside the span
    so the trace shows a clean step for a call that failed."""
    store = FakeStore()

    async def scenario() -> TurnOutcome:
        model = ModelClient(
            base_url=GATEWAY_URL,
            api_keys=API_KEYS,
            transport=httpx.MockTransport(lambda request: httpx.Response(500)),
        )
        try:
            state = await run_loop_turn(
                deps=_deps(model, FakeTools(), Clock()),
                tenant_id=TENANT_ID,
                user_id=USER_ID,
                session_id=SESSION_ID,
                tenant_slug=TENANT_SLUG,
                message=MESSAGE,
                store=store,
            )
        finally:
            await model.aclose()
        return state.outcome

    outcome = asyncio.run(scenario())

    assert outcome is TurnOutcome.FAILED
    steps = [node for node in client.all() if node.name == "model_step"]
    assert len(steps) == 1
    assert steps[0].closed
    assert steps[0].error is not None


def test_finished_spans_are_appended_to_the_span_log(tmp_path: Path) -> None:
    """Catches a span log that stays empty, records the wrong name, drops the
    length, or writes anything but the name, start and length."""
    log = tmp_path / "spans.jsonl"
    configure_tracing(
        settings=Settings(
            database_url="postgresql+psycopg://fake:fake@localhost:5432/fake",
            redis_url="redis://localhost:6379/0",
            qdrant_url="http://localhost:6333",
            jwt_secret="fake-secret-for-tests-only-0123456789",
            span_log_file=str(log),
        )
    )
    try:
        with span("model_step", attributes={"step": 1}):
            pass
    finally:
        configure_tracing()

    (line,) = log.read_text(encoding="utf-8").splitlines()
    item = json.loads(line)
    assert set(item) == {"name", "started_at", "duration_ms"}
    assert item["name"] == "model_step"
    assert item["duration_ms"] >= 0
