"""The reply writer is handed roles by their plain name, never a stored code value.

Surface under test (written before it exists):

``helpdesk.agent_api.agent.role_words``
    ``with_plain_role_names(data)``: a copy of ``data`` in which every dict
    (at any depth, through dicts and lists) holding ``system_name`` and
    ``role_name`` has ``role_name`` rewritten to the plain name: the system's
    name, then the role's own word. The word comes from ``role_slug`` when the
    row has one, else from the part of ``role_name`` after its last ".".
    Pure; the input is left alone.
``compose_reply`` hands the writer the tool results and the requestable
groups through it.
``reply_composition.md`` tells the writer to name a role only that way.

Cost: nothing backing. The node case uses the staged gateway and recording
session of ``test_agent_compose.py``; the rest are plain functions.
"""

import copy
from typing import Any

import pytest

from helpdesk.agent_api.agent.prompts import load_prompt
from helpdesk.agent_api.agent.role_words import with_plain_role_names
from helpdesk.agent_api.agent.state import ToolResult
from tests.agent_api.test_agent_compose import (
    RecordingSession,
    StagedGateway,
    _compose,
    _messages,
    _state,
)

STORED = "wiki.editor"
PLAIN = "Internal Wiki editor"


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "system_name": "Internal Wiki",
        "role_slug": "editor",
        "role_name": STORED,
    }
    row.update(overrides)
    return row


def test_a_row_with_a_slug_gets_its_plain_name() -> None:
    """Catches an implementation that leaves the stored name in place, and one
    that reads the word after the dot even when a slug is given: the second
    row has a slug ("page-editor") that differs from the dotted word
    ("editor"), so only reading the slug gives "Internal Wiki page editor"."""
    same = with_plain_role_names(_row())
    differs = with_plain_role_names(_row(role_slug="page-editor"))

    assert same["role_name"] == PLAIN
    assert differs["role_name"] == "Internal Wiki page editor"
    assert same["role_slug"] == "editor"
    assert same["system_name"] == "Internal Wiki"


def test_a_row_without_a_slug_reads_the_word_after_the_dot() -> None:
    """Catches an implementation that needs a slug (review rows carry none and
    would crash or keep the code value), and one that keeps the dash: the
    word after the dot reads with its dash as a space."""
    row = {"system_name": "Payroll", "role_name": "hris.payroll-approver"}

    assert with_plain_role_names(row)["role_name"] == "Payroll payroll approver"


def test_the_last_dot_is_the_one_that_counts() -> None:
    """Catches an implementation that splits at the FIRST dot, which would
    turn "a.b.editor" into "Internal Wiki b.editor" and leave a dot in chat."""
    row = {"system_name": "Internal Wiki", "role_name": "a.b.editor"}

    assert with_plain_role_names(row)["role_name"] == PLAIN


def test_rows_inside_lists_and_dicts_are_rewritten() -> None:
    """Catches an implementation that only rewrites the top level: both rows
    sit in a list inside a dict, and every other key must stay equal."""
    data = {"requests": [_row(), _row(system_name="Mail and Files", role_slug="user")], "total": 2}

    out = with_plain_role_names(data)

    assert [r["role_name"] for r in out["requests"]] == [PLAIN, "Mail and Files user"]
    assert out["total"] == 2
    assert [r["system_name"] for r in out["requests"]] == ["Internal Wiki", "Mail and Files"]


def test_a_dict_without_system_name_is_left_alone() -> None:
    """Catches an implementation that rewrites any `role_name` it meets: with
    no system name there is nothing to build the plain name from, so the dict
    must come back equal."""
    data = {"role_name": STORED}

    assert with_plain_role_names(data) == {"role_name": STORED}


def test_the_input_is_left_alone() -> None:
    """Catches an implementation that rewrites in place: the same tool result
    is stored on the turn and sent to the web, which still needs the stored
    name."""
    data: dict[str, Any] = {"requests": [_row()], "one": _row()}
    before = copy.deepcopy(data)

    with_plain_role_names(data)

    assert data == before
    assert data["one"]["role_name"] == STORED


def test_the_writer_never_sees_a_stored_role_name() -> None:
    """Catches a `with_plain_role_names` that exists but is applied to only one
    of the two things the writer reads: the gateway request itself is read,
    and no message may hold the stored name while the tool-results message and
    the requestable message each hold the plain one."""
    gateway = StagedGateway()
    no_slug = {k: v for k, v in _row().items() if k != "role_slug"}
    requests = ToolResult(
        tool="list_my_requests",
        ok=True,
        payload={"requests": [{"request_number": "REQ-0107", **no_slug}]},
    )
    roles = ToolResult(
        tool="list_requestable_roles",
        ok=True,
        payload={
            "roles": [
                {
                    **_row(),
                    "system_slug": "wiki",
                    "system_sensitivity": "low",
                    "privilege": "write",
                    "default_expiry_days": 90,
                    "in_my_template": True,
                    "already_held": False,
                }
            ]
        },
    )
    state = _state(tool_results=[requests, roles])

    _compose(state, gateway, RecordingSession())

    # The system messages are the prompt files; the prompt line itself names
    # the code value to avoid. Only the data messages are checked.
    contents = [m["content"] for m in _messages(gateway.requests[0]) if m["role"] != "system"]
    assert not [c for c in contents if STORED in c]
    tool_message = next(c for c in contents if c.startswith("tool results:"))
    requestable = next(c for c in contents if c.startswith("roles this person may ask for"))
    assert PLAIN in tool_message
    assert PLAIN in requestable


def test_the_composition_prompt_names_roles_in_plain_words() -> None:
    """Catches a prompt that never tells the writer to use the plain name, and
    one that gives the example without the "never": one line must hold the
    plain example, the code value to avoid and the word "never"."""
    lines = load_prompt("reply_composition").text.splitlines()

    assert any(PLAIN in line and STORED in line and "never" in line.lower() for line in lines)


@pytest.mark.parametrize("data", [[], {}, "wiki.editor", 3, None])
def test_data_without_rows_comes_back_equal(data: Any) -> None:
    """Catches an implementation that fails on empty lists, empty dicts or
    plain values (a failed tool result has no payload at all)."""
    assert with_plain_role_names(data) == data
