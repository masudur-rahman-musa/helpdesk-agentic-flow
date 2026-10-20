"""Plain fallback replies and the list-status check.

Surface under test (written before it exists):

``agent_api.agent.postcheck``
    ``permitted_words`` also reads the ``status`` of every row in a result's
    ``requests`` list. ``POSTCHECK_FALLBACK_REPLY`` has new plain text.
    ``outcome_reply(state)`` writes a listing reply (count by status and the
    REQ numbers, or "You have no access requests."), the repeat reply for an
    ``already_requested`` result, and for an ``other`` turn that ran no tool
    ``OUT_OF_SCOPE_REPLY``.

``agent_api.agent.follow_up``
    ``OUT_OF_SCOPE_REPLY``.

Cost: nothing. Frozen Pydantic objects and pure functions; no service.
"""

from typing import Any

import pytest

from helpdesk.agent_api.agent.follow_up import OUT_OF_SCOPE_REPLY
from helpdesk.agent_api.agent.postcheck import (
    POSTCHECK_FALLBACK_REPLY,
    contradicts,
    outcome_reply,
    permitted_words,
    words_in,
)
from helpdesk.agent_api.agent.state import (
    Intent,
    IntentEntities,
    IntentExtraction,
    ToolResult,
    TurnOutcome,
    TurnState,
)
from tests.agent_api.test_agent_postcheck import _state

LIST_TOOL = "list_my_requests"


def _row(number: str, status: str) -> dict[str, Any]:
    """One request row, with the keys the reply reads."""
    return {
        "request_number": number,
        "status": status,
        "role_name": "Fake Reader",
        "system_slug": "fake-wiki",
    }


def _listing(rows: list[dict[str, Any]], *, ok: bool = True) -> ToolResult:
    return ToolResult(tool=LIST_TOOL, ok=ok, payload={"requests": rows})


def _with_intent(state: TurnState, intent: Intent) -> TurnState:
    extraction = IntentExtraction(
        intent=intent,
        entities=IntentEntities(**dict.fromkeys(IntentEntities.model_fields)),
        confidence=0.9,
        clarifying_question=None,
        second_request=None,
    )
    return state.model_copy(update={"extraction": extraction})


def test_list_statuses_permit_their_words() -> None:
    """Catches a check that reads only the top-level `status`: a list result
    carries each status inside `requests[]`, so such a check permits nothing
    and the turn falls back. A `pending` row gives `pending`; an `executed`
    row gives `approved` and `granted`, so exactly those three come back."""
    state = _state(
        tool_results=(_listing([_row("REQ-0001", "pending"), _row("REQ-0002", "executed")]),)
    )
    assert permitted_words(state) == frozenset({"pending", "approved", "granted"})


def test_failed_list_permits_nothing() -> None:
    """Catches a list reader that skips the `ok` test the top-level reader has:
    the same rows from a failed call must permit no word. The payload is
    identical to the green case, so only `ok=False` can flip it."""
    state = _state(
        tool_results=(
            _listing([_row("REQ-0001", "pending"), _row("REQ-0002", "executed")], ok=False),
        )
    )
    assert permitted_words(state) == frozenset()


def test_unknown_list_status_permits_nothing() -> None:
    """Catches a list reader that lets an unknown status widen the reply (a
    default of "everything", or a pass-through of the raw string). The row is
    otherwise well formed, so only the status can narrow it."""
    state = _state(tool_results=(_listing([_row("REQ-0001", "weird")]),))
    assert permitted_words(state) == frozenset()


def test_listing_reply_counts_by_status() -> None:
    """Catches a listing turn that ends on the generic fallback, a reply that
    counts all rows as waiting (the rejected one must not be in the "2"), or
    one that leaves out a REQ number. It must also pass the decision-word
    check it replaces."""
    state = _state(
        tool_results=(
            _listing(
                [
                    _row("REQ-0001", "pending"),
                    _row("REQ-0002", "pending"),
                    _row("REQ-0003", "rejected"),
                ]
            ),
        )
    )
    reply = outcome_reply(state)
    assert "2 requests waiting for approval" in reply
    assert "REQ-0001" in reply
    assert "REQ-0002" in reply
    assert not contradicts(reply, state)


def test_empty_listing_reply() -> None:
    """Catches an empty listing that falls to the generic "couldn't answer"
    text or invents a decision word: no rows is a true answer, said plainly."""
    state = _state(tool_results=(_listing([]),))
    reply = outcome_reply(state)
    assert reply == "You have no access requests."
    assert words_in(reply) == frozenset()


def test_fallback_text_is_plain() -> None:
    """Catches the old fallback that reads like a bug in the app, and a new
    text that drifts from the one the owner chose or brings back a decision
    word the turn never earned."""
    assert POSTCHECK_FALLBACK_REPLY == (
        "Sorry, I couldn't answer that one properly. Try asking it another way, "
        "or open My requests to see where things stand."
    )
    assert "went wrong" not in POSTCHECK_FALLBACK_REPLY.lower()
    assert words_in(POSTCHECK_FALLBACK_REPLY) == frozenset()


def test_out_of_scope_reply_on_other_intent() -> None:
    """Catches an out-of-scope question that ends on the "couldn't answer"
    fallback (which reads like a fault): an `other` turn that ran no tool must
    say it cannot help here. The same state with a different intent must still
    get the generic text, so only the intent picks the sentence."""
    assert OUT_OF_SCOPE_REPLY == (
        "Sorry, I can't help with that here. I can help with access requests, "
        "tickets, passwords and account unlocks."
    )
    base = _state(outcome=TurnOutcome.DONE)
    assert outcome_reply(_with_intent(base, Intent.OTHER)) == OUT_OF_SCOPE_REPLY
    assert outcome_reply(_with_intent(base, Intent.CHECK_MY_ACCESS)) == POSTCHECK_FALLBACK_REPLY


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        pytest.param(
            "pending",
            "You already asked for this (REQ-0002) and it is waiting for approval, "
            "so I have not filed it again.",
            id="pending",
        ),
        pytest.param(
            "approved",
            "You already asked for this (REQ-0002). It is approved and waits to be granted.",
            id="approved",
        ),
    ],
)
def test_repeat_reply_pending_and_approved(status: str, expected: str) -> None:
    """Catches a repeat that is told as a fresh filing: the payload carries
    `status` pending or approved, which the filed-request branch would turn
    into "Your request is filed", so the repeat flag must win over it. Also
    catches a missing number and the same sentence for both statuses."""
    result = ToolResult(
        tool="create_access_request",
        ok=True,
        payload={"already_requested": True, "request_number": "REQ-0002", "status": status},
    )
    assert outcome_reply(_state(tool_results=(result,))) == expected
