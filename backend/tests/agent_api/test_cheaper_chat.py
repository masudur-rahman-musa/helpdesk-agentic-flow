"""The chat model and the order of the messages in each request.

Two plain choices in the agent loop:

* every step asks the gateway for ``agent-default``;
* the request draft is one system message placed just before the person's
  newest message, so the prompt and the earlier history stay the same from
  step to step and from turn to turn.

Cost: the loop is driven with the fakes of ``test_agent_loop.py`` (scripted
gateway, fake tools, in-memory chat row). No service, no ``db`` marker, no
sleep, no network, and no call to a real model.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from helpdesk.agent_api.agent.draft import draft_message
from helpdesk.agent_api.agent.prompts import load_prompt
from helpdesk.agent_api.agent.state import RequestDraft
from helpdesk.shared.models.conversation import MessageRole
from tests.agent_api.test_agent_loop import (
    START,
    FakeStore,
    FakeTools,
    ScriptedGateway,
    answer_body,
    held_question,
    reading_body,
    run,
    wire_call,
)

DRAFT_PREFIX = "Request so far"
CHAT_ALIAS = "agent-default"
HISTORY: list[tuple[str, str]] = [("user", "Hi"), ("assistant", "Hello")]


class GrowingStore(FakeStore):
    """A chat row whose history grows with each saved message, so a second turn
    on the same row sees the first turn as history."""

    async def add_message(self, role: MessageRole, content: str) -> None:
        await super().add_message(role, content)
        self._history.append((role.value, content))


def _prompt() -> dict[str, str]:
    return {"role": "system", "content": load_prompt("agent_loop").text}


def _history_messages(history: Sequence[tuple[str, str]]) -> list[dict[str, str]]:
    return [{"role": role, "content": content} for role, content in history]


def _draft_index(request_body: Mapping[str, Any]) -> int:
    """Where the draft message sits in a request; exactly one is expected."""
    found = [
        index
        for index, message in enumerate(request_body["messages"])
        if message["role"] == "system" and message["content"].startswith(DRAFT_PREFIX)
    ]
    assert len(found) == 1, f"{len(found)} draft messages"
    return found[0]


def _roles(request_body: Mapping[str, Any]) -> list[str]:
    return [message["role"] for message in request_body["messages"]]


def test_every_step_asks_agent_default() -> None:
    """Catches a loop that still sends some or all steps to `agent-strong`, or
    that switches alias between the lookup step and the answer step.

    A lookup step and then an answer step: both requests must name the cheap
    model, and no request may name the strong one.
    """
    gateway = ScriptedGateway(
        answer_body(None, wire_call("call_1", "list_my_tickets", {})),
        answer_body("You have no tickets."),
    )
    tools = FakeTools({"list_my_tickets": {"tickets": []}})

    run(gateway, tools, FakeStore())

    assert [request["model"] for request in gateway.requests] == [CHAT_ALIAS, CHAT_ALIAS]
    assert all("agent-strong" not in str(request) for request in gateway.requests)


def test_the_last_step_with_no_tools_asks_agent_default() -> None:
    """Catches a loop that sets the cheap alias on the tool-offering steps but
    leaves the last, tool-free request on `agent-strong`.

    Three lookup steps (below the six-call cap), so the fourth step is the
    last and goes out answer-only; it must still name the cheap model.
    """
    gateway = ScriptedGateway(
        answer_body(None, wire_call("call_1", "list_my_tickets", {})),
        answer_body(None, wire_call("call_2", "list_my_tickets", {})),
        answer_body(None, wire_call("call_3", "list_my_tickets", {})),
        answer_body("Here is what I found."),
    )
    tools = FakeTools({"list_my_tickets": {"tickets": []}})

    run(gateway, tools, FakeStore())

    assert len(gateway.requests) == 4
    assert gateway.requests[3]["tools"]
    assert gateway.requests[3]["tool_choice"] == "none"
    assert gateway.requests[3]["model"] == CHAT_ALIAS


def test_the_draft_goes_after_the_history() -> None:
    """Catches a loop that keeps the draft right after the prompt (the start of
    the request then changes whenever the draft does), or that puts it after
    the person's message.

    With a two-message history and a saved draft, the first request must read
    prompt, history, draft, then the person's message.
    """
    draft = RequestDraft(role="wiki viewer", days=7, saved_at=START)
    gateway = ScriptedGateway(answer_body("Welcome back."))
    store = FakeStore(history=HISTORY, draft=draft)

    run(gateway, FakeTools(), store, message="I am back.")

    messages = gateway.requests[0]["messages"]
    assert _roles(gateway.requests[0]) == ["system", "user", "assistant", "system", "user"]
    assert messages[0] == _prompt()
    assert messages[3]["content"] == draft_message(draft)
    assert messages[4] == {"role": "user", "content": "I am back."}


def test_the_start_stays_the_same_when_the_draft_changes() -> None:
    """Catches a loop where the part of the request before the draft moves with
    the draft (for example the draft sits first, or inside the history), or
    where more than the draft message differs between steps.

    A three-step turn: a note changes the draft, a lookup follows, then the
    answer. In every request the messages before the draft are the prompt and
    the stored history exactly, and the draft differs between step 1 and 2.
    """
    draft = RequestDraft(role="wiki viewer", saved_at=START)
    gateway = ScriptedGateway(
        answer_body(None, wire_call("call_n", "note_request", {"person": "Dana Fake"})),
        answer_body(None, wire_call("call_1", "list_my_tickets", {})),
        answer_body("Thanks."),
    )
    tools = FakeTools({"list_my_tickets": {"tickets": []}})
    store = FakeStore(history=HISTORY, draft=draft)

    run(gateway, tools, store)

    expected_start = [_prompt(), *_history_messages(HISTORY)]
    drafts = []
    for request in gateway.requests:
        index = _draft_index(request)
        assert request["messages"][:index] == expected_start
        drafts.append(request["messages"][index]["content"])
    assert drafts[0] == draft_message(draft)
    assert drafts[1] != drafts[0]
    assert '"person": "Dana Fake"' in drafts[1] or '"person":"Dana Fake"' in drafts[1]
    assert drafts[2] == drafts[1]


def test_the_next_turn_keeps_the_same_start() -> None:
    """Catches a loop that leaves the draft inside the part of the request that
    the next turn must repeat (the prompt and history), so that turn 2 no
    longer begins with what turn 1 began with.

    Two turns on one chat row with a saved draft. Turn 2's first request must
    begin with every message turn 1's first request had apart from its draft:
    the prompt, the history and turn 1's own message.
    """
    draft = RequestDraft(role="wiki viewer", saved_at=START)
    gateway = ScriptedGateway(answer_body("First answer."), answer_body("Second answer."))
    store = GrowingStore(history=HISTORY, draft=draft)

    run(gateway, FakeTools(), store, message="First message.")
    run(gateway, FakeTools(), store, message="Second message.")

    first, second = gateway.requests
    first_index = _draft_index(first)
    assert first_index == 1 + len(HISTORY)
    without_draft = [m for i, m in enumerate(first["messages"]) if i != first_index]
    assert without_draft[-1] == {"role": "user", "content": "First message."}
    assert second["messages"][: len(without_draft)] == without_draft
    assert _draft_index(second) == len(without_draft) + 1


def test_a_denial_sits_before_the_draft() -> None:
    """Catches a loop that inserts the draft before the denied calls (so the
    draft sits among the history) or after the person's "no".

    A bare "no" to a held change with a saved draft: the request must end with
    the denied calls, their denial, the draft, then the person's "no".
    """
    draft = RequestDraft(role="wiki viewer", days=7, saved_at=START)
    gateway = ScriptedGateway(
        reading_body("no"), answer_body("Understood. What would you like instead?")
    )
    store = FakeStore(question=held_question(), draft=draft)

    run(gateway, FakeTools(), store, message="no")

    request = gateway.requests[1]
    messages = request["messages"]
    assert _roles(request)[-4:] == ["assistant", "tool", "system", "user"]
    assert messages[-2]["content"] == draft_message(draft)
    assert messages[-1] == {"role": "user", "content": "no"}


def test_no_draft_sends_no_draft_message() -> None:
    """Unhappy path. Catches a loop that sends an empty or placeholder draft
    message when there is no draft.

    With no saved draft, no later system message starts with the draft text and
    the request is the prompt, the history and the person's message only.
    """
    gateway = ScriptedGateway(answer_body("Hello again."))
    store = FakeStore(history=HISTORY)

    run(gateway, FakeTools(), store, message="Hi again.")

    messages = gateway.requests[0]["messages"]
    assert not any(
        m["role"] == "system" and m["content"].startswith(DRAFT_PREFIX) for m in messages[1:]
    )
    assert messages == [
        _prompt(),
        *_history_messages(HISTORY),
        {"role": "user", "content": "Hi again."},
    ]
