"""`compose_reply`: the turn said in plain words.

The decision is already made; this call only writes it down, so it uses the
default model. A post-check, not a bigger model, catches a reply that
contradicts the result: each decision word must match what the turn returned.
A contradiction is retried once with the allowed words quoted, and so is a
question asked after the request was filed. A second one ends on a fixed
sentence built from the turn's own results.

Everything the turn gathered goes in as `user` data, never `system`, since
it includes tool output and policy text. Failures reach the model as plain
sentences, not codes. Every reply is size-checked before a row is written.
The node never raises: a gateway failure or an oversized reply ends the turn
as state with no row. Tokens from both calls are counted, used or not.

On a best-reading turn (a clarifying question was just asked) a reply that
asks a question is retried once, then replaced by `BEST_READING_FALLBACK`,
and code adds the correct-me line at the end (a design note, pinned by
`test_agent_clarify_cap.py`).

`ask_capabilities` and `other` turns also get what this person can do, one
plain sentence per tool their principals allow, so the answer and the
pointers come from the real matrix, never a written list (a design note, pinned by
`test_agent_capabilities.py`). The requestable rows go in already sorted,
and the roles held are one sentence written by code ahead of the reply, so two
replies cannot disagree about them (`requestable.py`).

A question asked on a request or revoke turn that filed nothing is recorded
like a clarifying question, so the next message gets the best reading, not a
second question (a design note, pinned by `test_agent_clarify_cap.py`).
"""

import json
from dataclasses import asdict
from typing import Any

from helpdesk.agent_api.agent.clarify_cap import BEST_READING_FALLBACK, CORRECT_ME_LINE
from helpdesk.agent_api.agent.errors import PLAIN_ERRORS
from helpdesk.agent_api.agent.gates import REQUIRED_ENTITIES
from helpdesk.agent_api.agent.nodes.extract_intent import FAILURE_REPLY, SYSTEM_PROMPT
from helpdesk.agent_api.agent.postcheck import (
    asks_after_filing,
    asks_on_a_best_reading,
    contradicts,
    outcome_reply,
    permitted_words,
    request_filed,
)
from helpdesk.agent_api.agent.prompts import load_prompt
from helpdesk.agent_api.agent.requestable import held_sentence, ordered_requestable
from helpdesk.agent_api.agent.state import (
    Intent,
    PostCheckOutcome,
    ToolResult,
    TurnOutcome,
    TurnState,
)
from helpdesk.agent_api.agent.turn_deps import TurnDeps
from helpdesk.agent_api.services.model_gateway.client import ModelAlias
from helpdesk.shared.auth.capabilities import capability_lines
from helpdesk.shared.core.errors import ToolError
from helpdesk.shared.crud.messages import append_message
from helpdesk.shared.crud.sessions import set_asked_clarifying
from helpdesk.shared.models.conversation import MessageRole, check_message_content

COMPOSITION_PROMPT = "reply_composition"

COMPOSITION_ATTEMPTS = 2
"""Compose, then retry once on a contradiction. A model wrong twice running
is fixed by the template, not a third try."""


def _as_data(label: str, value: Any) -> dict[str, str]:
    """One labelled `user` message of gathered material, so none of it can pass
    as an instruction."""
    return {"role": "user", "content": f"{label}:\n{json.dumps(value, default=str)}"}


CAPABILITY_INTENTS = frozenset({Intent.ASK_CAPABILITIES, Intent.OTHER})
"""The kinds whose reply names what this person can do. Other kinds stay on
their own data, so a policy question is answered from policy text."""


def _capability_data(state: TurnState) -> list[dict[str, str]]:
    """The person's capability lines as one data message, or nothing when the
    turn is another kind or no principals were found."""
    if state.extraction is None or state.extraction.intent not in CAPABILITY_INTENTS:
        return []
    lines = capability_lines(state.principals)
    if not lines:
        return []
    return [_as_data("what this person can do here, from their real permissions", lines)]


LIST_REQUESTABLE_ROLES = "list_requestable_roles"


def _requestable_rows(state: TurnState) -> list[dict[str, Any]]:
    """Every row the requestable-roles tool answered this turn."""
    return [
        row
        for result in state.tool_results
        if result.ok and result.tool == LIST_REQUESTABLE_ROLES
        for row in result.payload.get("roles", [])
    ]


def _tool_data(state: TurnState) -> list[dict[str, Any]]:
    """Tool results as data, the requestable rows swapped for their sorted
    groups, so the model has no raw list to regroup."""
    return [
        result.model_dump(mode="json")
        for result in state.tool_results
        if not (result.ok and result.tool == LIST_REQUESTABLE_ROLES)
    ]


def _requestable_data(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """The roles this person may still ask for, in the order to say them."""
    if not rows:
        return []
    groups = [asdict(group) for group in ordered_requestable(rows)]
    return [_as_data("roles this person may ask for, template group first, in this order", groups)]


def _with_held(reply: str, rows: list[dict[str, Any]]) -> str:
    """The held sentence ahead of the reply, when this turn listed roles."""
    sentence = held_sentence(rows)
    return f"{sentence}\n\n{reply}" if sentence else reply


def _asks_before_filing(reply: str, state: TurnState) -> bool:
    """Whether `reply` asks a question on a request-like turn that filed
    nothing: that question counts toward the one-question cap."""
    if state.extraction is None or not REQUIRED_ENTITIES[state.extraction.intent]:
        return False
    return "?" in reply and not request_filed(state)


def _failure_sentences(results: tuple[ToolResult, ...]) -> list[str]:
    """One plain sentence per failed call, in call order. No default: an unknown
    code raises instead of reaching the user as a shrug."""
    return [
        PLAIN_ERRORS[result.error_code]
        for result in results
        if not result.ok and result.error_code is not None
    ]


def _regeneration_note(state: TurnState) -> dict[str, str]:
    """The message the retry adds, quoting the allowed words as data.

    Without it the retry would be the same prompt and likely the same mistake.
    Sorted so the message is the same for a given turn.
    """
    return _as_data(
        "your previous answer used a decision word this turn did not return; "
        "write it again, and of the words approved, granted, blocked and "
        "pending use only these",
        sorted(permitted_words(state)),
    )


def _question_note() -> dict[str, str]:
    """The retry message for a reply that asked a question after the request was filed."""
    return _as_data(
        "your previous answer asked a question, but the request is already "
        "filed and nothing more is needed from the person; write it again "
        "as a statement with no question",
        [],
    )


def _best_reading_note() -> dict[str, str]:
    """The retry message for a reply that asked a question right after one was asked."""
    return _as_data(
        "your previous answer asked a question, but the person was just asked "
        "one; answer with your best reading of the message as a statement "
        "with no question",
        [],
    )


def _with_correct_me(reply: str, state: TurnState) -> str:
    """The reply, with the correct-me line added on a best-reading turn."""
    return f"{reply}\n\n{CORRECT_ME_LINE}" if state.best_reading else reply


def _failed(state: TurnState, exc: ToolError) -> TurnState:
    """End the turn as state, with nothing composed anywhere on it."""
    return state.model_copy(
        update={
            "outcome": TurnOutcome.FAILED,
            "error_code": exc.code,
            "reply": FAILURE_REPLY,
        }
    )


async def compose_reply(state: TurnState, deps: TurnDeps) -> TurnState:
    """Say what happened, store it once, and carry it on the state.

    The row is written here because this is the only path whose reply is not a
    template stored by an ending node.
    """
    prompt = load_prompt(COMPOSITION_PROMPT)
    system = load_prompt(SYSTEM_PROMPT)
    rows = _requestable_rows(state)

    messages = [
        {"role": "system", "content": system.text},
        {"role": "system", "content": prompt.text},
        _as_data(
            "intent and entities",
            state.extraction.model_dump(mode="json") if state.extraction else {},
        ),
        _as_data(
            "resolved directory ids",
            state.resolved.model_dump(mode="json") if state.resolved else {},
        ),
        _as_data(
            "policy passages retrieved for this turn",
            [citation.model_dump(mode="json") for citation in state.citations],
        ),
        _as_data("tool results", _tool_data(state)),
        *_requestable_data(rows),
        _as_data("what did not work, in plain words", _failure_sentences(state.tool_results)),
        *_capability_data(state),
        {"role": "user", "content": state.user_message},
    ]

    spent = 0
    reply = BEST_READING_FALLBACK if state.best_reading else outcome_reply(state)
    postcheck = PostCheckOutcome.FALLBACK

    asked = False
    asked_again = False
    try:
        for attempt in range(COMPOSITION_ATTEMPTS):
            if attempt:
                if asked:
                    note = _question_note()
                elif asked_again:
                    note = _best_reading_note()
                else:
                    note = _regeneration_note(state)
                messages = [*messages, note]
            answer = await deps.model.complete(
                messages,
                alias=ModelAlias.AGENT_DEFAULT,
                tenant_slug=state.tenant_slug,
                session_id=str(state.session_id),
            )
            spent += answer.total_tokens
            candidate = check_message_content(_with_correct_me(answer.text, state))
            asked = asks_after_filing(answer.text, state)
            asked_again = asks_on_a_best_reading(answer.text, state)
            if not asked and not asked_again and not contradicts(answer.text, state):
                reply = candidate
                postcheck = (
                    PostCheckOutcome.PASSED if attempt == 0 else PostCheckOutcome.REGENERATED
                )
                break
    except ToolError as exc:
        return _failed(state, exc)
    if postcheck is PostCheckOutcome.FALLBACK:
        reply = _with_correct_me(reply, state)
    reply = check_message_content(_with_held(reply, rows))
    if _asks_before_filing(reply, state):
        await set_asked_clarifying(
            deps.db, session_id=state.session_id, value=deps.now().isoformat()
        )

    await append_message(
        deps.db,
        session_id=state.session_id,
        role=MessageRole.ASSISTANT,
        content=reply,
    )
    return state.model_copy(
        update={
            "reply": reply,
            "postcheck": postcheck,
            "tokens_used": state.tokens_used + spent,
        }
    )
