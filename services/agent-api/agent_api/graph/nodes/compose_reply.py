"""`compose_reply`: the turn said in plain words (the design step 8).

Exactly ONE gateway call, under the `agent-default` alias (decisions D and E).
The decision was made by the policy engine and by the tools that ran; this call
writes it down. A stronger alias is paid on every turn that gets this far for
judgment that has already happened, and row 19's decision-word post-check — not
a bigger model — is what catches a composition contradicting the engine. Plain
`complete` rather than `complete_json`, because the reply is prose: a JSON
wrapper buys no structure and the `ModelAnswer` is what the token budget reads.
Both pinned by
`test_agent_compose.py::test_the_composition_makes_exactly_one_call_under_the_default_alias`.

**Everything the turn gathered is quoted as DATA** (the design). The intent and
entities, the resolved directory ids, the citations, the tool results and each
failure's plain sentence ride in `user` messages; the only `system` messages
are the two prompt files this service ships. This is the one call whose input
includes both a tool's output and somebody else's policy text, and material
merged into a system prompt becomes an instruction the model obeys. Pinned by
`::test_everything_the_turn_gathered_is_quoted_as_data_and_not_as_instructions`.

**A failure reaches the model as a SENTENCE, never as its code** (criterion 6).
`PLAIN_ERRORS` is this service's own words; the tool caller's message is the
server's, and `SOD_CONFLICT` read back to a user is internal vocabulary.
Pinned by `::test_a_failure_reaches_the_prompt_in_plain_words_and_never_as_its_code`.

**The composed reply is pre-checked before any row is written** (standing rule
1). `check_message_content` gates it against the `messages.content` column's own
cap, so a model that answered past `MAX_MESSAGE_CHARS` ends the turn as state
rather than raising inside the turn's transaction with the row already added.
The inclusive boundary is pinned from both sides by
`::test_a_composed_reply_of_exactly_the_cap_is_stored` and
`::test_a_composed_reply_one_character_over_the_cap_writes_no_row_at_all`.

The node does not raise. A gateway that cannot answer, and a reply too long to
store, both end the TURN as state with a typed code and the templated reply,
and neither writes a row: the templated reply is `fail_turn`'s to store, and a
node that wrote one as well would leave two assistant messages for one question
(`::test_a_gateway_failure_ends_the_turn_and_leaves_no_partial_reply`).

The composition's own `total_tokens` is ADDED to `tokens_used` (criterion 8),
the last of the turn's three call sites.
"""

import json
from typing import Any

from helpdesk.core.repository import append_message
from helpdesk.errors import ToolError
from helpdesk.gateway.client import ModelAlias
from helpdesk.models.conversation import MessageRole, check_message_content

from agent_api.graph.deps import TurnDeps
from agent_api.graph.nodes.extract_intent import FAILURE_REPLY, SYSTEM_PROMPT
from agent_api.graph.plain_errors import PLAIN_ERRORS
from agent_api.graph.state import ToolResult, TurnOutcome, TurnState
from agent_api.prompts import load_prompt

COMPOSITION_PROMPT = "reply_composition"


def _as_data(label: str, value: Any) -> dict[str, str]:
    """One `user` message carrying gathered material, labelled and serialised.

    The same shape `plan_tools._as_data` uses, and for the same reason: a
    `user` role and never `system`, with a label that is this service's own
    word so the model can tell the kinds of material apart without any of them
    being able to claim it is an instruction.
    """
    return {"role": "user", "content": f"{label}:\n{json.dumps(value, default=str)}"}


def _failure_sentences(results: tuple[ToolResult, ...]) -> list[str]:
    """One plain sentence per failed call, in the order the calls were made.

    Read through `PLAIN_ERRORS` with no default: a code with no row raises here
    rather than reaching a user as a generic shrug
    (`test_agent_execution.py::test_plain_errors_covers_exactly_the_eight_error_codes`).
    """
    return [
        PLAIN_ERRORS[result.error_code]
        for result in results
        if not result.ok and result.error_code is not None
    ]


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

    The assistant row is written HERE rather than by an ending node: this is
    the only path that produces a reply nothing templated, so a user would
    otherwise see an answer the next turn's history has no record of.
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

    try:
        answer = await deps.model.complete(
            messages,
            alias=ModelAlias.AGENT_DEFAULT,
            tenant_slug=state.tenant_slug,
            session_id=str(state.session_id),
        )
        reply = check_message_content(answer.text)
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
            "tokens_used": state.tokens_used + answer.total_tokens,
        }
    )
