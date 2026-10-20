"""`extract_intent`: sort the message into a fixed set of intents.

One structured call on the default model: a closed label set with a schema is
the cheap case. The client validates the answer and retries once. The state
records the extraction prompt's digest as `prompt_version`. The node never
raises: a failure ends the turn as state with a typed code. History and the
message go in as `user` data, never into the system prompt.
"""

from helpdesk.agent_api.agent import gates
from helpdesk.agent_api.agent.clarify_cap import details_question
from helpdesk.agent_api.agent.prompts import load_prompt
from helpdesk.agent_api.agent.state import Intent, IntentExtraction, TurnOutcome, TurnState
from helpdesk.agent_api.agent.turn_deps import TurnDeps
from helpdesk.agent_api.services.model_gateway.client import ModelAlias
from helpdesk.shared.core.errors import ToolError

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

    extraction = answer.value
    tokens_used = state.tokens_used + answer.usage.total_tokens

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


def _clarifying_reply(extraction: IntentExtraction) -> str:
    """A confident request with no system asks for the system in code words;
    the length and reason wait until the role is found. Anything else keeps
    the model's own question."""
    confident = extraction.confidence >= gates.CONFIDENCE_THRESHOLD
    if confident and extraction.intent is Intent.REQUEST_ACCESS:
        return details_question(gates.missing_at_extraction(extraction), length_missing=False)
    return extraction.clarifying_question or DEFAULT_CLARIFYING_QUESTION
