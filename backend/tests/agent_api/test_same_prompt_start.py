"""The start of every model request is the same for the same tenant and roles.

The gateway caches the system message and the tool list, so nothing that
changes per request or per user (id, session, name, date, step number) may sit
there, and every call of a turn, the text-only ones too, sends the same tool
list. A text-only step sends it with ``tool_choice: "none"``.

Cost: the loop is driven with the fakes of ``test_agent_loop.py`` (scripted
gateway, fake tools, in-memory chat row, moved clock). No service, no ``db``
marker, no sleep, no network.
"""

import asyncio
import json
import uuid
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

import httpx

from helpdesk.agent_api.agent.loop import run_loop_turn
from helpdesk.agent_api.agent.loop_tools import PersonRow, offered_tools
from helpdesk.agent_api.agent.state import TurnState
from helpdesk.agent_api.services.model_gateway.client import ModelClient
from tests.agent_api.test_agent_loop import (
    API_KEYS,
    GATEWAY_URL,
    MESSAGE,
    SESSION_ID,
    START,
    TENANT_ID,
    TENANT_SLUG,
    USER_ID,
    YES_MESSAGE,
    Clock,
    FakeStore,
    FakeTools,
    ScriptedGateway,
    _deps,
    answer_body,
    held_question,
    reading_body,
    wire_call,
)

OTHER_USER_ID = uuid.UUID("b2222222-2222-5222-8222-222222222222")
OTHER_SESSION_ID = uuid.UUID("b3333333-3333-5333-8333-333333333333")
GATEWAY_USER = "u_" + "a1" * 16
OTHER_GATEWAY_USER = "u_" + "b2" * 16
NAME = "Ada Fakename"
OTHER_NAME = "Bo Otherfake"
LATER = timedelta(days=3, hours=5)


def _lookups_then(final: Mapping[str, Any]) -> ScriptedGateway:
    """Three lookup steps, then the given last answer: four model steps."""
    return ScriptedGateway(
        answer_body(None, wire_call("call_1", "list_my_tickets", {})),
        answer_body(None, wire_call("call_2", "list_my_tickets", {})),
        answer_body(None, wire_call("call_3", "list_my_tickets", {})),
        final,
    )


def _tickets() -> FakeTools:
    return FakeTools({"list_my_tickets": {"tickets": []}})


def _turn(
    gateway: ScriptedGateway,
    tools: FakeTools,
    store: FakeStore,
    *,
    user_id: uuid.UUID = USER_ID,
    session_id: uuid.UUID = SESSION_ID,
    gateway_user: str = GATEWAY_USER,
    clock: Clock | None = None,
    message: str = MESSAGE,
) -> TurnState:
    """One loop turn over the fakes, with the per-user values the caller picks."""
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
                user_id=user_id,
                session_id=session_id,
                tenant_slug=TENANT_SLUG,
                message=message,
                store=store,
                gateway_user=gateway_user,
            )
        finally:
            await model.aclose()

    return asyncio.run(scenario())


def _system(request: Mapping[str, Any]) -> str:
    return json.dumps(request["messages"][0])


def _tools(request: Mapping[str, Any]) -> str:
    return json.dumps(request.get("tools"))


def _wire(principals: tuple[str, ...]) -> list[dict[str, Any]]:
    return [tool.wire() for tool in offered_tools(principals)]


def _person_store(name: str) -> FakeStore:
    return FakeStore(people=[PersonRow(id=uuid.UUID(int=7), display_name=name, department="Fake")])


def _two_users() -> tuple[ScriptedGateway, ScriptedGateway]:
    """The same four-step turn for two people at two times."""
    first = _lookups_then(answer_body("First done."))
    second = _lookups_then(answer_body("Second done."))
    _turn(first, _tickets(), _person_store(NAME), message=f"Hi, I am {NAME}.")
    later = Clock()
    later.advance(LATER)
    _turn(
        second,
        _tickets(),
        _person_store(OTHER_NAME),
        user_id=OTHER_USER_ID,
        session_id=OTHER_SESSION_ID,
        gateway_user=OTHER_GATEWAY_USER,
        clock=later,
        message=f"Hi, I am {OTHER_NAME}.",
    )
    return first, second


def test_two_users_at_two_times_get_the_same_start() -> None:
    """Catches a loop whose system message or tool list changes with the user,
    the session, the gateway user, the name or the time, or whose last
    text-only step sends a different tool part from the steps before it (the
    gateway's cached start would then miss).

    Two four-step turns with the same roles: different user, session, gateway
    user, name, and a clock 3 days 5 hours later. Every request of both turns
    must hold one system message and one tool list, byte for byte.
    """
    first, second = _two_users()

    requests = [*first.requests, *second.requests]
    assert len(requests) == 8
    assert len({_system(request) for request in requests}) == 1
    assert len({_tools(request) for request in requests}) == 1
    assert all("tools" in request for request in requests)


def test_the_start_holds_no_per_request_value() -> None:
    """Catches a loop that puts the user id, the session id, the gateway user,
    the person's name, the date or the step number into the system message or
    the tools instead of the user turn.

    Every request of a four-step turn is searched. First the values are shown
    to be really in play (in the headers or the user turn), so a search for a
    value that never reaches the request cannot pass by being empty.
    """
    gateway = _lookups_then(answer_body("Done."))
    _turn(gateway, _tickets(), _person_store(NAME), message=f"Hi, I am {NAME}.")

    assert gateway.headers[0]["x-user"] == GATEWAY_USER
    assert gateway.headers[0]["x-session"] == str(SESSION_ID)
    assert NAME in json.dumps(gateway.requests[0]["messages"][1:])
    forbidden = [
        str(USER_ID),
        str(SESSION_ID),
        GATEWAY_USER,
        NAME,
        START.date().isoformat(),
    ]
    for step, request in enumerate(gateway.requests, start=1):
        start = (_system(request) + _tools(request)).lower()
        for value in forbidden:
            assert value.lower() not in start, value
        assert f"step {step}" not in start


def test_every_step_of_a_turn_sends_the_same_tools() -> None:
    """Catches a loop whose last, text-only step drops the tool list (its tool
    part then differs from every other call), one that sends `tool_choice`
    "auto" on the answer step, and one that sends "none" too early.

    Three lookup steps then the answer step (the fourth and last): all four
    requests carry the same tools; the first three say "auto", the last "none".
    """
    gateway = _lookups_then(answer_body("Here is what I found."))

    _turn(gateway, _tickets(), FakeStore())

    assert len(gateway.requests) == 4
    expected = _wire(("employee",))
    assert expected
    assert [request.get("tools") for request in gateway.requests] == [expected] * 4
    assert [request.get("tool_choice") for request in gateway.requests] == [
        "auto",
        "auto",
        "auto",
        "none",
    ]


def test_a_send_back_rewrite_sends_the_same_tools() -> None:
    """Catches a rewrite step after a decision-word breach that goes out with
    no tools, or with the tools but `tool_choice` "auto" (the model could then
    call a tool while it should only fix its words).

    The first answer says "granted" with no tool result behind it; the rewrite
    must carry the first request's tools and `tool_choice` "none".
    """
    gateway = ScriptedGateway(
        answer_body("Good news, your access is granted."),
        answer_body("I could not check that. Tell me what you need."),
    )

    _turn(gateway, FakeTools(), FakeStore())

    first, rewrite = gateway.requests
    assert first["tool_choice"] == "auto"
    assert first["tools"] == _wire(("employee",))
    assert rewrite["tools"] == first["tools"]
    assert rewrite["tool_choice"] == "none"


def test_the_reply_after_a_held_yes_sends_the_same_tools() -> None:
    """Catches a yes-to-a-held-change reply step that sends no tools (a
    different tool part from the rest of the chat), or one that sends them
    with "auto" (the model could propose or run more after the change).

    The reading and one model request are scripted; the reply request must
    carry the offered tools and `tool_choice` "none".
    """
    gateway = ScriptedGateway(
        reading_body("yes"), answer_body("Done. REQ-0001 is filed and still pending.")
    )
    tools = FakeTools(
        {
            "create_access_request": {
                "request_id": str(uuid.UUID(int=9)),
                "request_number": "REQ-0001",
                "status": "pending",
                "decision": "approval",
                "required_approvers": [],
            }
        }
    )

    _turn(gateway, tools, FakeStore(question=held_question()), message=YES_MESSAGE)

    assert len(gateway.requests) == 2
    request = gateway.requests[1]
    assert request["tools"] == _wire(("employee",))
    assert request["tool_choice"] == "none"


def test_a_tool_call_on_a_text_only_step_runs_nothing() -> None:
    """Unhappy path. Catches a loop that, now that the tool list is sent on the
    last step, runs a tool call the model sends back anyway (the model would
    then act on a step meant for words only).

    The scripted answer step holds a tool call and text. Only the three
    earlier lookups run, no fifth model request is made (it would fail the
    run), and the reply is the text.
    """
    gateway = _lookups_then(
        answer_body("The words only.", wire_call("call_x", "list_my_tickets", {}))
    )
    tools = _tickets()

    state = _turn(gateway, tools, FakeStore())

    assert gateway.requests[3]["tool_choice"] == "none"
    assert tools.names == ["list_my_tickets"] * 3
    assert len(gateway.requests) == 4
    assert state.reply == "The words only."


def test_other_roles_get_other_tools() -> None:
    """Unhappy path. Catches a change that makes the start the same for
    everyone by offering one shared tool list: a person would then see a tool
    their roles do not allow.

    An employee turn and a manager turn: the tools differ, each equals the
    offered list for its roles, and the system message stays equal.
    """
    employee = ScriptedGateway(answer_body("Hello."))
    manager = ScriptedGateway(answer_body("Hello."))

    _turn(employee, FakeTools(), FakeStore(principals=("employee",)))
    _turn(manager, FakeTools(), FakeStore(principals=("manager",)))

    employee_request = employee.requests[0]
    manager_request = manager.requests[0]
    assert employee_request["tools"] == _wire(("employee",))
    assert manager_request["tools"] == _wire(("manager",))
    assert _tools(employee_request) != _tools(manager_request)
    assert _system(employee_request) == _system(manager_request)
