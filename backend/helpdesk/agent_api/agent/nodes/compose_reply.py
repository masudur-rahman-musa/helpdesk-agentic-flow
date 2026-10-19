"""`compose_reply`: the turn said in plain words.

The decision is already made; this call only writes it down, so it uses the
default model. A post-check, not a bigger model, catches a reply that
contradicts the result: each decision word must match what the turn returned.
A contradiction is retried once with the allowed words quoted, and so is a
question asked after the request was filed. A second one ends on a fixed
template.

Everything the turn gathered goes in as `user` data, never `system`, since
it includes tool output and policy text. Failures reach the model as plain
sentences, not codes. Every reply is size-checked before a row is written.
The node never raises: a gateway failure or an oversized reply ends the turn
as state with no row. Tokens from both calls are counted, used or not.
"""

import json
from typing import Any

from helpdesk.agent_api.agent.errors import PLAIN_ERRORS
from helpdesk.agent_api.agent.nodes.extract_intent import FAILURE_REPLY, SYSTEM_PROMPT
from helpdesk.agent_api.agent.postcheck import (
    POSTCHECK_FALLBACK_REPLY,
    asks_after_filing,
    contradicts,
    permitted_words,
)
from helpdesk.agent_api.agent.prompts import load_prompt
from helpdesk.agent_api.agent.state import PostCheckOutcome, ToolResult, TurnOutcome, TurnState
from helpdesk.agent_api.agent.turn_deps import TurnDeps
from helpdesk.agent_api.services.model_gateway.client import ModelAlias
from helpdesk.shared.core.errors import ToolError
from helpdesk.shared.crud.messages import append_message
from helpdesk.shared.models.conversation import MessageRole, check_message_content

COMPOSITION_PROMPT = "reply_composition"

COMPOSITION_ATTEMPTS = 2
"""Compose, then retry once on a contradiction. A model wrong twice running
is fixed by the template, not a third try."""


def _as_data(label: str, value: Any) -> dict[str, str]:
    """One labelled `user` message of gathered material, so none of it can pass
    as an instruction."""
    return {"role": "user", "content": f"{label}:\n{json.dumps(value, default=str)}"}


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
        _as_data("tool results", [result.model_dump(mode="json") for result in state.tool_results]),
        _as_data("what did not work, in plain words", _failure_sentences(state.tool_results)),
        {"role": "user", "content": state.user_message},
    ]

    spent = 0
    reply = POSTCHECK_FALLBACK_REPLY
    postcheck = PostCheckOutcome.FALLBACK

    asked = False
    try:
        for attempt in range(COMPOSITION_ATTEMPTS):
            if attempt:
                note = _question_note() if asked else _regeneration_note(state)
                messages = [*messages, note]
            answer = await deps.model.complete(
                messages,
                alias=ModelAlias.AGENT_DEFAULT,
                tenant_slug=state.tenant_slug,
                session_id=str(state.session_id),
            )
            spent += answer.total_tokens
            candidate = check_message_content(answer.text)
            asked = asks_after_filing(candidate, state)
            if not asked and not contradicts(candidate, state):
                reply = candidate
                postcheck = (
                    PostCheckOutcome.PASSED if attempt == 0 else PostCheckOutcome.REGENERATED
                )
                break
    except ToolError as exc:
        return _failed(state, exc)

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
