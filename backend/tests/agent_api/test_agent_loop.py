"""The tool-calling agent loop, its lookups and its stream.

Expected implementation surface:

``helpdesk.agent_api.agent.loop``
    ``MAX_STEPS = 4``, ``TIMEOUT_REPLY``, ``CHANGE_FAILED_REPLY``,
    ``LoopStore`` (a protocol), ``run_loop_turn(...)`` and
    ``stream_loop_turn(...)``; both take ``store=`` so a fake can stand in for
    the chat row.
``helpdesk.agent_api.agent.loop_tools``
    ``offered_tools(principals)``, ``find_role(words, *, tools, directory)``,
    ``find_person(words, *, directory)`` and ``PersonRow``.

The gateway is a real ``ModelClient`` over ``httpx.MockTransport`` that plays
scripted OpenAI-shaped answers and records every request body. The tool server
is a fake that plays scripted payloads and records every call. The chat row is
an in-memory store. The clock is a plain object the test moves. No socket, no
database, no real waiting. Each case drives one ``asyncio.run``.
"""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.agent_api.agent.confirmation import NOTHING_FILED_REPLY, OpenQuestion
from helpdesk.agent_api.agent.events import ReplyEvent, StepEvent, TurnEvent
from helpdesk.agent_api.agent.gates import MAX_TOOL_CALLS, TURN_DEADLINE_SECONDS
from helpdesk.agent_api.agent.loop import (
    CHANGE_FAILED_REPLY,
    MAX_STEPS,
    TIMEOUT_REPLY,
    run_loop_turn,
    stream_loop_turn,
)
from helpdesk.agent_api.agent.loop_tools import (
    PersonRow,
    find_person,
    find_role,
    offered_tools,
)
from helpdesk.agent_api.agent.postcheck import outcome_reply
from helpdesk.agent_api.agent.prompts import load_prompt
from helpdesk.agent_api.agent.state import (
    Intent,
    IntentEntities,
    IntentExtraction,
    PostCheckOutcome,
    ResolvedEntities,
    ToolResult,
    TurnOutcome,
    TurnState,
    ValidatedCall,
)
from helpdesk.agent_api.agent.tool_args import CreateAccessRequestArgs
from helpdesk.agent_api.agent.turn_deps import TurnDeps
from helpdesk.agent_api.services.mcp_client import ToolCaller
from helpdesk.agent_api.services.model_gateway.client import ModelClient
from helpdesk.shared.core.errors import ToolError
from helpdesk.shared.models.conversation import MessageRole
from helpdesk.shared.models.idempotency import KEY_LENGTH

GATEWAY_URL = "http://gateway.invalid:8080"
TENANT_SLUG = "nordlicht"
# Obviously fake, tests only: this is not a credential.
API_KEYS: dict[str, str] = {TENANT_SLUG: "fake-key-nordlicht"}

START = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
TENANT_ID = uuid.UUID("a1111111-1111-5111-8111-111111111111")
USER_ID = uuid.UUID("a2222222-2222-5222-8222-222222222222")
SESSION_ID = uuid.UUID("a3333333-3333-5333-8333-333333333333")
ROLE_ID = uuid.UUID("a4444444-4444-5444-8444-444444444444")
REQUEST_ID = uuid.UUID("a5555555-5555-5555-8555-555555555555")

MESSAGE = "What requests do I have open?"

WIKI_VIEWER_ID = uuid.UUID(int=101)
WIKI_EDITOR_ID = uuid.UUID(int=102)
VPN_USER_ID = uuid.UUID(int=103)
MAIL_USER_ID = uuid.UUID(int=104)
LEDGER_AUDITOR_ID = uuid.UUID(int=105)
ROLE_IDS: dict[tuple[str, str], uuid.UUID] = {
    ("wiki", "viewer"): WIKI_VIEWER_ID,
    ("wiki", "editor"): WIKI_EDITOR_ID,
    ("vpn", "user"): VPN_USER_ID,
    ("mail", "user"): MAIL_USER_ID,
    ("payroll", "ledger-auditor"): LEDGER_AUDITOR_ID,
}

# --- the gateway: scripted OpenAI-shaped answers ------------------------------


def answer_body(text: str | None, *calls: Mapping[str, Any], tokens: int = 2) -> dict[str, Any]:
    """One gateway answer: text, or tool calls, with a usage block."""
    message: dict[str, Any] = {"role": "assistant", "content": text}
    if calls:
        message["tool_calls"] = list(calls)
    return {
        "choices": [{"message": message}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": tokens},
    }


def wire_call(call_id: str, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """One tool call as the gateway sends it, the arguments as a JSON string."""
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(dict(arguments))},
    }


class ScriptedGateway:
    """Answers in order and keeps each request body. A request past the last
    scripted answer fails the run, which is how "no more model calls" is shown."""

    def __init__(self, *bodies: Mapping[str, Any]) -> None:
        self._bodies = list(bodies)
        self.requests: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        index = len(self.requests) - 1
        if index >= len(self._bodies):
            raise AssertionError(
                f"the turn made {len(self.requests)} model requests; "
                f"only {len(self._bodies)} were scripted"
            )
        return httpx.Response(200, json=dict(self._bodies[index]))


# --- the tool server, the chat row and the clock ------------------------------


class FakeTools:
    """A tool caller playing one scripted payload per tool name, recording calls."""

    def __init__(
        self,
        answers: Mapping[str, Mapping[str, Any]] | None = None,
        failures: Mapping[str, ToolError] | None = None,
    ) -> None:
        self._answers = dict(answers or {})
        self._failures = dict(failures or {})
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> ToolCaller:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def call_tool(self, name: str, arguments: BaseModel) -> Mapping[str, Any]:
        self.calls.append((name, arguments.model_dump(mode="json")))
        if name in self._failures:
            raise self._failures[name]
        if name not in self._answers:
            raise AssertionError(f"the turn called an unscripted tool: {name}")
        return self._answers[name]

    @property
    def names(self) -> list[str]:
        return [name for name, _arguments in self.calls]


class FakeStore:
    """The chat row in memory. `on_user_message` runs when the user's message is
    saved, which is how a test moves the clock between the start of a turn and
    its first model call."""

    def __init__(
        self,
        *,
        principals: Sequence[str] = ("employee",),
        history: Sequence[tuple[str, str]] = (),
        people: Sequence[PersonRow] = (),
        on_user_message: Callable[[], None] | None = None,
        question: OpenQuestion | None = None,
    ) -> None:
        self._principals = tuple(principals)
        self._history = list(history)
        self._people = list(people)
        self._on_user_message = on_user_message
        self.prepared: list[datetime] = []
        self.held: list[OpenQuestion] = []
        self.messages: list[tuple[MessageRole, str]] = []
        self.question = question
        """The held change on the chat row; `None` once it is cleared."""
        self.cleared = 0

    async def prepare(self, now: datetime) -> None:
        self.prepared.append(now)

    async def principals(self) -> tuple[str, ...]:
        return self._principals

    async def history(self) -> list[tuple[str, str]]:
        return list(self._history)

    async def open_question(self) -> OpenQuestion | None:
        return self.question

    async def clear_open_question(self) -> None:
        self.question = None
        self.cleared += 1

    async def hold(self, question: OpenQuestion) -> None:
        self.held.append(question)

    async def add_message(self, role: MessageRole, content: str) -> None:
        self.messages.append((role, content))
        if role is MessageRole.USER and self._on_user_message is not None:
            self._on_user_message()

    async def role_ids(self) -> Mapping[tuple[str, str], uuid.UUID]:
        return ROLE_IDS

    async def people(self) -> Sequence[PersonRow]:
        return self._people


class Clock:
    """A clock the test moves; nothing here ever waits."""

    def __init__(self) -> None:
        self.at = START

    def __call__(self) -> datetime:
        return self.at

    def advance(self, by: timedelta) -> None:
        self.at = self.at + by


# --- running one turn -----------------------------------------------------------


def _deps(model: ModelClient, tools: FakeTools, clock: Clock) -> TurnDeps:
    return TurnDeps(db=cast(AsyncSession, None), model=model, tools=tools, now=clock)


def run(
    gateway: ScriptedGateway,
    tools: FakeTools,
    store: FakeStore,
    *,
    message: str = MESSAGE,
    clock: Clock | None = None,
) -> TurnState:
    """One whole loop turn over the fakes."""
    moving = clock or Clock()

    async def scenario() -> TurnState:
        model = ModelClient(
            base_url=GATEWAY_URL,
            api_keys=API_KEYS,
            transport=httpx.MockTransport(gateway.handler),
        )
        try:
            return await run_loop_turn(
                deps=_deps(model, tools, moving),
                tenant_id=TENANT_ID,
                user_id=USER_ID,
                session_id=SESSION_ID,
                tenant_slug=TENANT_SLUG,
                message=message,
                store=store,
            )
        finally:
            await model.aclose()

    return asyncio.run(scenario())


def stream(
    gateway: ScriptedGateway, tools: FakeTools, store: FakeStore, *, message: str = MESSAGE
) -> list[TurnEvent]:
    """Every event of one streamed loop turn, in order."""

    async def scenario() -> list[TurnEvent]:
        model = ModelClient(
            base_url=GATEWAY_URL,
            api_keys=API_KEYS,
            transport=httpx.MockTransport(gateway.handler),
        )
        try:
            events: AsyncIterator[TurnEvent] = stream_loop_turn(
                deps=_deps(model, tools, Clock()),
                tenant_id=TENANT_ID,
                user_id=USER_ID,
                session_id=SESSION_ID,
                tenant_slug=TENANT_SLUG,
                message=message,
                store=store,
            )
            return [event async for event in events]
        finally:
            await model.aclose()

    return asyncio.run(scenario())


def tool_names(request_body: Mapping[str, Any]) -> list[str]:
    """The names of the tools one request offered."""
    return [entry["function"]["name"] for entry in request_body.get("tools", [])]


def tool_turns(request_body: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The `tool` turns one request carried, in order."""
    return [turn for turn in request_body["messages"] if turn["role"] == "tool"]


def role_row(
    system_slug: str, system_name: str, role_slug: str, *, held: bool = False
) -> dict[str, Any]:
    """One `list_requestable_roles` row as the tool server shapes it."""
    return {
        "system_slug": system_slug,
        "system_name": system_name,
        "system_sensitivity": "low",
        "role_slug": role_slug,
        "role_name": f"{system_slug}.{role_slug}",
        "privilege": "read",
        "default_expiry_days": 90,
        "in_my_template": False,
        "already_held": held,
    }


def suggestion(system_slug: str, system_name: str, role_slug: str, score: float) -> dict[str, Any]:
    """One `suggest_roles` row."""
    return {
        "system_slug": system_slug,
        "system_name": system_name,
        "role_slug": role_slug,
        "role_name": f"{system_slug}.{role_slug}",
        "score": score,
    }


# --- the history and the plain answer -------------------------------------------


def test_history_goes_as_real_roles() -> None:
    """Catches a loop that folds the earlier messages into one big text (the
    model then cannot tell who said what), that drops the prompt, that puts the
    new message before the history, or that sends the new message twice
    because it also sits in the stored history.

    The first request must be the prompt as one `system` message, the earlier
    messages as `user` and `assistant` turns in the stored order, then the new
    message last, all on the strong alias.
    """
    gateway = ScriptedGateway(answer_body("Here is the answer."))
    store = FakeStore(
        history=[
            ("user", "Hi, I am new here."),
            ("assistant", "Welcome. What do you need?"),
            ("user", "Something about the wiki."),
        ]
    )

    run(gateway, FakeTools(), store, message="The viewer one, please.")

    first = gateway.requests[0]
    assert first["model"] == "agent-strong"
    assert [(turn["role"], turn["content"]) for turn in first["messages"]] == [
        ("system", load_prompt("agent_loop").text),
        ("user", "Hi, I am new here."),
        ("assistant", "Welcome. What do you need?"),
        ("user", "Something about the wiki."),
        ("user", "The viewer one, please."),
    ]


def test_answer_without_tools_ends_in_one_step() -> None:
    """Catches a loop that always runs a tool or a second model step before it
    answers, that forgets to save the user's message and the reply, or that
    ends the turn on anything but the plain done outcome.

    One scripted answer is all the gateway holds, so a second request fails the
    run; the tool server holds nothing, so any call fails it too.
    """
    gateway = ScriptedGateway(answer_body("You have nothing open right now."))
    tools = FakeTools()
    store = FakeStore()

    state = run(gateway, tools, store)

    assert state.reply == "You have nothing open right now."
    assert state.outcome is TurnOutcome.DONE
    assert len(gateway.requests) == 1
    assert tools.calls == []
    assert state.tool_results == ()
    assert store.messages == [
        (MessageRole.USER, MESSAGE),
        (MessageRole.ASSISTANT, "You have nothing open right now."),
    ]


def test_lookup_then_answer() -> None:
    """Catches a loop that runs the lookup but never gives its result back to
    the model, that sends the result under another role or another call id,
    that calls the tool with arguments not shaped by its input model (the
    defaults would be missing), or that leaves the result off the state.

    The second request must end with the assistant's call and then a `tool`
    turn for the same id whose text is the tool's payload.
    """
    payload = {"requests": [{"request_number": "REQ-0001", "status": "rejected"}]}
    gateway = ScriptedGateway(
        answer_body(None, wire_call("call_a", "list_my_requests", {})),
        answer_body("You have one request, REQ-0001, and it was turned down."),
    )
    tools = FakeTools({"list_my_requests": payload})

    state = run(gateway, tools, FakeStore())

    assert state.reply == "You have one request, REQ-0001, and it was turned down."
    assert tools.calls == [("list_my_requests", {"status": None, "limit": 50})]
    assert state.tool_results == (ToolResult(tool="list_my_requests", ok=True, payload=payload),)
    second = gateway.requests[1]["messages"]
    assert second[-2]["role"] == "assistant"
    assert second[-2]["tool_calls"][0]["id"] == "call_a"
    assert second[-2]["tool_calls"][0]["function"]["name"] == "list_my_requests"
    assert second[-1]["role"] == "tool"
    assert second[-1]["tool_call_id"] == "call_a"
    assert json.loads(second[-1]["content"]) == payload


# --- the tools offered ----------------------------------------------------------


def test_only_role_allowed_tools_are_offered() -> None:
    """Catches a loop that offers every tool to everyone (an employee would be
    shown the approvals listing and the person finder), that reads the
    principals from the message instead of the chat row, or that sends tool
    definitions in some other shape than the function wire form.

    An employee and a manager each run one turn. The offered lists must equal
    what `offered_tools` gives for their principals.
    """
    employee_gateway = ScriptedGateway(answer_body("Fine."))
    manager_gateway = ScriptedGateway(answer_body("Fine."))

    run(employee_gateway, FakeTools(), FakeStore(principals=["employee"]))
    run(manager_gateway, FakeTools(), FakeStore(principals=["manager"]))

    employee_request = employee_gateway.requests[0]
    manager_request = manager_gateway.requests[0]
    assert "list_pending_approvals" not in tool_names(employee_request)
    assert "find_person" not in tool_names(employee_request)
    assert "list_my_requests" in tool_names(employee_request)
    assert "find_role" in tool_names(employee_request)
    assert "list_pending_approvals" in tool_names(manager_request)
    assert employee_request["tools"] == [t.wire() for t in offered_tools(["employee"])]
    assert manager_request["tools"] == [t.wire() for t in offered_tools(["manager"])]
    assert employee_request["tool_choice"] == "auto"


def test_a_tool_not_offered_is_refused() -> None:
    """Unhappy path. Catches a loop that runs any tool the model names, even
    one the person's role was never offered (the tool server's own check would
    be the only guard left), or that treats the call as a held change.

    An employee's model names `approve_request`. Nothing may run or be held, and
    the model must get an error `tool` turn for that call id and another step.
    """
    arguments = {"request_id": str(uuid.UUID(int=7))}
    gateway = ScriptedGateway(
        answer_body(None, wire_call("call_x", "approve_request", arguments)),
        answer_body("I cannot do that for you."),
    )
    tools = FakeTools()
    store = FakeStore(principals=["employee"])

    state = run(gateway, tools, store, message="Remove that grant.")

    assert tools.calls == []
    assert store.held == []
    assert state.outcome is not TurnOutcome.CONFIRM
    assert not any(result.tool == "approve_request" and result.ok for result in state.tool_results)
    assert state.reply == "I cannot do that for you."
    refused = tool_turns(gateway.requests[1])
    assert [turn["tool_call_id"] for turn in refused] == ["call_x"]
    assert "error" in refused[0]["content"].lower()


# --- find_role and find_person --------------------------------------------------


def test_find_role_matches_a_typo() -> None:
    """Catches a finder that needs the exact name (the typo finds nothing), that
    always asks the suggestion tool as well, that returns the stored name
    (`wiki.viewer`) instead of the plain one, that takes `already_held` from
    anywhere but the listed row, or that answers an id the directory does not
    hold.

    The viewer row is marked held, so a finder that fills a constant fails.
    """
    tools = FakeTools(
        {
            "list_requestable_roles": {
                "roles": [
                    role_row("wiki", "Internal Wiki", "viewer", held=True),
                    role_row("wiki", "Internal Wiki", "editor"),
                    role_row("vpn", "VPN", "user"),
                ]
            }
        }
    )

    rows = asyncio.run(find_role("wikki viewer", tools=tools, directory=FakeStore()))

    assert rows == [
        {
            "role_id": str(WIKI_VIEWER_ID),
            "name": "Internal Wiki viewer",
            "system": "Internal Wiki",
            "already_held": True,
        }
    ]
    assert tools.calls == [("list_requestable_roles", {"system": None, "limit": 200})]


def test_find_role_falls_back_to_suggestions() -> None:
    """Catches a finder that gives up when no name matches (the person gets
    nothing), that returns every suggestion instead of at most three, that
    reorders the suggestions, or that never asks the suggestion tool with the
    typed words.

    Four suggestions come back; the first three must be answered in order.
    """
    tools = FakeTools(
        {
            "list_requestable_roles": {
                "roles": [
                    role_row("wiki", "Internal Wiki", "viewer"),
                    role_row("wiki", "Internal Wiki", "editor"),
                ]
            },
            "suggest_roles": {
                "roles": [
                    suggestion("wiki", "Internal Wiki", "viewer", 0.9),
                    suggestion("wiki", "Internal Wiki", "editor", 0.8),
                    suggestion("vpn", "VPN", "user", 0.75),
                    suggestion("mail", "Mail", "user", 0.7),
                ]
            },
        }
    )

    rows = asyncio.run(find_role("payroll stuff", tools=tools, directory=FakeStore()))

    assert tools.names == ["list_requestable_roles", "suggest_roles"]
    assert tools.calls[1][1] == {"query": "payroll stuff"}
    assert [row["role_id"] for row in rows] == [
        str(WIKI_VIEWER_ID),
        str(WIKI_EDITOR_ID),
        str(VPN_USER_ID),
    ]
    assert rows[0]["name"] == "Internal Wiki viewer"
    assert rows[0]["system"] == "Internal Wiki"
    assert all(set(row) == {"role_id", "name", "system", "already_held"} for row in rows)


def test_find_person_returns_no_email() -> None:
    """Catches a finder that answers every person it knows (the cap of three
    is lost), that answers an email address or any field beyond id, name and
    department, or that gives an id that is not text.

    Four people share the first name typed; exactly three may come back, all
    of them with the first name, in the three-field shape.
    """
    people = [
        PersonRow(id=uuid.UUID(int=201), display_name="Dana Fake", department="Sales"),
        PersonRow(id=uuid.UUID(int=202), display_name="Dana Quill", department="Support"),
        PersonRow(id=uuid.UUID(int=203), display_name="Dana Moss", department="Sales"),
        PersonRow(id=uuid.UUID(int=204), display_name="Dana Reed", department="Finance"),
        PersonRow(id=uuid.UUID(int=205), display_name="Bo Wren", department="Sales"),
    ]

    rows = asyncio.run(find_person("dana", directory=FakeStore(people=people)))

    assert len(rows) == 3
    assert all(set(row) == {"user_id", "name", "department"} for row in rows)
    assert all(row["name"].startswith("Dana") for row in rows)
    assert {row["user_id"] for row in rows} <= {str(person.id) for person in people[:4]}
    assert "@" not in json.dumps(rows)


# --- the limits -------------------------------------------------------------------


def test_step_cap_ends_with_one_answer_step() -> None:
    """Catches a loop with more or fewer than four model steps, and one that
    still offers tools on its last step (a model that never stops asking would
    then never answer).

    Only three tool calls are made, below the six-call cap, so the cap on
    calls cannot be what removes the tools from the fourth step.
    """
    gateway = ScriptedGateway(
        answer_body(None, wire_call("call_1", "list_my_tickets", {})),
        answer_body(None, wire_call("call_2", "list_my_tickets", {})),
        answer_body(None, wire_call("call_3", "list_my_tickets", {})),
        answer_body("Here is what I found."),
    )
    tools = FakeTools({"list_my_tickets": {"tickets": []}})

    state = run(gateway, tools, FakeStore())

    assert MAX_STEPS == 4
    assert len(gateway.requests) == 4
    assert len(tools.calls) == 3
    for earlier in gateway.requests[:3]:
        assert "tools" in earlier
    assert "tools" not in gateway.requests[3]
    assert "tool_choice" not in gateway.requests[3]
    assert state.reply == "Here is what I found."


def test_tool_call_cap_is_six() -> None:
    """Catches a loop that runs a seventh call in one turn, one that drops the
    seventh call without answering it (the gateway rejects a call with no
    `tool` turn), and one that offers tools again after the cap was hit.

    One step carries seven calls, so the cap, not the step count, ends it: the
    next step is only the second and must still go out with no tools.
    """
    calls = [wire_call(f"call_{n}", "list_my_requests", {}) for n in range(1, 8)]
    gateway = ScriptedGateway(answer_body(None, *calls), answer_body("That is all I could do."))
    tools = FakeTools({"list_my_requests": {"requests": []}})

    state = run(gateway, tools, FakeStore())

    assert MAX_TOOL_CALLS == 6
    assert len(tools.calls) == 6
    assert len(state.tool_results) == 6
    turns = tool_turns(gateway.requests[1])
    assert [turn["tool_call_id"] for turn in turns] == [f"call_{n}" for n in range(1, 8)]
    assert "error" in turns[6]["content"].lower()
    assert "tools" not in gateway.requests[1]
    assert state.reply == "That is all I could do."


@pytest.mark.parametrize(
    ("delay", "timed_out"),
    [
        (timedelta(seconds=90), False),
        (timedelta(seconds=90, microseconds=1), True),
        (timedelta(seconds=91), True),
    ],
    ids=["exactly_90_seconds_is_still_in_time", "one_microsecond_late", "91_seconds"],
)
def test_deadline_with_no_results_gives_the_fixed_line(delay: timedelta, timed_out: bool) -> None:
    """Unhappy path. Catches a loop with a deadline other than 90 seconds, one
    that counts the 90th second as late, one that still calls the model after
    the time is up with nothing to report on, and one that ends with an empty
    reply.

    The clock is moved while the user's message is saved, before the first
    model call. A late turn is scripted no model answer at all, so any model
    call fails the run.
    """
    clock = Clock()
    store = FakeStore(on_user_message=lambda: clock.advance(delay))
    gateway = ScriptedGateway() if timed_out else ScriptedGateway(answer_body("Done in time."))

    state = run(gateway, FakeTools(), store, clock=clock)

    assert TURN_DEADLINE_SECONDS == 90.0
    if timed_out:
        assert state.reply == TIMEOUT_REPLY
        assert TIMEOUT_REPLY
        assert gateway.requests == []
        assert store.messages[-1] == (MessageRole.ASSISTANT, TIMEOUT_REPLY)
    else:
        assert state.reply == "Done in time."
        assert len(gateway.requests) == 1


# --- the decision-word check ------------------------------------------------------


def test_decision_word_breach_is_sent_back_once() -> None:
    """Catches a loop that lets "granted" through when no tool result says so,
    that sends the breach back only once (it goes back twice), that gives the model
    no way to fix it (the reason, with no tools), that sends a third breach
    back too, or that ends the third breach on the model's own words instead of
    the fixed fallback line.

    Two runs: a fixed second answer is the regenerated reply; with no tool
    results, a second breach goes back again and a third ends on the fallback
    with no fourth model call.
    """
    fixed_gateway = ScriptedGateway(
        answer_body("Good news, your access is granted."),
        answer_body("I could not check that. Tell me what you need."),
    )
    fixed_store = FakeStore()

    fixed = run(fixed_gateway, FakeTools(), fixed_store)

    first, second = fixed_gateway.requests
    assert fixed.reply == "I could not check that. Tell me what you need."
    assert fixed.postcheck is PostCheckOutcome.REGENERATED
    assert second["messages"][: len(first["messages"])] == first["messages"]
    assert second["messages"][len(first["messages"])] == {
        "role": "assistant",
        "content": "Good news, your access is granted.",
    }
    reason = second["messages"][-1]
    assert reason["role"] == "user"
    assert reason["content"]
    assert reason["content"] != MESSAGE
    assert "tools" not in second
    assert fixed_store.messages == [
        (MessageRole.USER, MESSAGE),
        (MessageRole.ASSISTANT, "I could not check that. Tell me what you need."),
    ]

    stubborn_gateway = ScriptedGateway(
        answer_body("Your access is granted."),
        answer_body("It is granted now."),
        answer_body("Granted, all done."),
    )

    stubborn = run(stubborn_gateway, FakeTools(), FakeStore())

    assert len(stubborn_gateway.requests) == 3
    third_messages = stubborn_gateway.requests[2]["messages"]
    assert third_messages[-2] == {"role": "assistant", "content": "It is granted now."}
    assert third_messages[-1]["role"] == "user"
    assert "tools" not in stubborn_gateway.requests[2]
    assert stubborn.postcheck is PostCheckOutcome.FALLBACK
    assert stubborn.reply == outcome_reply(stubborn)
    assert stubborn.reply is not None
    assert "granted" not in stubborn.reply.lower()


# --- changes are held, never run --------------------------------------------------


def _create_arguments(**more: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "target_user_id": str(USER_ID),
        "role_id": str(ROLE_ID),
        "justification": "Obviously fake reason for a test.",
        "requested_expiry_days": 30,
    }
    arguments.update(more)
    return arguments


def test_proposed_change_is_held_not_run() -> None:
    """Catches a loop that runs the change itself (the model would then act
    with no yes), that holds it without a key from code, that lets the model
    pick the key, that asks the model to word the summary (a second request),
    or that ends on any outcome but the confirm one.

    The held call must fit the tool's own input model, and the reply must be
    the code-written "I'll file this" summary naming the reason and the days.
    """
    gateway = ScriptedGateway(
        answer_body(None, wire_call("call_c", "create_access_request", _create_arguments()))
    )
    tools = FakeTools()
    store = FakeStore()

    state = run(gateway, tools, store, message="Please file wiki viewer access for me.")

    assert tools.calls == []
    assert len(gateway.requests) == 1
    assert len(store.held) == 1
    held = store.held[0]
    assert held.message == "Please file wiki viewer access for me."
    assert [call.tool for call in held.calls] == ["create_access_request"]
    arguments = held.calls[0].arguments
    key = arguments["idempotency_key"]
    assert isinstance(key, str)
    assert 0 < len(key) <= KEY_LENGTH
    checked = CreateAccessRequestArgs.model_validate(arguments)
    assert checked.role_id == ROLE_ID
    assert checked.justification == "Obviously fake reason for a test."
    assert state.outcome is TurnOutcome.CONFIRM
    assert state.reply is not None
    assert state.reply.startswith("I'll file this:")
    assert '"Obviously fake reason for a test."' in state.reply
    assert "30 days" in state.reply
    assert store.messages[-1] == (MessageRole.ASSISTANT, state.reply)


BAD_PROPOSALS = [
    pytest.param(
        "employee",
        "create_access_request",
        {"target_user_id": str(USER_ID), "role_id": str(ROLE_ID)},
        "justification",
        id="missing_justification",
    ),
    pytest.param(
        "employee",
        "create_access_request",
        _create_arguments(idempotency_key="model-made-key"),
        None,
        id="model_made_the_key",
    ),
    pytest.param(
        "employee",
        "create_access_request",
        _create_arguments(tenant_id=str(TENANT_ID)),
        "tenant_id",
        id="invented_identity",
    ),
    pytest.param(
        "manager",
        "approve_request",
        {"request_id": str(REQUEST_ID)},
        None,
        id="approval_not_in_this_turns_listing",
    ),
]


@pytest.mark.parametrize(("principal", "tool", "arguments", "reason_word"), BAD_PROPOSALS)
def test_bad_change_arguments_go_back_once(
    principal: str, tool: str, arguments: dict[str, Any], reason_word: str | None
) -> None:
    """Unhappy path. Catches a loop that holds a change whose arguments do not
    fit (a yes would then file garbage), that lets the model supply its own
    idempotency key or an identity field, that approves a request this turn
    never listed, that holds something while also asking again, that sends the
    problem back more than once, or that sends it back with no reason.

    Two runs per row. First the model fixes itself: the second step gets an
    error `tool` turn for the call and the answer is its text, nothing held.
    Then the model proposes badly again: the turn ends on the fixed failure
    line, still nothing held, and no third model call.
    """
    recovering_gateway = ScriptedGateway(
        answer_body(None, wire_call("call_b", tool, arguments)),
        answer_body("I need one more detail first. What is it for?"),
    )
    recovering_store = FakeStore(principals=[principal])
    tools = FakeTools()

    recovered = run(recovering_gateway, tools, recovering_store)

    assert recovered.reply == "I need one more detail first. What is it for?"
    assert recovering_store.held == []
    assert tools.calls == []
    assert recovered.outcome is not TurnOutcome.CONFIRM
    assert len(recovering_gateway.requests) == 2
    problem = tool_turns(recovering_gateway.requests[1])
    assert [turn["tool_call_id"] for turn in problem] == ["call_b"]
    assert problem[0]["content"]
    if reason_word is not None:
        assert reason_word in problem[0]["content"]

    stubborn_gateway = ScriptedGateway(
        answer_body(None, wire_call("call_b", tool, arguments)),
        answer_body(None, wire_call("call_d", tool, arguments)),
    )
    stubborn_store = FakeStore(principals=[principal])

    stubborn = run(stubborn_gateway, FakeTools(), stubborn_store)

    assert stubborn.reply == CHANGE_FAILED_REPLY
    assert CHANGE_FAILED_REPLY
    assert stubborn_store.held == []
    assert len(stubborn_gateway.requests) == 2
    assert stubborn_store.messages[-1] == (MessageRole.ASSISTANT, CHANGE_FAILED_REPLY)


# --- the stream -------------------------------------------------------------------


def test_stream_yields_steps_then_reply() -> None:
    """Catches a stream that yields a step for the final text step (which ran no
    tool), skips the step of a lookup, numbers steps from the wrong place, puts
    the reply before the steps, yields more than one reply, or builds a reply
    that differs from the run path's or lacks a key `/chat` gives.

    One lookup step then the answer: exactly one step event for the lookup with
    the tool name, its success and no error code, then one reply event.
    """
    payload: dict[str, Any] = {"requests": []}

    def script() -> ScriptedGateway:
        return ScriptedGateway(
            answer_body(None, wire_call("call_a", "list_my_requests", {})),
            answer_body("You have no open requests."),
        )

    events = stream(script(), FakeTools({"list_my_requests": payload}), FakeStore())
    ran = run(script(), FakeTools({"list_my_requests": payload}), FakeStore())

    assert len(events) == 2
    step, reply = events
    assert isinstance(step, StepEvent)
    assert step.node == "agent_step"
    assert step.round == 1
    assert step.tools == [{"tool": "list_my_requests", "ok": True, "error_code": None}]
    assert isinstance(reply, ReplyEvent)
    assert reply.reply == ran.reply == "You have no open requests."
    assert set(reply.reply_data) == {"reply", "outcome", "citations", "request_id", "decision"}
    assert reply.reply_data["reply"] == ran.reply


# --- the confirmation, the yes path and the wider grounding check ---------

STORED_KEY = "stored-key-0001"
FILED_REPLY = "Done. REQ-0001 is filed and still pending."
FILED_PAYLOAD: dict[str, Any] = {
    "request_id": str(REQUEST_ID),
    "request_number": "REQ-0001",
    "status": "pending",
    "decision": "approval",
    "required_approvers": [],
}
YES_MESSAGE = "Yes, that is right."


def held_question(key: str = STORED_KEY) -> OpenQuestion:
    """A change held from an earlier message: one filing call and its stored key."""
    arguments = CreateAccessRequestArgs.model_validate(
        {
            "target_user_id": str(USER_ID),
            "role_id": str(WIKI_VIEWER_ID),
            "justification": "Obviously fake reason for a test.",
            "requested_expiry_days": 30,
            "idempotency_key": key,
        }
    ).model_dump(mode="json")
    return OpenQuestion(
        asked_at=START,
        message="Please file wiki viewer access for me.",
        calls=(ValidatedCall(tool="create_access_request", arguments=arguments),),
        extraction=IntentExtraction(
            intent=Intent.REQUEST_ACCESS,
            entities=IntentEntities(
                system=None,
                role=None,
                target_user=None,
                department=None,
                expiry_days=None,
                justification=None,
            ),
            confidence=1.0,
            clarifying_question=None,
            second_request=None,
        ),
        resolved=ResolvedEntities(),
    )


def test_confirmation_names_role_days_reason_person() -> None:
    """Catches a confirmation that shows the stored role name (`wiki.viewer`) or
    the role id instead of the plain name, drops the days or the reason, or
    names "someone else" or nobody for a request filed on behalf of a person
    the finder found.

    A manager has the model look up the role and the person, then propose the
    filing for that person. The held reply must carry all four in plain words.
    """
    gateway = ScriptedGateway(
        answer_body(
            None,
            wire_call("call_r", "find_role", {"words": "wiki viewer"}),
            wire_call("call_p", "find_person", {"words": "dana"}),
        ),
        answer_body(
            None,
            wire_call(
                "call_c",
                "create_access_request",
                {
                    "target_user_id": str(uuid.UUID(int=201)),
                    "role_id": str(WIKI_VIEWER_ID),
                    "justification": "Obviously fake reason: audit preparation.",
                    "requested_expiry_days": 30,
                },
            ),
        ),
    )
    tools = FakeTools(
        {"list_requestable_roles": {"roles": [role_row("wiki", "Internal Wiki", "viewer")]}}
    )
    store = FakeStore(
        principals=["manager"],
        people=[PersonRow(id=uuid.UUID(int=201), display_name="Dana Fake", department="Sales")],
    )

    state = run(gateway, tools, store, message="File wiki viewer access for Dana.")

    assert state.outcome is TurnOutcome.CONFIRM
    assert state.reply is not None
    assert "Internal Wiki viewer" in state.reply
    assert "30 days" in state.reply
    assert '"Obviously fake reason: audit preparation."' in state.reply
    assert "Dana Fake" in state.reply
    assert "wiki.viewer" not in state.reply
    assert str(WIKI_VIEWER_ID) not in state.reply
    assert "someone else" not in state.reply
    assert [name for name in tools.names if name == "create_access_request"] == []


def test_yes_files_held_calls_with_stored_keys() -> None:
    """Catches a yes that is still handed to the old graph, that files nothing,
    that files with a new key instead of the stored one (a double yes would
    then file twice), that files the model's idea of the call instead of the
    stored arguments, that offers tools on the reply step (the model could
    then propose or run more), or that never gives the results to the model.

    The chat holds one filing call under a known key. One scripted model answer
    is all the gateway holds: a second model step, or one with tools, fails.
    """
    gateway = ScriptedGateway(answer_body(FILED_REPLY))
    tools = FakeTools({"create_access_request": FILED_PAYLOAD})
    question = held_question()
    store = FakeStore(question=question)

    state = run(gateway, tools, store, message=YES_MESSAGE)

    stored = CreateAccessRequestArgs.model_validate(question.calls[0].arguments)
    assert tools.calls == [("create_access_request", stored.model_dump(mode="json"))]
    assert tools.calls[0][1]["idempotency_key"] == STORED_KEY
    assert store.question is None
    assert store.held == []
    assert len(gateway.requests) == 1
    request = gateway.requests[0]
    assert request["model"] == "agent-strong"
    assert "tools" not in request
    assert "tool_choice" not in request
    assert "REQ-0001" in json.dumps(request["messages"])
    assert state.reply == FILED_REPLY
    assert state.outcome not in (TurnOutcome.CONFIRM, TurnOutcome.FAILED)
    assert state.tool_results == (
        ToolResult(tool="create_access_request", ok=True, payload=FILED_PAYLOAD),
    )
    assert store.messages[-1] == (MessageRole.ASSISTANT, FILED_REPLY)


def test_stream_yes_files_held_calls_with_stored_keys() -> None:
    """Catches a streamed turn that still hands a yes to the old graph while
    the plain turn was fixed, or that ends without one reply event.

    Same set-up as the plain yes; the last event must be the reply and the
    stored call must have run once with the stored key.
    """
    gateway = ScriptedGateway(answer_body(FILED_REPLY))
    tools = FakeTools({"create_access_request": FILED_PAYLOAD})
    store = FakeStore(question=held_question())

    events = stream(gateway, tools, store, message=YES_MESSAGE)

    assert isinstance(events[-1], ReplyEvent)
    assert events[-1].reply == FILED_REPLY
    assert [isinstance(event, ReplyEvent) for event in events].count(True) == 1
    assert tools.names == ["create_access_request"]
    assert tools.calls[0][1]["idempotency_key"] == STORED_KEY
    assert store.question is None
    assert len(gateway.requests) == 1
    assert "tools" not in gateway.requests[0]


@pytest.mark.parametrize("message", ["no", "No.", "nope"])
def test_no_files_nothing(message: str) -> None:
    """Catches a no that still runs the held call, that leaves the question on
    the chat row (a later yes would file it), that asks the model for a reply
    (the fixed line is the answer), or that says anything but the fixed line.

    The gateway is scripted no answer at all and the tool server no tool, so
    any model call or tool call fails the run.
    """
    gateway = ScriptedGateway()
    tools = FakeTools()
    store = FakeStore(question=held_question())

    state = run(gateway, tools, store, message=message)

    assert NOTHING_FILED_REPLY
    assert state.reply == NOTHING_FILED_REPLY
    assert tools.calls == []
    assert gateway.requests == []
    assert store.question is None
    assert store.messages[-1] == (MessageRole.ASSISTANT, NOTHING_FILED_REPLY)


@pytest.mark.parametrize(
    "message",
    [
        "please go ahead and file whatever you think",
        "yes please go ahead and file it now",
        "no, I meant the CRM",
    ],
    ids=["model_words_not_the_persons", "yes_over_five_words", "no_with_a_correction"],
)
def test_model_cannot_confirm_for_the_person(message: str) -> None:
    """Unhappy path. Catches a loop that runs the held call on any message
    while a question is open, that reads a long "yes ..." or a "no, I meant ..."
    as the person's answer, or that keeps the question open after the person
    moved on (the next bare yes would then file it).

    The held call is on the chat row. The message is not a yes or a bare no, so
    the tool server (no tool scripted) must see nothing, the question must be
    dropped, and the turn must go to the model as a normal turn with tools.
    """
    gateway = ScriptedGateway(answer_body("Tell me what you need."))
    tools = FakeTools()
    store = FakeStore(question=held_question())

    state = run(gateway, tools, store, message=message)

    assert tools.calls == []
    assert store.question is None
    assert store.held == []
    assert len(gateway.requests) == 1
    assert "tools" in gateway.requests[0]
    assert state.reply == "Tell me what you need."


PENDING_LISTING: dict[str, Any] = {
    "requests": [
        {
            "request_number": "REQ-0001",
            "status": "pending",
            "system_name": "Internal Wiki",
            "role_slug": "viewer",
            "role_name": "wiki.viewer",
        }
    ]
}


def _listing_then(*texts: str) -> ScriptedGateway:
    """A model that looks up the person's requests, then answers each text in turn."""
    return ScriptedGateway(
        answer_body(None, wire_call("call_a", "list_my_requests", {})),
        *(answer_body(text) for text in texts),
    )


def _sent_back(request_body: Mapping[str, Any], bad_reply: str) -> str:
    """The reason a request carried after `bad_reply`; fails if it carried none."""
    messages = request_body["messages"]
    assert messages[-2] == {"role": "assistant", "content": bad_reply}
    assert messages[-1]["role"] == "user"
    reason = messages[-1]["content"]
    assert isinstance(reason, str)
    return reason


@pytest.mark.parametrize(
    ("message", "bad_reply", "offender"),
    [
        (MESSAGE, "Your request REQ-0099 is pending.", "REQ-0099"),
        ("What is REQ-0042 doing?", "REQ-0042 is fine, and REQ-0001 is pending.", "REQ-0042"),
        (MESSAGE, "Your request REQ-00010 is pending.", "REQ-00010"),
    ],
    ids=["number_in_no_result", "number_only_the_person_typed", "longer_number_with_same_start"],
)
def test_unknown_request_number_is_sent_back(message: str, bad_reply: str, offender: str) -> None:
    """Catches a loop that lets a request number through that no result of this
    turn holds, that lets one pass because the person typed it (only role names
    may be echoed from the person's words), that matches by prefix so
    `REQ-00010` passes on `REQ-0001`, or that sends it back with no reason
    naming the number.

    The listing holds only REQ-0001 and "pending" is allowed by it, so the number
    is the only breach. The second model request must carry the bad reply and a
    reason naming the number, with no tools.
    """
    good = "You have one request, REQ-0001, and it is pending."
    gateway = _listing_then(bad_reply, good)
    tools = FakeTools({"list_my_requests": PENDING_LISTING})

    state = run(gateway, tools, FakeStore(), message=message)

    assert len(gateway.requests) == 3
    assert offender in _sent_back(gateway.requests[2], bad_reply)
    assert "tools" not in gateway.requests[2]
    assert state.reply == good
    assert state.postcheck is PostCheckOutcome.REGENERATED


def test_unknown_role_name_is_sent_back() -> None:
    """Catches a loop that lets the reply name a tenant role that no tool result
    of this turn and no word of the person's message named (the model would be
    promising a role from memory), or that sends it back without the role in
    the reason.

    The reply's request number and decision word are both backed by the
    listing, so the role name is the only breach. The role is in the tenant's
    role list (`LoopStore.role_ids`) as system `payroll`, role `ledger-auditor`.
    """
    bad = "REQ-0001 is pending. I can also get you Payroll ledger auditor access."
    good = "REQ-0001 is pending."
    gateway = _listing_then(bad, good)
    tools = FakeTools({"list_my_requests": PENDING_LISTING})

    state = run(gateway, tools, FakeStore())

    assert len(gateway.requests) == 3
    assert "ledger" in _sent_back(gateway.requests[2], bad).lower()
    assert state.reply == good


def test_role_name_the_person_typed_passes() -> None:
    """Catches a role check so strict that the assistant cannot say back the
    role the person asked about (every such reply would bounce), by ignoring
    the words of the person's own message.

    No tool ran, the reply uses no number or decision word, and the role words
    are the person's own. One scripted answer: a send-back would fail the run.
    """
    reply = "I can look into the Payroll ledger auditor role for you."
    gateway = ScriptedGateway(answer_body(reply))

    state = run(
        gateway,
        FakeTools(),
        FakeStore(),
        message="Can I get the payroll ledger auditor role?",
    )

    assert state.reply == reply
    assert len(gateway.requests) == 1


def test_breach_goes_back_at_most_twice() -> None:
    """Unhappy path. Catches a loop that sends a breach back only once, that
    sends it back a third time, that ends on the model's own breaching words,
    that ends on the "I am not sure" line although tool results exist, or that
    names the wrong value in a later reason.

    Three breaching replies follow one lookup. Exactly four model requests are
    made. The turn ends on the sentence built from the listing, which names
    REQ-0001 and none of the invented numbers.
    """
    gateway = _listing_then("REQ-0099 is pending.", "REQ-0098 is pending.", "REQ-0097 is pending.")
    tools = FakeTools({"list_my_requests": PENDING_LISTING})
    store = FakeStore()

    state = run(gateway, tools, store)

    assert len(gateway.requests) == 4
    assert "REQ-0099" in _sent_back(gateway.requests[2], "REQ-0099 is pending.")
    last_reason = _sent_back(gateway.requests[3], "REQ-0098 is pending.")
    assert "REQ-0098" in last_reason
    assert "REQ-0099" not in last_reason
    assert state.postcheck is PostCheckOutcome.FALLBACK
    assert state.reply is not None
    assert state.reply == outcome_reply(state)
    assert "REQ-0001" in state.reply
    assert "I am not sure" not in state.reply
    for invented in ("REQ-0097", "REQ-0098", "REQ-0099"):
        assert invented not in state.reply
    assert store.messages[-1] == (MessageRole.ASSISTANT, state.reply)


def test_grounded_reply_passes_first_time() -> None:
    """Catches a grounding check that bounces a reply whose request number, role
    name and decision word are all in this turn's results (the rewritten role
    name `Internal Wiki viewer` is the plain form of the row's `wiki.viewer`),
    or one that rewrites the reply it let through.

    One scripted answer after the lookup: any send-back fails the run.
    """
    reply = "Your request REQ-0001 for Internal Wiki viewer is pending."
    gateway = _listing_then(reply)
    tools = FakeTools({"list_my_requests": PENDING_LISTING})

    state = run(gateway, tools, FakeStore())

    assert len(gateway.requests) == 2
    assert state.reply == reply
    assert state.postcheck is PostCheckOutcome.PASSED
