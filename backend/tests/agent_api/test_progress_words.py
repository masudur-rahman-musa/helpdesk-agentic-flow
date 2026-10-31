"""Plain progress words for each tool step, and the `progress` stream event.

Expected implementation surface:

``helpdesk.agent_api.agent.progress``
    ``PROGRESS_WORDS`` (tool name to plain words) and
    ``progress_words(tool_names)``: one kind of tool gives its own words, several
    different kinds give "Looking a few things up".
``helpdesk.agent_api.agent.events.ProgressEvent(round, text)``
    a frozen dataclass, one of the ``TurnEvent`` kinds.
``helpdesk.agent_api.api.sse``
    ``PROGRESS = "progress"`` and the frame ``{"round", "text"}``.
"""

import json
from typing import Any

import pytest

from helpdesk.agent_api.agent.events import ProgressEvent, ReplyEvent, StepEvent
from helpdesk.agent_api.agent.loop_tools import offered_tools
from helpdesk.agent_api.agent.progress import PROGRESS_WORDS, progress_words
from helpdesk.agent_api.api.sse import PROGRESS, encode_turn_event
from helpdesk.shared.auth import rbac
from tests.agent_api.test_agent_loop import (
    FakeStore,
    FakeTools,
    ScriptedGateway,
    answer_body,
    stream,
    wire_call,
)

SEVERAL = "Looking a few things up"
ACCESS: dict[str, Any] = {"grants": []}
ME: dict[str, Any] = {"user_id": "u-1", "display_name": "Fake Person"}


# --- the table -------------------------------------------------------------------


def test_every_offered_tool_has_its_own_words() -> None:
    """Catches a table that forgets a tool (a step with it would show no words
    or crash), one that leaves a change out because changes are held, one with
    empty or blank words, and one that gives `list_my_access` other words than
    "Checking your access".

    Every tool any kind of person is offered must have non-blank words, and
    asking for that one tool alone must give those same words.
    """
    names = {tool.name for principal in rbac.Principal for tool in offered_tools([principal.value])}

    missing = sorted(name for name in names if name not in PROGRESS_WORDS)
    assert missing == []
    for name in sorted(names):
        assert PROGRESS_WORDS[name].strip(), name
        assert progress_words([name]) == PROGRESS_WORDS[name], name
    assert PROGRESS_WORDS["list_my_access"] == "Checking your access"


def test_several_kinds_of_tool_say_looking_a_few_things_up() -> None:
    """Catches a function that names only the first tool when a step asked for
    several kinds, or one that says the general words for one kind asked twice.

    Two different tools give the general words; the same tool twice gives its own.
    """
    assert progress_words(["get_me", "list_my_access"]) == SEVERAL
    assert progress_words(["list_my_access", "list_my_access"]) == "Checking your access"
    assert PROGRESS_WORDS["list_my_access"] != SEVERAL


# --- the stream ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("calls", "words"),
    [
        (
            [wire_call("call_a", "list_my_access", {})],
            "Checking your access",
        ),
        (
            [wire_call("call_a", "get_me", {}), wire_call("call_b", "list_my_access", {})],
            SEVERAL,
        ),
        (
            [wire_call("call_a", "list_my_access", {}), wire_call("call_b", "list_my_access", {})],
            "Checking your access",
        ),
    ],
    ids=["one_kind", "two_kinds", "one_kind_twice"],
)
def test_one_progress_event_comes_before_each_step(calls: list[dict[str, Any]], words: str) -> None:
    """Catches a stream with no progress event, one that sends it after the
    step it announces, one that sends one per tool call instead of one per
    step, one that numbers the round wrongly, and one that words a step of
    several kinds by its first tool.

    A lookup turn must stream exactly: the progress event (round 1 and the
    words), then the step, then the reply.
    """
    gateway = ScriptedGateway(answer_body(None, *calls), answer_body("Nothing to report."))
    tools = FakeTools({"get_me": ME, "list_my_access": ACCESS})

    events = stream(gateway, tools, FakeStore())

    assert [type(event) for event in events] == [ProgressEvent, StepEvent, ReplyEvent]
    assert events[0] == ProgressEvent(round=1, text=words)
    assert isinstance(events[1], StepEvent)
    assert events[1].round == 1


def test_each_tool_step_gets_its_own_progress_round() -> None:
    """Catches a stream that announces only the first tool step of a turn, or
    that numbers the second progress event with the first step's round.

    Two lookup steps then the reply: progress, step, progress, step, reply, with
    the progress rounds 1 and 2 matching the step rounds.
    """
    gateway = ScriptedGateway(
        answer_body(None, wire_call("call_a", "list_my_access", {})),
        answer_body(None, wire_call("call_b", "get_me", {})),
        answer_body("Nothing to report."),
    )
    tools = FakeTools({"get_me": ME, "list_my_access": ACCESS})

    events = stream(gateway, tools, FakeStore())

    assert [type(event) for event in events] == [
        ProgressEvent,
        StepEvent,
        ProgressEvent,
        StepEvent,
        ReplyEvent,
    ]
    assert [event.round for event in events if isinstance(event, ProgressEvent)] == [1, 2]
    assert [event.round for event in events if isinstance(event, StepEvent)] == [1, 2]
    assert events[2] == ProgressEvent(round=2, text=PROGRESS_WORDS["get_me"])


def test_a_text_only_turn_streams_no_progress() -> None:
    """Unhappy path. Catches a stream that announces every model step, or the
    start of every turn, even when no tool runs (the person would be told
    "checking" about nothing).

    The model answers in text at once. The only event is the reply.
    """
    gateway = ScriptedGateway(answer_body("You have nothing open right now."))

    events = stream(gateway, FakeTools(), FakeStore())

    assert [type(event) for event in events] == [ReplyEvent]


# --- the frame -------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["Checking your access", "odd\n\nevent: reply\ndata: {}"],
    ids=["plain_words", "words_with_a_frame_break"],
)
def test_the_progress_frame_is_one_line_of_json(text: str) -> None:
    """Catches a frame named anything but `progress`, one with more or fewer
    keys than `round` and `text`, one that writes the text outside `json.dumps`
    (words holding a blank line could forge a second event), and one that
    sends more than one frame for one event.

    The frame is `event: progress`, one `data:` line, then a blank line: three
    newlines in all, whatever the text holds.
    """
    frames = encode_turn_event(ProgressEvent(round=2, text=text))

    assert PROGRESS == "progress"
    assert len(frames) == 1
    frame = frames[0]
    assert frame.startswith("event: progress\ndata: ")
    assert frame.endswith("\n\n")
    assert frame.count("\n") == 3
    data = frame.split("\n")[1].removeprefix("data: ")
    assert json.loads(data) == {"round": 2, "text": text}
