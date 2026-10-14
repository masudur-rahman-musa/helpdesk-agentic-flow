"""`compose_reply`: the turn said in plain words (the design step 8).

At most TWO gateway calls, under the `agent-default` alias (17d decisions D
and E; an earlier change's post-check is the second).
The decision was made by the policy engine and by the tools that ran; this call
writes it down. A stronger alias is paid on every turn that gets this far for
judgment that has already happened, and the decision-word post-check — not a
bigger model — is what catches a composition contradicting the engine. Plain
`complete` rather than `complete_json`, because the reply is prose: a JSON
wrapper buys no structure and the `ModelAnswer` is what the token budget reads.
Both pinned by
`test_agent_compose.py::test_the_composition_makes_exactly_one_call_under_the_default_alias`.

**A reply may not claim a decision the turn did not return** (the design step
8). Every decision word in the answer is checked against `postcheck.
permitted_words`, which reads the turn's SUCCESSFUL tool results and its
outcome. A contradiction is regenerated ONCE with the permitted words quoted as
data, and a second contradiction ends on `POSTCHECK_FALLBACK_REPLY` rather than
on prose nothing could vouch for — the answer the user sees is never the one
the check refused. Pinned by
`::test_a_contradicting_reply_is_regenerated_once_and_only_the_second_is_stored`
and `::test_a_second_contradicting_reply_ends_the_turn_on_the_template`; that
the ordinary answer still costs one call is pinned by
`::test_a_permitted_reply_is_stored_unchanged_after_a_single_call`.

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

**Every composed reply is pre-checked before any row is written** (standing
rule 1), the regenerated one exactly as the first — ONE gate against ONE
constant, at both call sites. `check_message_content` gates
it against the `messages.content` column's own cap, so a model that answered
past `MAX_MESSAGE_CHARS` ends the turn as state rather than raising inside the
turn's transaction with the row already added. The inclusive boundary is pinned
from both sides on both paths by
`::test_a_composed_reply_of_exactly_the_cap_is_stored`,
`::test_a_composed_reply_one_character_over_the_cap_writes_no_row_at_all`,
`::test_a_regenerated_reply_of_exactly_the_cap_is_stored` and
`::test_a_regenerated_reply_one_character_over_the_cap_writes_no_row_at_all`.

The node does not raise, on either call. A gateway that cannot answer, and a
reply too long to store, both end the TURN as state with a typed code and the
templated reply, and neither writes a row: the templated reply is `fail_turn`'s
to store, and a node that wrote one as well would leave two assistant messages
for one question (`::test_a_gateway_failure_ends_the_turn_and_leaves_no_partial_reply`,
and on the regeneration
`::test_a_gateway_failure_on_the_regeneration_ends_the_turn_with_no_row`).

BOTH calls' `total_tokens` are ADDED to `tokens_used` (criterion 8): the spend
happened whether or not the prose it bought was usable, and a turn that
understated itself by a whole call could pass `MAX_TURN_TOKENS` having spent
past it.
"""

import json
from typing import Any

from helpdesk.agent_api.agent.errors import PLAIN_ERRORS
from helpdesk.agent_api.agent.nodes.extract_intent import FAILURE_REPLY, SYSTEM_PROMPT
from helpdesk.agent_api.agent.postcheck import (
    POSTCHECK_FALLBACK_REPLY,
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
"""The hard bound on gateway calls from this node, ever (criterion 6).

Compose, and regenerate once when the answer contradicts what the turn
returned. A third attempt would multiply this node's latency and its bill on
turns that are already going badly, and a model that contradicted itself twice
running is not one a third prompt fixes — the template is. a project rule
pins the value (`test_agent_compose.py::test_the_composition_bound_is_exactly_two_attempts`),
and the staged gateway refuses a third request, so the bound holds from both
sides.
"""


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


def _regeneration_note(state: TurnState) -> dict[str, str]:
    """The one message the second attempt adds, quoting what may be claimed.

    A `user` message and never `system` (decision D): these are this service's
    own words about this turn, and a `system` message assembled per turn is
    the shape the design's data-quoting rule exists against. The permitted
    words are sorted so the added message is deterministic for a given turn.

    Re-sending the identical prompt instead would mostly reproduce the same
    contradiction and spend the second call reaching the same fallback, which
    is what `test_agent_compose.py`'s
    `::test_the_regeneration_names_the_permitted_words_as_data_and_adds_no_system_message`
    fails on: with nothing added, the second request is the first one again.
    """
    return _as_data(
        "your previous answer used a decision word this turn did not return; "
        "write it again, and of the words approved, granted, blocked and "
        "pending use only these",
        sorted(permitted_words(state)),
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

    spent = 0
    reply = POSTCHECK_FALLBACK_REPLY
    postcheck = PostCheckOutcome.FALLBACK

    try:
        for attempt in range(COMPOSITION_ATTEMPTS):
            if attempt:
                messages = [*messages, _regeneration_note(state)]
            answer = await deps.model.complete(
                messages,
                alias=ModelAlias.AGENT_DEFAULT,
                tenant_slug=state.tenant_slug,
                session_id=str(state.session_id),
            )
            spent += answer.total_tokens
            candidate = check_message_content(answer.text)
            if not contradicts(candidate, state):
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
