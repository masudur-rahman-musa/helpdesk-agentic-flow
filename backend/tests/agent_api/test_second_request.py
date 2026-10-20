"""The fixed line that names a second request in the same message.

Surface the implementer must provide (names fixed by an earlier change):
`helpdesk.agent_api.agent.follow_up` with `INTENT_NAMES` (one plain name per
intent) and `second_request_line(intent)`; `IntentExtraction.second_request`.
The turn appends `"\\n\\n" + second_request_line(second)` to the final reply of
the turn it arrives on (a question, the summary before filing, a composed
reply) and never on the yes turn.

Cost and isolation. No Postgres and no socket: whole turns run on the `ChatDb`,
`Tools` and `play` fakes of `test_draft_turns.py`. Each test builds its own.
"""

from typing import Any

import pytest

from helpdesk.agent_api.agent.follow_up import INTENT_NAMES, second_request_line
from helpdesk.agent_api.agent.state import Intent, TurnOutcome
from tests.agent_api.test_draft_turns import (
    CREATE,
    EMPTY_PLAN,
    FILED_REPLY,
    WIKI_REASON,
    extraction_json,
    filing_tools,
    gateway_of,
    hold_plan,
    play,
    wiki_db,
)

ACCESS_LINE = second_request_line(Intent.REQUEST_ACCESS)
SEPARATOR = "\n\n"


def test_the_second_request_is_named_in_fixed_words() -> None:
    """Catches a turn that drops the second request silently, or lets the model
    word it: "reset my password and give me wiki access" handles the password
    reset first and its reply must end with the fixed line naming access to a
    system."""
    db = wiki_db()

    turn = play(
        db,
        "reset my password and give me wiki access",
        gateway_of(
            extraction_json("reset_password", second_request="request_access"),
            EMPTY_PLAN,
            "Your fake password reset link is on its way.",
        ),
        filing_tools(),
    )

    assert turn.extraction is not None
    assert turn.extraction.intent is Intent.RESET_PASSWORD
    assert turn.extraction.second_request is Intent.REQUEST_ACCESS
    assert "access to a system" in ACCESS_LINE
    assert turn.reply is not None
    assert turn.reply.endswith(SEPARATOR + ACCESS_LINE)


def test_every_intent_has_a_plain_name() -> None:
    """Catches a table that misses an intent (the line for that second request
    would be blank or crash) or gives two intents one name (the person could not
    tell which request was left): all 15 intents are named, each name distinct
    and present in its own line."""
    assert set(INTENT_NAMES) == set(Intent)
    assert len(INTENT_NAMES) == 15
    names = [INTENT_NAMES[intent] for intent in Intent]
    assert all(name.strip() for name in names)
    assert len(set(names)) == 15
    for intent in Intent:
        assert INTENT_NAMES[intent] in second_request_line(intent)


def test_the_line_rides_on_a_question() -> None:
    """Catches a line added only to composed replies: a turn that ends on the
    details question must carry it too."""
    turn = play(
        wiki_db(),
        "I need wiki editor access and reset my password",
        gateway_of(
            extraction_json(
                "request_access",
                system="internal-wiki",
                role="editor",
                second_request="reset_password",
            )
        ),
        filing_tools(),
    )

    assert turn.outcome is TurnOutcome.CLARIFY
    assert turn.reply is not None
    assert turn.reply.endswith(SEPARATOR + second_request_line(Intent.RESET_PASSWORD))


def test_the_line_rides_on_the_summary_before_filing() -> None:
    """Catches a line lost on the "I'll file this" summary: that ending is built
    by code and not by the writer, so it needs its own append."""
    db = wiki_db()
    tools = filing_tools()
    entities: dict[str, Any] = {
        "system": "internal-wiki",
        "role": "editor",
        "expiry_days": 14,
        "justification": WIKI_REASON,
    }

    turn = play(
        db,
        "wiki editor for 14 days to read the runbooks, and reset my password",
        gateway_of(
            extraction_json("request_access", second_request="reset_password", **entities),
            hold_plan(14, WIKI_REASON),
        ),
        tools,
    )

    assert turn.outcome is TurnOutcome.CONFIRM
    assert CREATE not in tools.names
    assert turn.reply is not None
    assert turn.reply.endswith(SEPARATOR + second_request_line(Intent.RESET_PASSWORD))


def test_the_line_rides_on_a_composed_reply() -> None:
    """Catches a line added to code-built endings but not to the writer's
    reply: a composed answer to a policy question carries it as its last
    words."""
    turn = play(
        wiki_db(),
        "what is the wiki policy, and also reset my password",
        gateway_of(
            extraction_json("ask_policy", second_request="reset_password"),
            EMPTY_PLAN,
            "Wiki access follows the fake access policy.",
        ),
        filing_tools(),
    )

    assert turn.reply is not None
    assert turn.reply.startswith("Wiki access follows the fake access policy.")
    assert turn.reply.endswith(SEPARATOR + second_request_line(Intent.RESET_PASSWORD))


def test_the_line_is_not_repeated_on_the_yes_turn() -> None:
    """Catches a line kept in the saved state and added again later: the summary
    carried it, and the yes that files the request must not, because the line
    belongs to the turn the second request arrived on."""
    db = wiki_db()
    tools = filing_tools()
    entities: dict[str, Any] = {
        "system": "internal-wiki",
        "role": "editor",
        "expiry_days": 14,
        "justification": WIKI_REASON,
    }
    summary = play(
        db,
        "wiki editor for 14 days to read the runbooks, and reset my password",
        gateway_of(
            extraction_json("request_access", second_request="reset_password", **entities),
            hold_plan(14, WIKI_REASON),
        ),
        tools,
    )
    assert summary.reply is not None
    line = second_request_line(Intent.RESET_PASSWORD)
    assert line in summary.reply, "premise: it rode the summary"

    yes = play(db, "yes", gateway_of(EMPTY_PLAN, FILED_REPLY), tools)

    assert CREATE in tools.names, "premise: the yes filed the request"
    assert yes.reply is not None
    assert line not in yes.reply


@pytest.mark.parametrize("intent", list(Intent))
def test_a_turn_with_no_second_request_adds_no_line(intent: Intent) -> None:
    """Catches a line added to every reply: the fixed line for each intent must
    never appear on a turn whose `second_request` is null."""
    turn = play(
        wiki_db(),
        "what is the wiki policy",
        gateway_of(
            extraction_json("ask_policy"),
            EMPTY_PLAN,
            "Wiki access follows the fake access policy.",
        ),
        filing_tools(),
    )

    assert turn.reply == "Wiki access follows the fake access policy."
    assert second_request_line(intent) not in turn.reply
