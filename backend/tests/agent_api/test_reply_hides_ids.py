"""The reply writer never sees a database id, and says "number" where it said "id".

Surface under test (written before it exists):

``helpdesk.agent_api.agent.nodes.compose_reply``
    ``strip_id_keys(data)``: a copy of ``data`` with every dict key ending
    ``_id`` or ``_ids`` removed at any depth (through nested dicts and lists).
    ``compose_reply`` hands the writer the tool results through it.
``reply_composition.md`` and ``system.md`` say "request number", never "request id".
``DecisionCard.request_number``: the shown number of a filed request, ``None``
when nothing was filed.

Cost: nothing backing. The node cases use the staged gateway and recording
session of ``test_agent_compose.py``; the rest are plain functions.
"""

import copy
import json
import re
import uuid
from typing import Any

import pytest

from helpdesk.agent_api.agent.nodes.compose_reply import strip_id_keys
from helpdesk.agent_api.agent.prompts import load_prompt
from helpdesk.agent_api.agent.state import ToolResult
from helpdesk.agent_api.services.decision_card import decision_card
from tests.agent_api.test_agent_compose import (
    RecordingSession,
    StagedGateway,
    _compose,
    _messages,
    _state,
)
from tests.agent_api.test_decision_card import (
    CLASSIFY_REQUEST,
    CREATE_REQUEST,
    _classify_payload,
    _create_payload,
    _ok,
)
from tests.agent_api.test_decision_card import (
    _state as _card_state,
)

_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

# Obviously fake ids, tests only.
REQUEST_ID = "aaaaaaaa-aaaa-5aaa-8aaa-aaaaaaaaaaaa"
ROLE_ID = "bbbbbbbb-bbbb-5bbb-8bbb-bbbbbbbbbbbb"
APPROVER_ID = "cccccccc-cccc-5ccc-8ccc-cccccccccccc"
OTHER_APPROVER_ID = "dddddddd-dddd-5ddd-8ddd-dddddddddddd"


def _create_result() -> dict[str, Any]:
    return {
        "request_id": REQUEST_ID,
        "request_number": "REQ-0107",
        "role_id": ROLE_ID,
        "approver_user_ids": [APPROVER_ID, OTHER_APPROVER_ID],
        "status": "pending",
        "risk_class": "elevated",
    }


def test_writer_data_has_no_id_keys() -> None:
    """Catches a strip that misses one of the two suffixes (`_ids` lists left
    behind, or `_id` left behind), one that strips by a fixed key list so a
    new `grant_id` slips through, and one that over-strips: `request_number`,
    `status` and a key that merely ends in the letters `id` (`valid`,
    `paid`) must all stay."""
    data = {**_create_result(), "valid": True, "paid": "no"}

    stripped = strip_id_keys(data)

    assert stripped == {
        "request_number": "REQ-0107",
        "status": "pending",
        "risk_class": "elevated",
        "valid": True,
        "paid": "no",
    }


def test_strip_id_keys_leaves_its_input_alone() -> None:
    """Catches a strip that deletes keys from the caller's own dict: the same
    tool result is stored on the turn and returned to the web, which still
    needs `request_id`."""
    data = _create_result()
    before = copy.deepcopy(data)

    strip_id_keys(data)

    assert data == before


def test_writer_data_has_no_uuid_anywhere() -> None:
    """Catches a strip that only looks at the top level: three rows in a
    nested `requests` list, each with ids at its own level and in a nested
    `target` object and a list of objects, must leave no key ending in an id
    and no string that parses as a UUID (every value in this fixture that
    looks like a UUID sits under an id key)."""
    rows = [
        {
            "request_id": str(uuid.uuid4()),
            "request_number": f"REQ-{n:04d}",
            "target": {"user_id": str(uuid.uuid4()), "display_name": "Fake Person"},
            "approvals": [{"slot": "it_admin", "user_id": str(uuid.uuid4())}],
            "approver_user_ids": [str(uuid.uuid4())],
        }
        for n in (1, 2, 3)
    ]

    stripped = strip_id_keys({"requests": rows, "total": 3})

    dumped = json.dumps(stripped)
    assert not _UUID.search(dumped)
    assert "_id" not in dumped
    assert [row["request_number"] for row in stripped["requests"]] == [
        "REQ-0001",
        "REQ-0002",
        "REQ-0003",
    ]
    assert stripped["requests"][0]["target"] == {"display_name": "Fake Person"}
    assert stripped["requests"][0]["approvals"] == [{"slot": "it_admin"}]


def test_the_writer_is_handed_tool_results_without_ids() -> None:
    """Catches a `strip_id_keys` that exists but is never applied on the way to
    the model: the gateway request itself is read, and its tool-results
    message must hold the number and none of the three ids (key names or
    values)."""
    gateway = StagedGateway()
    state = _state(
        tool_results=[ToolResult(tool="create_access_request", ok=True, payload=_create_result())]
    )

    _compose(state, gateway, RecordingSession())

    messages = _messages(gateway.requests[0])
    tool_message = next(m["content"] for m in messages if m["content"].startswith("tool results:"))
    assert "REQ-0107" in tool_message
    for leaked in (REQUEST_ID, ROLE_ID, APPROVER_ID, OTHER_APPROVER_ID):
        assert leaked not in tool_message
    for key in ("request_id", "role_id", "approver_user_ids"):
        assert key not in tool_message


@pytest.mark.parametrize("name", ["reply_composition", "system"])
def test_prompts_say_number_not_id(name: str) -> None:
    """Catches a prompt left telling the writer to quote the "request id"
    (it would then ask for a thing it is no longer given, or invent one), and
    a prompt that dropped the instruction without naming the number: both
    shipped prompts must say "request number" and neither may say "request id"."""
    text = load_prompt(name).text.lower()

    assert "request id" not in text
    assert "request number" in text


def test_decision_card_carries_the_request_number() -> None:
    """Catches a card that drops the number (the web would then fall back to
    the id), and one that invents a number for an unfiled request: a filed
    card shows exactly the number the tool answered, a classification (nothing
    filed) shows `None`."""
    filed = decision_card(
        _card_state(_ok(CREATE_REQUEST, _create_payload(request_number="REQ-0107")))
    )
    unfiled = decision_card(_card_state(_ok(CLASSIFY_REQUEST, _classify_payload())))

    assert filed is not None
    assert filed.model_dump(mode="json")["request_number"] == "REQ-0107"
    assert unfiled is not None
    assert unfiled.model_dump(mode="json")["request_number"] is None
