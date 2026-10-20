"""Fixed word rules for "skip the approval" and "access to everything".

Surface the implementer must provide (names fixed by an earlier change): the module
`helpdesk.agent_api.agent.pushback` with `skips_approval(message)`,
`wants_everything(message)`, `SKIP_LINE` and `EVERYTHING_REPLY`. The skip line
leads the reply of a skip-approval turn (`follow_up.py`); an "everything"
request ends the turn with no tools and the fixed no.

Cost and isolation. No services: the two rules are pure functions on text, and
three whole turns run on the fakes of `test_draft_turns.py`. No model reads the
message for these rules. Each test builds its own fakes.
"""

import pytest

from helpdesk.agent_api.agent.pushback import (
    EVERYTHING_REPLY,
    SKIP_LINE,
    skips_approval,
    wants_everything,
)
from helpdesk.agent_api.agent.state import TurnOutcome
from helpdesk.agent_api.agent.tool_args import SEARCH_POLICY
from tests.agent_api.confirm_support import FOLLOW_ON_REPLY
from tests.agent_api.test_draft_turns import (
    EMPTY_PLAN,
    Tools,
    extraction_json,
    gateway_of,
    play,
    search_page,
    wiki_db,
)


def test_the_two_fixed_lines_are_the_exact_words() -> None:
    """Catches a reworded line: the person is told these words, and a test that
    only checked "starts with" would let a different sentence through."""
    assert SKIP_LINE == (
        "Approval can't be skipped: the policy decides who must approve, and I can't change that."
    )
    assert EVERYTHING_REPLY == (
        "No. I can't ask for access to everything. Each request is for one role "
        "on one system. Tell me the one you need and why."
    )


@pytest.mark.parametrize(
    "message",
    [
        "skip the approval",
        "without approval",
        "bypass approval",
        "no approval needed",
        "Please SKIP the Approval step",
        "skip all the approval",
        "I need wiki editor access, skip the approval",
    ],
)
def test_skip_words_are_caught(message: str) -> None:
    """Catches a rule that misses a way of asking to skip approval: each of the
    four plan phrases, an upper-case one, the third word before the approve word,
    and one after a request, is caught."""
    assert skips_approval(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "approve my request",
        "who approves CRM editor",
        "approve all of them",
        "I need wiki editor access",
        "skip lunch today, then I will check on the approval",
    ],
)
def test_approve_alone_is_not_caught(message: str) -> None:
    """Catches a rule that fires on the approve word alone, or on a skip word far
    from it: asking who approves, or to approve something, is a normal request,
    and "skip" with the approve word more than three words away is not a
    request to skip it."""
    assert skips_approval(message) is False


@pytest.mark.parametrize(
    "message",
    [
        "give me admin access to everything",
        "I want access to all systems",
        "Give me every system",
        "ACCESS TO EVERYTHING please",
    ],
)
def test_everything_words_are_caught(message: str) -> None:
    """Catches a rule that misses "everything", "all systems" or "every system",
    in any case."""
    assert wants_everything(message) is True


@pytest.mark.parametrize(
    "message",
    ["I need wiki editor access", "give me access to the ledger", "every day this week"],
)
def test_one_system_is_not_everything(message: str) -> None:
    """Catches a rule that fires on "every" or "all" alone: a request for one
    system, or a message with "every day" in it, is not a request for everything."""
    assert wants_everything(message) is False


def test_skip_line_comes_first() -> None:
    """Catches a skip-approval turn that answers only the question, or buries the
    line after it: "skip the approval, I need wiki editor access" still asks for
    the length and reason, and the reply STARTS with the fixed line, then asks."""
    turn = play(
        wiki_db(),
        "skip the approval, I need wiki editor access",
        gateway_of(
            extraction_json("request_access", system="internal-wiki", role="editor"),
        ),
        Tools(),
    )

    assert turn.outcome is TurnOutcome.CLARIFY
    assert turn.reply is not None
    assert turn.reply.startswith(SKIP_LINE)
    assert "business reason" in turn.reply


def test_no_skip_line_without_a_skip_request() -> None:
    """Catches a line added to every request: the same turn without the skip
    words asks its question and never says approval cannot be skipped."""
    turn = play(
        wiki_db(),
        "I need wiki editor access",
        gateway_of(
            extraction_json("request_access", system="internal-wiki", role="editor"),
        ),
        Tools(),
    )

    assert turn.reply is not None
    assert "can't be skipped" not in turn.reply


def test_everything_stops_with_no() -> None:
    """Catches the live failure of "give me admin access to everything" being
    treated as a normal request: no tool runs, no search, no further model
    request (none is staged), and the reply is the fixed no, starting "No."."""
    tools = Tools()

    turn = play(
        wiki_db(),
        "give me admin access to everything",
        gateway_of(extraction_json("request_access", system="everything", role="admin")),
        tools,
    )

    assert tools.calls == []
    assert turn.reply == EVERYTHING_REPLY
    assert turn.reply is not None
    assert turn.reply.startswith("No.")


def test_everything_off_request_is_not_caught() -> None:
    """Catches an "everything" rule that fires on any intent: "what can I ask for,
    everything?" is an `ask_capabilities` turn and goes on as one, so its reply is
    the composed answer and not the fixed no."""
    tools = Tools({SEARCH_POLICY: search_page()})

    turn = play(
        wiki_db(),
        "what can I ask for, everything?",
        gateway_of(extraction_json("ask_capabilities"), EMPTY_PLAN, FOLLOW_ON_REPLY),
        tools,
    )

    assert turn.reply == FOLLOW_ON_REPLY
    assert turn.reply != EVERYTHING_REPLY
