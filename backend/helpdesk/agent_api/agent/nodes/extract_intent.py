"""`extract_intent`: sort the message into a fixed set of intents.

One structured call on the default model: a closed label set with a schema is
the cheap case. The client validates the answer and retries once. The state
records the extraction prompt's digest as `prompt_version`. The node never
raises: a failure ends the turn as state with a typed code. History and the
message go in as `user` data, never into the system prompt.

The model fills only what the latest message says. Code lays that over the
saved request draft (`request_draft.py`), leaves the result to save
(`save_request_draft`), and every later step reads the merged extraction. A
clear follow-up ("say it shorter") skips resolving, search and tools: it goes
straight to the reply, or ends on the cannot-place line when there is no
earlier reply to reshape (`test_follow_up.py`).
"""

from helpdesk.agent_api.agent import gates
from helpdesk.agent_api.agent.clarify_cap import QuestionKind
from helpdesk.agent_api.agent.follow_up import CANNOT_PLACE_REPLY, last_assistant_reply
from helpdesk.agent_api.agent.prompts import load_prompt
from helpdesk.agent_api.agent.request_draft import draft_dump, merge_draft
from helpdesk.agent_api.agent.required_fields import Ask, AskStep, next_ask
from helpdesk.agent_api.agent.state import Intent, IntentExtraction, TurnOutcome, TurnState
from helpdesk.agent_api.agent.turn_deps import TurnDeps
from helpdesk.agent_api.services.model_gateway.client import ModelAlias
from helpdesk.shared.core.errors import ToolError
from helpdesk.shared.crud.sessions import set_request_draft

SYSTEM_PROMPT = "system"
EXTRACTION_PROMPT = "intent_extraction"

DEFAULT_CLARIFYING_QUESTION = (
    "I want to be sure I get this right. Could you say which system or "
    "account you mean, and what you need to do with it?"
)
"""Used when the check fires but the model wrote no question, which happens
when only an entity is missing."""

FAILURE_REPLY = (
    "Sorry, I could not work out what you need just now. Please try saying it "
    "again, or ask me to open a ticket so a person can pick it up."
)
"""The fixed reply a failed extraction ends on. The model is what failed, so
none of its output is shown; the code rides on the state as `error_code`."""


async def save_request_draft(state: TurnState, deps: TurnDeps) -> None:
    """Store the merged draft the node left on the state. The graph runs this
    right after the node, so a failed extraction (no merge) saves nothing and
    the chat keeps the draft it had."""
    if state.draft_to_save is not None:
        await set_request_draft(
            deps.db, session_id=state.session_id, value=draft_dump(state.draft_to_save)
        )


async def extract_intent(state: TurnState, deps: TurnDeps) -> TurnState:
    """Classify the message and record the outcome. Never raises."""
    prompt = load_prompt(EXTRACTION_PROMPT)
    system = load_prompt(SYSTEM_PROMPT)

    messages = [
        {"role": "system", "content": system.text},
        {"role": "system", "content": prompt.text},
        *({"role": "user", "content": line} for line in state.history),
        {"role": "user", "content": state.user_message},
    ]

    try:
        answer = await deps.model.complete_json(
            IntentExtraction,
            messages,
            alias=ModelAlias.AGENT_DEFAULT,
            tenant_slug=state.tenant_slug,
            session_id=str(state.session_id),
        )
    except ToolError as exc:
        return state.model_copy(
            update={
                "prompt_version": prompt.sha256,
                "outcome": TurnOutcome.FAILED,
                "error_code": exc.code,
                "reply": FAILURE_REPLY,
            }
        )

    extraction, draft = merge_draft(state.request_draft, answer.value, now=deps.now())
    state = state.model_copy(update={"draft_to_save": draft})
    tokens_used = state.tokens_used + answer.usage.total_tokens

    if (
        extraction.intent is Intent.FOLLOW_UP
        and extraction.confidence >= gates.CONFIDENCE_THRESHOLD
    ):
        # Nothing to ask or look up: reshape the last reply, or say what is possible.
        has_reply = last_assistant_reply(state.history) is not None
        return state.model_copy(
            update={
                "extraction": extraction,
                "prompt_version": prompt.sha256,
                "tokens_used": tokens_used,
                "outcome": TurnOutcome.ANSWER if has_reply else TurnOutcome.STOP,
                "reply": None if has_reply else CANNOT_PLACE_REPLY,
            }
        )

    asked = _ask_by_the_rule(state, extraction)
    if asked is not None:
        return state.model_copy(
            update={
                "extraction": extraction,
                "prompt_version": prompt.sha256,
                "tokens_used": tokens_used,
                "outcome": TurnOutcome.STOP if asked.step is AskStep.STOP else TurnOutcome.CLARIFY,
                "reply": asked.reply,
                "asks_made": asked.asks,
            }
        )

    outcome = gates.extraction_outcome(extraction, state.asked_before)
    if outcome is TurnOutcome.CLARIFY:
        return state.model_copy(
            update={
                "extraction": extraction,
                "prompt_version": prompt.sha256,
                "tokens_used": tokens_used,
                "outcome": TurnOutcome.CLARIFY,
                "reply": _clarifying_reply(extraction),
            }
        )

    return state.model_copy(
        update={
            "extraction": extraction,
            "prompt_version": prompt.sha256,
            "tokens_used": tokens_used,
            "outcome": outcome,
            "best_reading": gates.needs_clarification_at_extraction(extraction),
        }
    )


def _ask_by_the_rule(state: TurnState, extraction: IntentExtraction) -> Ask | None:
    """A clear intent missing something the intent step needs asks by the
    required-field rule: the normal question, then why it is required, then
    stop. An unclear intent keeps the model's question and the one-question
    cap (`test_question_kinds.py`)."""
    if extraction.confidence < gates.CONFIDENCE_THRESHOLD:
        return None
    missing = gates.missing_at_extraction(extraction)
    if not missing:
        return None
    return next_ask(
        missing, asks=state.asks, details_asked=state.asked_kind is QuestionKind.DETAILS
    )


def _clarifying_reply(extraction: IntentExtraction) -> str:
    """The model's own question for an unclear intent; a clear one missing
    something is asked by the rule before this."""
    return extraction.clarifying_question or DEFAULT_CLARIFYING_QUESTION
