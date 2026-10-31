"""Shortening a tool result for the model, and only for the model.

Expected implementation surface:

``helpdesk.agent_api.agent.trim``
    ``trim_for_model(payload)``: a pure copy of any JSON-shaped value where a
    list inside a dict longer than 20 rows becomes its first 20 rows plus a
    ``"more": N`` key on the same dict, text over 1,500 characters is cut there
    and ends in ``...(cut)``, and the keys ``score`` and ``chunk_id`` are gone,
    at any depth. A fixed set of fields is never cut however long.

The turn tests drive the loop over the same fakes as the loop's own tests.
"""

import json
from typing import Any

import pytest

from helpdesk.agent_api.agent.state import PostCheckOutcome, ToolResult
from helpdesk.agent_api.agent.trim import trim_for_model
from tests.agent_api.test_agent_loop import (
    FakeStore,
    FakeTools,
    ScriptedGateway,
    answer_body,
    run,
    tool_turns,
    wire_call,
)

CUT_MARK = "...(cut)"
NEVER_TRIMMED = [
    "decision",
    "status",
    "grant_id",
    "request_id",
    "request_number",
    "role_id",
    "role_name",
    "target_user_id",
    "user_id",
    "manager_user_id",
]


def request_rows(count: int) -> list[dict[str, Any]]:
    """`count` listing rows, numbered REQ-0001 upwards."""
    return [
        {"request_number": f"REQ-{number:04d}", "status": "pending", "role_slug": "viewer"}
        for number in range(1, count + 1)
    ]


# --- the function on its own ----------------------------------------------------


@pytest.mark.parametrize(
    ("count", "more"),
    [(0, None), (20, None), (21, 1), (45, 25)],
    ids=["empty", "twenty_stay_whole", "twenty_one_is_one_more", "forty_five_is_twenty_five_more"],
)
def test_a_list_keeps_twenty_rows_and_counts_the_rest(count: int, more: int | None) -> None:
    """Catches a trim with the wrong row limit (19 or 21 kept), one that drops
    the rows past 20 without saying how many (the model would think it has seen
    them all), one that counts the kept rows instead of the cut ones, and one
    that puts the count anywhere but on the dict that held the list.

    Twenty rows is the inclusive limit and must come back untouched with no
    `more` key; one over must come back as the first twenty and `more: 1`.
    """
    rows = request_rows(count)

    trimmed = trim_for_model({"requests": rows})

    assert trimmed["requests"] == rows[:20]
    if more is None:
        assert "more" not in trimmed
    else:
        assert trimmed["more"] == more
    assert set(trimmed) == ({"requests"} if more is None else {"requests", "more"})


@pytest.mark.parametrize(
    ("length", "cut"),
    [(1500, False), (1501, True), (6000, True)],
    ids=["at", "one_over", "far_over"],
)
def test_long_text_is_cut_at_the_limit(length: int, cut: bool) -> None:
    """Catches a trim that cuts at another length (1,499 or 1,501 kept), one
    that cuts text at the limit itself, one that cuts without a mark (the model
    cannot tell the text stopped), and one that only looks at the top level,
    not at the text inside a row.

    Exactly 1,500 characters must stay as they are; 1,501 must become the first
    1,500 and the cut mark. The same holds for a text inside a list row.
    """
    text = "".join(chr(ord("a") + n % 26) for n in range(length))

    top = trim_for_model({"summary": text})["summary"]
    nested = trim_for_model({"rows": [{"text": text}]})["rows"][0]["text"]

    for result in (top, nested):
        if cut:
            assert result[:1500] == text[:1500]
            assert result[1500:].strip() == CUT_MARK
        else:
            assert result == text


def test_score_and_chunk_id_are_dropped() -> None:
    """Catches a trim that keeps the search score or the chunk id (they cost
    tokens and mean nothing to the model), that drops them at the first level
    only, or that drops a field the reply needs along with them.

    A `search_policy` answer keeps `document`, `title`, `version`,
    `heading_path` and `text`, and loses `score` and `chunk_id` inside each
    citation. A `score` on the answer itself goes too.
    """
    citation = {
        "document": "fake-policy.md",
        "title": "Fake Access Policy",
        "version": "v1",
        "heading_path": ["Fake Section", "Fake Part"],
        "chunk_id": "chunk-0001",
        "text": "Obviously fake policy text.",
        "score": 0.42,
    }

    trimmed = trim_for_model({"citations": [citation], "refused": False, "score": 0.9})

    assert trimmed == {
        "citations": [
            {
                "document": "fake-policy.md",
                "title": "Fake Access Policy",
                "version": "v1",
                "heading_path": ["Fake Section", "Fake Part"],
                "text": "Obviously fake policy text.",
            }
        ],
        "refused": False,
    }


@pytest.mark.parametrize("field", NEVER_TRIMMED)
def test_reply_check_fields_are_never_trimmed(field: str) -> None:
    """Catches a trim that cuts a field the reply check or the change check
    reads (a long id or status would stop matching), that spares such a field at
    the first level only, or that skips cutting everything (a trim that does
    nothing would also leave the field whole).

    The field is 5,000 characters, three levels down inside a list row. It must
    come back whole, while the long `text` next to it is cut, so the trim
    demonstrably ran on this row.
    """
    long_value = "x" * 5000
    payload = {"rows": [{"inner": {field: long_value, "text": long_value}}]}

    trimmed = trim_for_model(payload)

    inner = trimmed["rows"][0]["inner"]
    assert inner[field] == long_value
    assert len(inner["text"]) < len(long_value)


def test_trim_leaves_its_input_alone() -> None:
    """Catches a trim that cuts the caller's own dict in place: the reply check
    and the saved result would then see the short version.

    The input has 30 rows and a long text; both must be as they were after the call.
    """
    payload = {"requests": request_rows(30), "text": "y" * 2000}
    before = json.dumps(payload)

    trim_for_model(payload)

    assert json.dumps(payload) == before


# --- the turn --------------------------------------------------------------------


def test_the_reply_check_still_reads_the_full_result() -> None:
    """Catches a turn that trims the result before the reply check reads it: a
    request number that sits only in row 25 would then back nothing, and a true
    reply would be sent back as a breach.

    The listing holds 30 rows; REQ-0025 is past the 20 the model gets. The reply
    names it and must pass at once, in two model requests, with no send-back.
    """
    listing = {"requests": request_rows(30)}
    reply = "Your request REQ-0025 is pending."
    gateway = ScriptedGateway(
        answer_body(None, wire_call("call_a", "list_my_requests", {})),
        answer_body(reply),
    )

    state = run(gateway, FakeTools({"list_my_requests": listing}), FakeStore())

    assert len(gateway.requests) == 2
    assert state.reply == reply
    assert state.postcheck is PostCheckOutcome.PASSED


def test_the_turn_sends_the_trimmed_result_to_the_model() -> None:
    """Catches a turn that sends the whole result on (nothing is saved on the
    model's side), one that trims the saved result too (the Sources list and
    the reply check lose rows), and one that sends 20 rows without the count.

    The listing holds 30 rows. The tool turn the gateway gets must hold 20 rows
    and `more: 10`; the turn's own saved result must hold all 30 and no `more`.
    """
    listing = {"requests": request_rows(30)}
    gateway = ScriptedGateway(
        answer_body(None, wire_call("call_a", "list_my_requests", {})),
        answer_body("You have thirty requests."),
    )

    state = run(gateway, FakeTools({"list_my_requests": listing}), FakeStore())

    sent = json.loads(tool_turns(gateway.requests[1])[0]["content"])
    assert len(sent["requests"]) == 20
    assert sent["requests"] == listing["requests"][:20]
    assert sent["more"] == 10
    assert state.tool_results == (ToolResult(tool="list_my_requests", ok=True, payload=listing),)
    assert len(state.tool_results[0].payload["requests"]) == 30
    assert "more" not in state.tool_results[0].payload
