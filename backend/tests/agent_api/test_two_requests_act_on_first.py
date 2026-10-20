"""Two requests in one message act only on the first.

Surface the implementer must provide (names fixed by an earlier change): with
`second_request` set, `create_ticket` leaves the turn's allowed tools unless
the first intent is `open_ticket`; `reset_password` and `unlock_account` turns
blank `system` and `role` before resolving.

Cost and isolation. No services: `validate_tool_calls` is run directly on a
plain `TurnState`, and one whole turn runs on the fakes of `test_draft_turns.py`.
Each test builds its own state and fakes.
"""

import asyncio
import json
from datetime import UTC, datetime
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.agent_api.agent.nodes.validate_tool_calls import validate_tool_calls
from helpdesk.agent_api.agent.state import (
    Intent,
    IntentEntities,
    IntentExtraction,
    ProposedCall,
    TurnOutcome,
    TurnState,
)
from helpdesk.agent_api.agent.turn_deps import TurnDeps
from helpdesk.agent_api.services.mcp_client import ToolCaller
from helpdesk.agent_api.services.model_gateway.client import ModelClient
from helpdesk.shared.crud.sessions import OPEN_QUESTION_KEY, REQUEST_DRAFT_KEY
from tests.agent_api.confirm_support import RESET, TICKET, proposed
from tests.agent_api.test_draft_turns import (
    SESSION_ID,
    TENANT_ID,
    USER_ID,
    Tools,
    extraction_json,
    gateway_of,
    plan_json,
    play,
    wiki_db,
)

NOW = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
_NO_DEPS = TurnDeps(
    db=cast(AsyncSession, None),
    model=cast(ModelClient, None),
    tools=cast(ToolCaller, None),
    now=lambda: NOW,
)
"""A pure node must never touch these: every member is `None`."""

_BLANK = dict.fromkeys(IntentEntities.model_fields)


def _state(first: Intent, second: Intent | None, *calls: ProposedCall) -> TurnState:
    """A turn standing where `plan_tools` has just written its proposals."""
    return TurnState(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        session_id=SESSION_ID,
        tenant_slug="fake-tenant",
        user_message="a fake two-part message",
        extraction=IntentExtraction(
            intent=first,
            entities=IntentEntities(**_BLANK),
            confidence=1.0,
            clarifying_question=None,
            second_request=second,
        ),
        round_number=1,
        proposed=calls,
    )


def _ticket() -> ProposedCall:
    return proposed(TICKET, kind="general", title="Fake wiki access", body="Obviously fake.")


def test_reset_and_wiki_opens_no_ticket() -> None:
    """Catches the live failure of "reset my password and give me wiki editor
    access" opening a ticket for the second half and speaking of a password "for
    Internal Wiki": the model leaks system and role into the reset turn, yet the
    summary is the plain reset, only `reset_own_password` is held, nothing runs,
    and neither the reply nor the saved draft nor the held entities mention the
    wiki."""
    db = wiki_db()
    tools = Tools()

    turn = play(
        db,
        "reset my password and give me wiki editor access",
        gateway_of(
            extraction_json(
                "reset_password",
                second_request="request_access",
                system="internal-wiki",
                role="editor",
            ),
            plan_json((RESET, {})),
        ),
        tools,
    )

    assert tools.calls == []
    assert turn.outcome is TurnOutcome.CONFIRM
    assert turn.reply is not None
    assert turn.reply.startswith("I'll reset your password. Say yes to go ahead, or no to stop.")
    assert "Internal Wiki" not in turn.reply
    held = db.metadata[OPEN_QUESTION_KEY]
    assert [call["tool"] for call in held["calls"]] == [RESET]
    assert held["extraction"]["entities"]["system"] is None
    assert held["extraction"]["entities"]["role"] is None
    assert held["resolved"]["system_name"] is None
    draft = db.metadata.get(REQUEST_DRAFT_KEY)
    assert "wiki" not in json.dumps(draft).lower()


def test_a_ticket_beside_a_second_request_is_refused() -> None:
    """Catches a `create_ticket` that is still allowed when a second request
    rides on the message: the round is refused whole and nothing is held to be
    run on a yes. The same ticket with no second request is allowed, so only the
    second-request rule can refuse it."""
    refused = asyncio.run(
        validate_tool_calls(
            _state(Intent.RESET_PASSWORD, Intent.REQUEST_ACCESS, proposed(RESET), _ticket()),
            _NO_DEPS,
        )
    )
    alone = asyncio.run(
        validate_tool_calls(
            _state(Intent.RESET_PASSWORD, None, proposed(RESET), _ticket()), _NO_DEPS
        )
    )

    assert alone.outcome is TurnOutcome.CONFIRM, "premise: with no second request it is allowed"
    assert [call.tool for call in alone.validated] == [RESET, TICKET]
    assert refused.outcome is TurnOutcome.REFUSE
    assert refused.validated == ()


def test_open_ticket_first_keeps_ticket() -> None:
    """Catches a rule that removes `create_ticket` whenever a second request
    exists, even when the ticket IS the first request: an `open_ticket` turn with
    a second request still allows the ticket, and holds it for a yes."""
    result = asyncio.run(
        validate_tool_calls(_state(Intent.OPEN_TICKET, Intent.REQUEST_ACCESS, _ticket()), _NO_DEPS)
    )

    assert result.outcome is TurnOutcome.CONFIRM
    assert [call.tool for call in result.validated] == [TICKET]
