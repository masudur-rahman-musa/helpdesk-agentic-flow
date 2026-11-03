"""A step's read-only lookups run together; everything else stays in order.

The tool caller here counts calls in flight. It never sleeps: a call waits by
handing control back to the event loop a bounded number of times, so a turn
that runs its lookups one after another simply sees nothing else start and
carries on. Each case drives one `asyncio.run` through the loop's own fakes.
"""

import asyncio
import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel

from helpdesk.agent_api.agent.events import ErrorEvent, ReplyEvent, StepEvent
from helpdesk.agent_api.agent.gates import MAX_TOOL_CALLS
from helpdesk.agent_api.agent.loop import FAILURE_REPLY
from helpdesk.agent_api.agent.loop_tools import PersonRow
from helpdesk.agent_api.agent.state import TurnOutcome
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.models.conversation import MessageRole
from tests.agent_api.test_agent_loop import (
    MESSAGE,
    PENDING_LISTING,
    WRITTEN_UNNAMED_ROLE,
    FakeStore,
    FakeTools,
    ScriptedGateway,
    _create_arguments,
    _listing_then,
    answer_body,
    lookup_tools,
    question_body,
    run,
    stream,
    tool_turns,
    wire_call,
)

WAIT_LIMIT = 25
"""How many times a waiting call hands control back before it gives up. Only a
turn that runs its lookups one after another ever uses them all."""


class Probe:
    """What the fakes saw: how many calls were in flight, and who started and
    finished when."""

    def __init__(self) -> None:
        self.inflight = 0
        self.peak = 0
        self.started: list[str] = []
        self.finished: list[str] = []

    def enter(self, label: str) -> None:
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        self.started.append(label)

    def leave(self, label: str) -> None:
        self.inflight -= 1
        self.finished.append(label)

    async def wait_until(
        self, *, started: Sequence[str] = (), finished: Sequence[str] = ()
    ) -> None:
        """Hand control back until those labels have started and finished, or
        `WAIT_LIMIT` times, whichever comes first."""
        for _ in range(WAIT_LIMIT):
            if all(label in self.started for label in started) and all(
                label in self.finished for label in finished
            ):
                return
            await asyncio.sleep(0)

    async def linger(self, turns: int = 5) -> None:
        """Stay in flight for a few turns of the event loop."""
        for _ in range(turns):
            await asyncio.sleep(0)


class FlightTools(FakeTools):
    """The scripted tool server, counting calls in flight.

    `wait_started` names, per tool, the tools that must have started before it
    answers; `wait_finished` names the ones that must have finished.
    """

    def __init__(
        self,
        probe: Probe,
        answers: Mapping[str, Mapping[str, Any]] | None = None,
        failures: Mapping[str, ToolError] | None = None,
        *,
        wait_started: Mapping[str, Sequence[str]] | None = None,
        wait_finished: Mapping[str, Sequence[str]] | None = None,
        linger: int = 0,
    ) -> None:
        super().__init__(answers, failures)
        self.probe = probe
        self._wait_started = dict(wait_started or {})
        self._wait_finished = dict(wait_finished or {})
        self._linger = linger

    async def call_tool(self, name: str, arguments: BaseModel) -> Mapping[str, Any]:
        self.probe.enter(name)
        try:
            await self.probe.wait_until(
                started=self._wait_started.get(name, ()),
                finished=self._wait_finished.get(name, ()),
            )
            await self.probe.linger(self._linger)
            return await super().call_tool(name, arguments)
        finally:
            self.probe.leave(name)


class FlightStore(FakeStore):
    """The chat row, counting the finders' and the note's reads and writes in flight."""

    def __init__(self, probe: Probe, **more: Any) -> None:
        super().__init__(**more)
        self.probe = probe

    async def role_ids(self) -> Mapping[tuple[str, str], uuid.UUID]:
        self.probe.enter("role_ids")
        try:
            await self.probe.linger()
            return await super().role_ids()
        finally:
            self.probe.leave("role_ids")

    async def people(self) -> Sequence[PersonRow]:
        self.probe.enter("people")
        try:
            await self.probe.linger()
            return await super().people()
        finally:
            self.probe.leave("people")

    async def save_request_draft(self, draft: Any) -> None:
        self.probe.enter("save_request_draft")
        try:
            await self.probe.linger()
            await super().save_request_draft(draft)
        finally:
            self.probe.leave("save_request_draft")


ME: dict[str, Any] = {"user_id": str(uuid.UUID(int=1)), "display_name": "Fake Person"}
ACCESS: dict[str, Any] = {"grants": []}
REQUESTS: dict[str, Any] = {"requests": []}
THREE_LOOKUPS = ("get_me", "list_my_access", "list_my_requests")
ANSWERS: dict[str, Mapping[str, Any]] = {
    "get_me": ME,
    "list_my_access": ACCESS,
    "list_my_requests": REQUESTS,
}


def three_lookup_step() -> dict[str, Any]:
    """One model step asking for `get_me`, `list_my_access` and `list_my_requests`."""
    return answer_body(
        None,
        wire_call("call_me", "get_me", {}),
        wire_call("call_access", "list_my_access", {}),
        wire_call("call_requests", "list_my_requests", {}),
    )


def reverse_finish_tools(probe: Probe) -> FlightTools:
    """Tools where the last asked finishes first and the first asked finishes last,
    but only when the three really are in flight together."""
    return FlightTools(
        probe,
        ANSWERS,
        wait_started={name: THREE_LOOKUPS for name in THREE_LOOKUPS},
        wait_finished={
            "get_me": ("list_my_access",),
            "list_my_access": ("list_my_requests",),
        },
    )


# --- run together ----------------------------------------------------------------


def test_lookups_of_one_step_run_at_the_same_time() -> None:
    """Catches a turn that still runs a step's lookups one after another: the
    most calls ever in flight would be 1, not 3.

    The step asks for three lookups. Each waits for the other two to have
    started, so only a turn that starts all three before any finishes reaches a
    peak of 3.
    """
    probe = Probe()
    tools = FlightTools(
        probe, ANSWERS, wait_started={name: THREE_LOOKUPS for name in THREE_LOOKUPS}
    )
    gateway = ScriptedGateway(three_lookup_step(), answer_body("Nothing to report."))

    state = run(gateway, tools, FakeStore())

    assert probe.peak == 3
    assert sorted(probe.started) == sorted(THREE_LOOKUPS)
    assert state.reply == "Nothing to report."


def test_results_keep_the_order_the_model_asked() -> None:
    """Catches a turn that hands results back in the order the lookups finish:
    the model's tool turns, the turn's saved results and the step event would
    be reversed, and each tool turn would sit under a call it does not answer.

    The fake finishes the lookups in reverse (list_my_requests, then
    list_my_access, then get_me), which the first assertion proves. Everything
    after it must still be in the order asked.
    """
    probe = Probe()
    gateway = ScriptedGateway(three_lookup_step(), answer_body("Nothing to report."))

    state = run(gateway, reverse_finish_tools(probe), FakeStore())

    assert probe.finished == ["list_my_requests", "list_my_access", "get_me"]
    assert [turn["tool_call_id"] for turn in tool_turns(gateway.requests[1])] == [
        "call_me",
        "call_access",
        "call_requests",
    ]
    assert [result.tool for result in state.tool_results] == list(THREE_LOOKUPS)

    stream_probe = Probe()
    events = stream(
        ScriptedGateway(three_lookup_step(), answer_body("Nothing to report.")),
        reverse_finish_tools(stream_probe),
        FakeStore(),
    )

    assert stream_probe.finished == ["list_my_requests", "list_my_access", "get_me"]
    step = next(event for event in events if isinstance(event, StepEvent))
    assert [entry["tool"] for entry in step.tools] == list(THREE_LOOKUPS)


# --- never run together ----------------------------------------------------------


def test_a_change_in_a_step_with_lookups_is_held_and_never_run() -> None:
    """Catches a turn that, once it runs lookups together, also sends the
    change to the tool server along with them (the model would act with no
    yes), that loses the hold because the change came last, or that goes on to
    a second model step.

    The step asks two lookups and `create_access_request` with the ids those
    lookups return. The tool server must see the two lookups only; the change
    must sit held on the chat row; the second scripted answer is the written
    question, and no more are scripted.
    """
    gateway = ScriptedGateway(
        answer_body(
            None,
            wire_call("call_me", "get_me", {}),
            wire_call("call_access", "list_my_access", {}),
            wire_call("call_c", "create_access_request", _create_arguments()),
        ),
        question_body(WRITTEN_UNNAMED_ROLE),
    )
    tools = lookup_tools()
    store = FakeStore()

    state = run(gateway, tools, store, message="Please file wiki viewer access for me.")

    assert sorted(tools.names) == ["get_me", "list_my_access"]
    assert [call.tool for call in store.held[0].calls] == ["create_access_request"]
    assert len(store.held) == 1
    assert state.outcome is TurnOutcome.CONFIRM
    assert len(gateway.requests) == 2


def test_finders_and_the_note_run_one_after_another() -> None:
    """Catches a turn that runs the finders and the note at the same time as
    one another: they share the turn's one database session, which allows one
    query at a time.

    A manager's step asks `find_role`, `find_person` and `note_request`. Each of
    their reads and writes stays in flight for a few turns of the event loop, so
    any overlap shows as a peak above 1. All three must have run.
    """
    probe = Probe()
    tools = FlightTools(
        probe,
        {
            "list_requestable_roles": {"roles": []},
            "suggest_roles": {"roles": []},
        },
        linger=5,
    )
    store = FlightStore(
        probe,
        principals=["manager"],
        people=[PersonRow(id=uuid.UUID(int=201), display_name="Dana Fake", department="Sales")],
    )
    gateway = ScriptedGateway(
        answer_body(
            None,
            wire_call("call_r", "find_role", {"words": "wiki viewer"}),
            wire_call("call_p", "find_person", {"words": "dana"}),
            wire_call("call_n", "note_request", {"role": "wiki viewer"}),
        ),
        answer_body("Noted."),
    )

    state = run(gateway, tools, store)

    assert {"list_requestable_roles", "people", "save_request_draft"} <= set(probe.started)
    assert probe.peak == 1
    assert state.reply == "Noted."


# --- the limits and the failures ---------------------------------------------------


def test_the_call_limit_is_counted_before_lookups_start() -> None:
    """Catches a turn that starts every lookup of a step together and only then
    counts them (a seventh call would run), and one that drops the over-limit
    call without answering it.

    Five calls are used over two steps, so one is left. The third step asks two
    lookups: the first must run, and the second must be answered with the
    `limit` error and never sent.
    """
    assert MAX_TOOL_CALLS == 6
    gateway = ScriptedGateway(
        answer_body(
            None,
            wire_call("c1", "list_my_tickets", {}),
            wire_call("c2", "list_my_tickets", {}),
            wire_call("c3", "list_my_tickets", {}),
        ),
        answer_body(
            None, wire_call("c4", "list_my_tickets", {}), wire_call("c5", "list_my_tickets", {})
        ),
        answer_body(
            None, wire_call("c6", "list_my_requests", {}), wire_call("c7", "list_my_tickets", {})
        ),
        answer_body("That is all I could do."),
    )
    tools = FakeTools({"list_my_tickets": {"tickets": []}, "list_my_requests": REQUESTS})

    state = run(gateway, tools, FakeStore())

    assert len(tools.calls) == 6
    assert tools.names.count("list_my_tickets") == 5
    assert tools.names.count("list_my_requests") == 1
    last_step = {turn["tool_call_id"]: turn for turn in tool_turns(gateway.requests[3])}
    assert json.loads(last_step["c6"]["content"]) == REQUESTS
    assert json.loads(last_step["c7"]["content"])["error"] == "limit"
    assert state.reply == "That is all I could do."


def test_an_internal_failure_still_fails_the_turn() -> None:
    """Unhappy path. Catches a turn that stops the other lookups of the step the
    moment one fails (they would be left half done), that carries on after an
    `INTERNAL` failure as if it were a refusal, or that shows the model's own
    words from the failed step.

    `get_me`, asked first, raises `INTERNAL`. `list_my_access` must still finish.
    The turn must end on the failure reply with `INTERNAL`, show none of the
    model's text, and make no further model request.
    """
    probe = Probe()
    model_words = "Let me look that up for you."

    def script() -> ScriptedGateway:
        return ScriptedGateway(
            answer_body(
                model_words,
                wire_call("call_me", "get_me", {}),
                wire_call("call_access", "list_my_access", {}),
            )
        )

    tools = FlightTools(probe, ANSWERS, {"get_me": ToolError("a fake outage")})
    store = FakeStore()

    state = run(script(), tools, store)

    assert "list_my_access" in probe.finished
    assert state.outcome is TurnOutcome.FAILED
    assert state.error_code is ErrorCode.INTERNAL
    assert state.reply == FAILURE_REPLY
    assert model_words not in (state.reply or "")
    assert store.messages[-1] == (MessageRole.ASSISTANT, FAILURE_REPLY)

    streamed_probe = Probe()
    events = stream(
        script(),
        FlightTools(streamed_probe, ANSWERS, {"get_me": ToolError("a fake outage")}),
        FakeStore(),
    )

    assert "list_my_access" in streamed_probe.finished
    assert not any(isinstance(event, ReplyEvent) for event in events)
    assert events[-1] == ErrorEvent(code=ErrorCode.INTERNAL)


def test_a_bad_lookup_argument_starts_no_lookup() -> None:
    """Unhappy path. Catches a turn that sends a lookup whose arguments do not
    fit its input model (the tool server would be asked something no one
    checked), or that lets one bad call hold back or drop its valid neighbour.

    `list_my_requests` gets a limit that is not a number; `get_me` is fine.
    Only `get_me` may reach the tool server. The bad call gets a
    `bad_arguments` answer and the good one its payload, each under its own id.
    """
    gateway = ScriptedGateway(
        answer_body(
            None,
            wire_call("call_bad", "list_my_requests", {"limit": "many"}),
            wire_call("call_me", "get_me", {}),
        ),
        answer_body("I could only check who you are."),
    )
    tools = FakeTools({"get_me": ME})

    state = run(gateway, tools, FakeStore())

    assert tools.names == ["get_me"]
    turns = tool_turns(gateway.requests[1])
    assert [turn["tool_call_id"] for turn in turns] == ["call_bad", "call_me"]
    assert json.loads(turns[0]["content"])["error"] == "bad_arguments"
    assert json.loads(turns[1]["content"]) == ME
    assert [result.tool for result in state.tool_results] == ["get_me"]


def test_every_model_step_uses_the_default_alias() -> None:
    """Catches a turn that moves one step to another model alias to save time
    (the prompt cache is kept per model, so it would stop working).

    The turn has a lookup step, a reply the check sends back, and the rewrite:
    three requests, and each must name `agent-default`.
    """
    bad = "Your request REQ-0099 is pending."
    good = "You have one request, REQ-0001, and it is pending."
    gateway = _listing_then(bad, good)
    tools = FakeTools({"list_my_requests": PENDING_LISTING})

    state = run(gateway, tools, FakeStore(), message=MESSAGE)

    assert state.reply == good
    assert len(gateway.requests) == 3
    assert [request["model"] for request in gateway.requests] == ["agent-default"] * 3
