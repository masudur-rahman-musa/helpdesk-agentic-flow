"""A busy model gateway ends the turn on a calm try-again reply.

The model client turns a 429 that is not a budget refusal into
``ModelBusyError``; the loop ends the turn on the fixed ``BUSY_REPLY`` with the
outcome ``busy``. It is not a refusal and not a failure: no error code, no
refusal count, no retry, no more tool calls. A 503 still fails the turn.

Cost: the real ``ModelClient`` over ``httpx.MockTransport`` and the fakes of
``test_agent_loop.py``. No service, no ``db`` marker, no sleep, no network.
"""

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from prometheus_client import CollectorRegistry

from helpdesk.agent_api.agent.events import ErrorEvent, ReplyEvent
from helpdesk.agent_api.agent.loop import BUSY_REPLY, FAILURE_REPLY
from helpdesk.agent_api.agent.state import TurnOutcome
from helpdesk.agent_api.services.model_gateway.client import (
    ModelBudgetExceededError,
    ModelBusyError,
    ModelUnavailableError,
)
from helpdesk.shared.core.errors import ErrorCode
from helpdesk.shared.core.observability import metrics
from helpdesk.shared.models.conversation import MessageRole
from tests.agent_api.test_agent_loop import (
    MESSAGE,
    TENANT_ID,
    FakeStore,
    FakeTools,
    answer_body,
    run,
    stream,
    wire_call,
)
from tests.agent_api.test_budget_refusal import (
    ORG_MESSAGE,
    RefusingGateway,
    budget_body,
    call_client,
    ok,
    rate_limited_body,
    refused,
)

GATEWAY_TEXT = "Too many requests, slow down."
BUSY = (429, rate_limited_body())
ENDINGS = "helpdesk_turn_endings_total"
REFUSALS = "helpdesk_refusals_total"


@pytest.fixture
def fresh_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[CollectorRegistry]:
    """A registry of this case's own, installed over the module-level pair."""
    registry = CollectorRegistry()
    monkeypatch.setattr(metrics, "REGISTRY", registry)
    monkeypatch.setattr(metrics, "METRICS", metrics.build_metrics(registry))
    yield registry


# --- the client reads the 429 ------------------------------------------------


def test_a_rate_limited_429_raises_the_busy_error() -> None:
    """Catches a client that keeps raising the plain unavailable error for a
    non-budget 429 (the loop cannot tell it from a broken gateway), or that
    mistakes it for a budget refusal; the code stays ``INTERNAL``."""
    with pytest.raises(ModelBusyError) as caught:
        call_client(httpx.Response(429, json=rate_limited_body()))

    assert not isinstance(caught.value, ModelBudgetExceededError)
    assert isinstance(caught.value, ModelUnavailableError)
    assert caught.value.code is ErrorCode.INTERNAL


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(httpx.Response(429, text="slow down"), id="plain-text-body"),
        pytest.param(httpx.Response(429, json=budget_body("")), id="budget-code-empty-message"),
    ],
)
def test_a_429_with_no_readable_body_is_busy_too(response: httpx.Response) -> None:
    """Catches a client that only calls a 429 busy when the body parses and
    names ``RATE_LIMITED`` (a text body or an unusable budget message would then
    stay a 500), or that crashes on a body that is not JSON."""
    with pytest.raises(ModelBusyError):
        call_client(response)


def test_a_budget_refusal_is_still_the_budget_error() -> None:
    """Catches a client that turns every 429 busy, which would hide the budget
    message from the person; the budget error must not be a busy error."""
    with pytest.raises(ModelBudgetExceededError) as caught:
        call_client(httpx.Response(429, json=budget_body(ORG_MESSAGE)))

    assert not isinstance(caught.value, ModelBusyError)
    assert caught.value.gateway_message == ORG_MESSAGE


# --- the loop ends the turn on the busy reply --------------------------------


def test_a_busy_gateway_ends_the_turn_on_the_busy_reply() -> None:
    """Catches a loop that still ends ``FAILED`` on the failure sentence, that
    ends ``DONE`` instead of ``busy``, that sets an error code, or that does not
    store the reply as the assistant message."""
    store = FakeStore()

    state = run(RefusingGateway(BUSY), FakeTools(), store)

    assert TurnOutcome.BUSY.value == "busy"
    assert state.outcome is TurnOutcome.BUSY
    assert state.reply == BUSY_REPLY
    assert state.error_code is None
    assert store.messages == [
        (MessageRole.USER, MESSAGE),
        (MessageRole.ASSISTANT, BUSY_REPLY),
    ]


def test_the_gateway_429_text_never_reaches_the_reply() -> None:
    """Catches a loop that shows the gateway's own 429 wording (written for an
    operator, not the person) in the reply, the stream or the stored chat."""
    store = FakeStore()
    gateway_body = rate_limited_body()
    assert gateway_body["error"]["message"] == GATEWAY_TEXT

    state = run(RefusingGateway(BUSY), FakeTools(), store)
    events = stream(RefusingGateway(BUSY), FakeTools(), FakeStore())

    assert state.reply is not None
    assert GATEWAY_TEXT not in state.reply
    assert all(GATEWAY_TEXT not in content for _role, content in store.messages)
    assert all(GATEWAY_TEXT not in repr(event) for event in events)


def test_the_stream_sends_the_busy_reply_and_no_error() -> None:
    """Catches a stream that yields an ``ErrorEvent`` for a busy gateway (the
    web would show the generic error), yields the reply twice, or labels the
    reply with another outcome."""
    events = stream(RefusingGateway(BUSY), FakeTools(), FakeStore())

    replies = [event for event in events if isinstance(event, ReplyEvent)]
    assert len(replies) == 1
    assert replies[0].reply == BUSY_REPLY
    assert replies[0].reply_data["outcome"] == "busy"
    assert not any(isinstance(event, ErrorEvent) for event in events)


def test_the_agent_does_not_retry_after_a_busy_429() -> None:
    """Catches a loop that retries the model call after a 429 (it holds the
    request open and adds load to a gateway that asked for less)."""
    gateway = RefusingGateway(BUSY)

    run(gateway, FakeTools(), FakeStore())

    assert len(gateway.requests) == 1


def test_a_busy_429_after_a_lookup_runs_no_more_tools() -> None:
    """Catches a loop that, when step two is busy, still runs queued tools or
    asks the model again; the lookup of step one stays the only tool call."""
    payload: dict[str, Any] = {"requests": []}
    gateway = RefusingGateway(
        (200, answer_body(None, wire_call("call_a", "list_my_requests", {}))),
        BUSY,
    )
    tools = FakeTools({"list_my_requests": payload})

    state = run(gateway, tools, FakeStore())

    assert tools.names == ["list_my_requests"]
    assert len(gateway.requests) == 2
    assert state.outcome is TurnOutcome.BUSY
    assert state.reply == BUSY_REPLY


def test_the_chat_stays_usable_after_a_busy_429() -> None:
    """Catches a busy turn that leaves state behind (a held flag, a poisoned
    chat row) so the next message in the same chat cannot get an answer."""
    store = FakeStore()
    busy_state = run(RefusingGateway(BUSY), FakeTools(), store)
    gateway = RefusingGateway(ok("Here is your answer."))

    state = run(gateway, FakeTools(), store, message="Try again please.")

    assert busy_state.reply == BUSY_REPLY
    assert state.outcome is TurnOutcome.DONE
    assert state.reply == "Here is your answer."
    assert len(gateway.requests) == 1


# --- the metrics -------------------------------------------------------------


def _ending(registry: CollectorRegistry, outcome: str) -> float | None:
    return registry.get_sample_value(ENDINGS, {"tenant": str(TENANT_ID), "outcome": outcome})


def _total(registry: CollectorRegistry, name: str) -> float:
    return sum(
        sample.value
        for family in registry.collect()
        for sample in family.samples
        if sample.name == name
    )


def test_a_busy_turn_counts_its_own_ending_and_no_refusal(
    fresh_registry: CollectorRegistry,
) -> None:
    """Catches a busy turn counted as ``failed`` (the busy gateway would read as
    an outage), counted as a refusal, or not counted under its own ``busy``
    ending at all."""
    run(RefusingGateway(BUSY), FakeTools(), FakeStore())

    assert _ending(fresh_registry, "busy") == 1.0
    assert _ending(fresh_registry, "failed") in (None, 0.0)
    assert _total(fresh_registry, ENDINGS) == 1.0
    assert _total(fresh_registry, REFUSALS) == 0.0


# --- only a 429 is calm ------------------------------------------------------


def test_a_503_still_fails_the_turn() -> None:
    """Catches a loop that treats every gateway error as busy; a real outage must
    still end ``FAILED`` on the failure sentence with an ``ErrorEvent``."""
    response = (503, {"error": "down"})

    state = run(RefusingGateway(response), FakeTools(), FakeStore())
    events = stream(RefusingGateway(response), FakeTools(), FakeStore())

    assert state.outcome is TurnOutcome.FAILED
    assert state.reply == FAILURE_REPLY
    assert any(isinstance(event, ErrorEvent) for event in events)
    assert not any(isinstance(event, ReplyEvent) for event in events)


def test_a_budget_refusal_is_not_a_busy_turn() -> None:
    """Catches a loop that sends a budget refusal down the busy path: the person
    must still read the gateway's budget message, with outcome ``DONE``."""
    state = run(RefusingGateway(refused(ORG_MESSAGE)), FakeTools(), FakeStore())

    assert state.outcome is TurnOutcome.DONE
    assert state.reply == ORG_MESSAGE
