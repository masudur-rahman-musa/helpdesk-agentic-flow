"""A request for another person keeps that person.

Surface the implementer must provide (names fixed by an earlier change):
`confirmation.only_for_you_reply(name)`; an `extract_intent` stop for a
`request_access` turn whose `target_user` names someone else when the speaker's
principals lack `create_access_request_on_behalf`; the details question and the
summary name the person ("for <name>") when they have it; a classify or create
aimed at the speaker while `target_user` names someone else is dropped.

Cost and isolation. No services: whole turns on the fakes of
`test_draft_turns.py` and `confirm_support.py`; the speaker's principals come
from the fake directory row. Each test builds its own fakes.
"""

from typing import Any

import pytest

from helpdesk.agent_api.agent.confirmation import only_for_you_reply
from helpdesk.agent_api.agent.state import TurnOutcome
from helpdesk.shared.auth import rbac
from helpdesk.shared.crud.sessions import OPEN_QUESTION_KEY
from tests.agent_api.confirm_support import DANA_ID, FollowOnGateway, wiki_db_as
from tests.agent_api.test_draft_turns import (
    CLASSIFY,
    CREATE,
    EDITOR_ID,
    USER_ID,
    WIKI_REASON,
    Tools,
    extraction_json,
    filing_tools,
    gateway_of,
    plan_json,
    play,
)

ON_BEHALF = rbac.Tool.CREATE_ACCESS_REQUEST_ON_BEHALF
FRIEND_REPLY = (
    "I can only file access requests for you. john needs to ask for it, "
    "or their manager can ask for them."
)

FULL_ENTITIES: dict[str, Any] = {
    "system": "internal-wiki",
    "role": "editor",
    "target_user": "Dana",
    "expiry_days": 14,
    "justification": WIKI_REASON,
}


def _file_for(person_id: object) -> str:
    """One round that classifies and files the wiki editor request for this person."""
    shared = {"target_user_id": str(person_id), "role_id": str(EDITOR_ID)}
    return plan_json(
        (CLASSIFY, {**shared, "requested_expiry_days": 14}),
        (
            CREATE,
            {**shared, "justification": WIKI_REASON, "requested_expiry_days": 14, "citations": []},
        ),
    )


def test_the_premise_only_managers_and_up_may_ask_for_others() -> None:
    """Catches a test run on a wrong premise: if an employee held the on-behalf
    scope, the next tests would prove nothing about the stop. The employee lacks
    it and the manager has it."""
    assert ON_BEHALF not in rbac.tools_for(["employee"])
    assert ON_BEHALF in rbac.tools_for(["manager"])


@pytest.mark.parametrize("system", ["internal-wiki", None])
def test_employee_asking_for_a_friend_is_told_no(system: str | None) -> None:
    """Catches the live failure of "give my friend john wiki access" dropping
    john and filing for the speaker: an `employee` gets the fixed reply naming
    john and "only for you", and no tool runs (an unscripted tool fails the
    test). With no system named the answer is the same, so the stop does not
    wait for the system question."""
    db = wiki_db_as("employee")
    tools = Tools()

    turn = play(
        db,
        "give my friend john wiki access",
        gateway_of(extraction_json("request_access", system=system, target_user="john")),
        tools,
    )

    assert tools.calls == []
    assert turn.reply == FRIEND_REPLY
    assert turn.reply == only_for_you_reply("john")


def test_the_only_for_you_reply_names_whoever_was_asked_for() -> None:
    """Catches a reply with the name hard-coded or left out: another name goes
    into the same fixed sentence."""
    assert only_for_you_reply("Dana Fake") == (
        "I can only file access requests for you. Dana Fake needs to ask for it, "
        "or their manager can ask for them."
    )


def test_manager_summary_names_the_person() -> None:
    """Catches a summary that hides who the request is for: a `manager` asking for
    Dana gets the held summary, not the stop, and it says "for Dana". It is held
    (outcome CONFIRM, nothing filed), so the on-behalf scope lets it through
    without letting it file unasked."""
    db = wiki_db_as("manager")
    tools = filing_tools()

    turn = play(
        db,
        "ask for wiki editor access for Dana to read the runbooks, 14 days",
        gateway_of(extraction_json("request_access", **FULL_ENTITIES), _file_for(DANA_ID)),
        tools,
    )

    assert turn.outcome is TurnOutcome.CONFIRM
    assert CREATE not in tools.names
    assert turn.reply is not None
    assert "for Dana" in turn.reply
    assert "Internal Wiki editor" in turn.reply


def test_manager_details_question_names_the_person() -> None:
    """Catches a details question that reads as if it were for the speaker: with
    the reason and length still missing, a `manager` asking for Dana is asked
    for them "for Dana"."""
    db = wiki_db_as("manager")
    entities: dict[str, Any] = {
        "system": "internal-wiki",
        "role": "editor",
        "target_user": "Dana",
    }

    turn = play(
        db,
        "ask for wiki editor access for Dana",
        gateway_of(extraction_json("request_access", **entities)),
        Tools(),
    )

    assert turn.outcome is TurnOutcome.CLARIFY
    assert turn.reply is not None
    assert "for Dana" in turn.reply


def test_self_aimed_filing_is_dropped() -> None:
    """Catches a filing for the speaker under another person's name: the message
    names Dana, a manager may ask for her, but the planned classify and create
    aim at the speaker's own id. Neither runs, and nothing is held to be filed
    on a yes. The same turn aimed at Dana's id is held (the summary test above),
    so only the self-aim check can differ."""
    db = wiki_db_as("manager")
    tools = filing_tools()

    play(
        db,
        "ask for wiki editor access for Dana to read the runbooks, 14 days",
        FollowOnGateway(extraction_json("request_access", **FULL_ENTITIES), _file_for(USER_ID)),
        tools,
    )

    assert CLASSIFY not in tools.names
    assert CREATE not in tools.names
    assert OPEN_QUESTION_KEY not in db.metadata
