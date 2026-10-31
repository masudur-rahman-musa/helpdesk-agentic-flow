"""A spent budget shows the gateway's own message in the chat.

The model client turns a 429 with code ``BUDGET_EXCEEDED`` into
``ModelBudgetExceededError`` carrying the gateway's text; the loop ends the turn
on that text as a finished reply. Every other gateway error stays generic.

Cost: the real ``ModelClient`` over ``httpx.MockTransport`` and the fakes of
``test_agent_loop.py``. No service, no ``db`` marker, no sleep, no network.
"""

import asyncio
from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from helpdesk.agent_api.agent.events import ErrorEvent, ReplyEvent
from helpdesk.agent_api.agent.loop import FAILURE_REPLY
from helpdesk.agent_api.agent.state import TurnOutcome
from helpdesk.agent_api.services.model_gateway.client import (
    ModelAlias,
    ModelBudgetExceededError,
    ModelClient,
    ModelUnavailableError,
)
from helpdesk.shared.models.conversation import MessageRole
from tests.agent_api.test_agent_loop import (
    API_KEYS,
    GATEWAY_URL,
    MESSAGE,
    TENANT_SLUG,
    FakeStore,
    FakeTools,
    ScriptedGateway,
    answer_body,
    run,
    stream,
    wire_call,
)

ORG_MESSAGE = (
    "Your organisation has used its monthly AI budget of $15.00; "
    "ask budget-owner@example.invalid to allow more."
)
USER_MESSAGE = "You have used your own AI budget of $2.00; ask your manager to allow more."
MAX_MESSAGE_LENGTH = 1000


def budget_body(message: object = ORG_MESSAGE, *, param: str | None = None) -> dict[str, Any]:
    """The console's budget refusal body."""
    return {
        "error": {
            "message": message,
            "type": "budget_error",
            "param": param,
            "code": "BUDGET_EXCEEDED",
        }
    }


def rate_limited_body() -> dict[str, Any]:
    """The console's rate-limit refusal: also a 429, but another code."""
    return {
        "error": {
            "message": "Too many requests, slow down.",
            "type": "rate_error",
            "param": None,
            "code": "RATE_LIMITED",
        }
    }


class RefusingGateway(ScriptedGateway):
    """Answers each request with the next scripted response: a `(status, json)`
    pair, or a plain 200 body. A request past the last one fails the run."""

    def __init__(self, *responses: tuple[int, Mapping[str, Any]]) -> None:
        super().__init__()
        self._responses = list(responses)

    def handler(self, request: httpx.Request) -> httpx.Response:
        index = len(self.requests)
        self.requests.append({})
        if index >= len(self._responses):
            raise AssertionError(
                f"the turn made {len(self.requests)} model requests; "
                f"only {len(self._responses)} were scripted"
            )
        status, body = self._responses[index]
        return httpx.Response(status, json=dict(body))


def ok(text: str) -> tuple[int, Mapping[str, Any]]:
    """A normal gateway answer."""
    return 200, answer_body(text)


def refused(message: object = ORG_MESSAGE) -> tuple[int, Mapping[str, Any]]:
    """A budget refusal."""
    return 429, budget_body(message)


def call_client(response: httpx.Response) -> None:
    """One `complete` call against a gateway that always answers `response`."""

    async def scenario() -> None:
        model = ModelClient(
            base_url=GATEWAY_URL,
            api_keys=API_KEYS,
            transport=httpx.MockTransport(lambda _request: response),
        )
        try:
            await model.complete(
                [{"role": "user", "content": "hi"}],
                alias=ModelAlias.AGENT_DEFAULT,
                tenant_slug=TENANT_SLUG,
            )
        finally:
            await model.aclose()

    asyncio.run(scenario())


def budget_error_from(body: Mapping[str, Any]) -> ModelBudgetExceededError:
    """The budget error a 429 with `body` raises; fails if it raises anything else."""
    with pytest.raises(ModelBudgetExceededError) as caught:
        call_client(httpx.Response(429, json=dict(body)))
    return caught.value


# --- the client reads the 429 ------------------------------------------------


def test_a_budget_refusal_raises_the_budget_error_with_the_gateway_message() -> None:
    """Catches a client that hardcodes its own budget wording, that keeps only
    the status (so the text is lost), or that alters the message (trims inner
    text, adds a prefix) instead of keeping it exactly."""
    error = budget_error_from(budget_body(ORG_MESSAGE))

    assert error.gateway_message == ORG_MESSAGE


def test_a_per_user_budget_refusal_carries_its_own_message() -> None:
    """Catches a client that reads only the organisation form (``param`` null)
    and turns the ``X-User`` form into the generic error, or that returns one
    fixed text for both."""
    error = budget_error_from(budget_body(USER_MESSAGE, param="X-User"))

    assert error.gateway_message == USER_MESSAGE
    assert error.gateway_message != ORG_MESSAGE


def test_the_gateway_text_never_enters_the_error_log_message() -> None:
    """Catches a client that passes the gateway text to the exception
    constructor, so it lands in logs and tracebacks (it can quote a prompt)."""
    error = budget_error_from(budget_body(ORG_MESSAGE))

    assert ORG_MESSAGE not in str(error)
    assert "$15.00" not in str(error)


@pytest.mark.parametrize(
    ("sent", "kept"),
    [
        pytest.param("x" * MAX_MESSAGE_LENGTH, "x" * MAX_MESSAGE_LENGTH, id="exactly-1000"),
        pytest.param(
            "  " + "x" * MAX_MESSAGE_LENGTH + "\n ",
            "x" * MAX_MESSAGE_LENGTH,
            id="1000-after-trim-longer-before",
        ),
    ],
)
def test_a_budget_message_of_exactly_1000_characters_is_kept(sent: str, kept: str) -> None:
    """Catches an off-by-one cap (``< 1000`` refuses the limit), and a cap
    measured before trimming (padding then pushes a 1000-character message over);
    the stored text is the trimmed one."""
    error = budget_error_from(budget_body(sent))

    assert error.gateway_message == kept
    assert len(error.gateway_message) == MAX_MESSAGE_LENGTH


@pytest.mark.parametrize(
    "sent",
    [
        pytest.param("x" * (MAX_MESSAGE_LENGTH + 1), id="1001"),
        pytest.param("  " + "x" * (MAX_MESSAGE_LENGTH + 1) + " ", id="1001-after-trim-padded"),
    ],
)
def test_a_budget_message_of_1001_characters_is_the_generic_error(sent: str) -> None:
    """Catches a missing or too-loose cap (``<= 1001``, or no cap), which would
    show an unbounded gateway text to the person."""
    with pytest.raises(ModelUnavailableError) as caught:
        call_client(httpx.Response(429, json=budget_body(sent)))

    assert not isinstance(caught.value, ModelBudgetExceededError)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(budget_body(""), id="empty"),
        pytest.param(budget_body("   \n\t "), id="only-spaces"),
        pytest.param(
            {"error": {"type": "budget_error", "param": None, "code": "BUDGET_EXCEEDED"}},
            id="missing",
        ),
        pytest.param(budget_body(5), id="a-number"),
        pytest.param({"code": "BUDGET_EXCEEDED", "message": ORG_MESSAGE}, id="no-error-object"),
    ],
)
def test_an_unusable_budget_message_is_the_generic_error(body: Mapping[str, Any]) -> None:
    """Catches a client that shows an empty reply, shows a non-text value, or
    crashes with a ``KeyError``/``TypeError`` instead of falling back to
    ``ModelUnavailableError``."""
    with pytest.raises(ModelUnavailableError) as caught:
        call_client(httpx.Response(429, json=dict(body)))

    assert not isinstance(caught.value, ModelBudgetExceededError)


def test_a_rate_limited_429_is_still_the_generic_error() -> None:
    """Catches a client that treats every 429 with a message as a budget
    refusal; only the ``code`` decides. The body has a good message, so the
    message checks cannot be what refuses it."""
    with pytest.raises(ModelUnavailableError) as caught:
        call_client(httpx.Response(429, json=rate_limited_body()))

    assert not isinstance(caught.value, ModelBudgetExceededError)


# --- the loop ends the turn on the message -----------------------------------


def test_a_budget_refusal_is_the_chat_reply_verbatim() -> None:
    """Catches a loop that ends on ``FAILURE_REPLY`` or ``FAILED``, that
    rewrites the message, or that does not store it as the assistant message."""
    gateway = RefusingGateway(refused(ORG_MESSAGE))
    store = FakeStore()

    state = run(gateway, FakeTools(), store)

    assert state.outcome is TurnOutcome.DONE
    assert state.reply == ORG_MESSAGE
    assert state.error_code is None
    assert store.messages == [
        (MessageRole.USER, MESSAGE),
        (MessageRole.ASSISTANT, ORG_MESSAGE),
    ]


def test_the_stream_sends_the_budget_message_as_a_reply() -> None:
    """Catches a stream that yields an ``ErrorEvent`` for a budget refusal (the
    web would show the generic error), or yields the reply twice."""
    events = stream(RefusingGateway(refused(ORG_MESSAGE)), FakeTools(), FakeStore())

    replies = [event for event in events if isinstance(event, ReplyEvent)]
    assert len(replies) == 1
    assert replies[0].reply == ORG_MESSAGE
    assert not any(isinstance(event, ErrorEvent) for event in events)


def test_the_agent_does_not_retry_after_a_budget_refusal() -> None:
    """Catches a loop that retries the model call, or sends the turn on to a
    repair step, after the refusal (a spent budget cannot recover mid-turn)."""
    gateway = RefusingGateway(refused(ORG_MESSAGE))

    run(gateway, FakeTools(), FakeStore())

    assert len(gateway.requests) == 1


def test_a_budget_refusal_after_a_lookup_runs_no_more_tools() -> None:
    """Catches a loop that, when step two is refused, still runs queued tools
    or asks the model again; the lookup of step one stays the only tool call."""
    payload: dict[str, Any] = {"requests": []}
    gateway = RefusingGateway(
        (200, answer_body(None, wire_call("call_a", "list_my_requests", {}))),
        refused(ORG_MESSAGE),
    )
    tools = FakeTools({"list_my_requests": payload})

    state = run(gateway, tools, FakeStore())

    assert tools.names == ["list_my_requests"]
    assert len(gateway.requests) == 2
    assert state.outcome is TurnOutcome.DONE
    assert state.reply == ORG_MESSAGE


def test_the_chat_stays_usable_after_a_budget_refusal() -> None:
    """Catches a refusal that leaves state behind (a held flag, a poisoned
    session, a cached error) so the next turn on the same chat cannot answer."""
    store = FakeStore()
    refused_state = run(RefusingGateway(refused(ORG_MESSAGE)), FakeTools(), store)
    gateway = RefusingGateway(ok("Here is your answer."))

    state = run(gateway, FakeTools(), store, message="Try again please.")

    assert refused_state.reply == ORG_MESSAGE
    assert state.outcome is TurnOutcome.DONE
    assert state.reply == "Here is your answer."
    assert len(gateway.requests) == 1


@pytest.mark.parametrize(
    "response",
    [
        pytest.param((503, {"error": "down"}), id="503"),
    ],
)
def test_any_other_gateway_error_still_shows_the_generic_error(
    response: tuple[int, Mapping[str, Any]],
) -> None:
    """Catches a loop that shows any gateway error text as a finished reply;
    only a budget refusal may. Both paths: the run ends
    ``FAILED`` on ``FAILURE_REPLY`` and the stream yields an ``ErrorEvent``."""
    state = run(RefusingGateway(response), FakeTools(), FakeStore())
    events = stream(RefusingGateway(response), FakeTools(), FakeStore())

    assert state.outcome is TurnOutcome.FAILED
    assert state.reply == FAILURE_REPLY
    assert any(isinstance(event, ErrorEvent) for event in events)
    assert not any(isinstance(event, ReplyEvent) for event in events)
