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

Three fixed stops and one fixed blank, by code, never the model. A
reset or unlock keeps no system or role, so none reaches its reply or draft
(`test_reset_and_wiki_opens_no_ticket`). A request for access to everything
ends on the fixed no (`test_everything_stops_with_no`). A request for someone
else ends on the only-for-you line unless the speaker may ask for others
(`test_employee_asking_for_a_friend_is_told_no`).
"""

from helpdesk.agent_api.agent import gates
from helpdesk.agent_api.agent.clarify_cap import QuestionKind
from helpdesk.agent_api.agent.confirmation import only_for_you_reply, other_person
from helpdesk.agent_api.agent.follow_up import CANNOT_PLACE_REPLY, last_assistant_reply
from helpdesk.agent_api.agent.prompts import load_prompt
from helpdesk.agent_api.agent.pushback import EVERYTHING_REPLY, wants_everything
from helpdesk.agent_api.agent.request_draft import draft_dump, merge_draft
from helpdesk.agent_api.agent.required_fields import Ask, AskStep, next_ask
from helpdesk.agent_api.agent.state import Intent, IntentExtraction, TurnOutcome, TurnState
from helpdesk.agent_api.agent.turn_deps import TurnDeps
from helpdesk.agent_api.services.model_gateway.client import ModelAlias
from helpdesk.shared.auth import rbac
from helpdesk.shared.core.config import load_settings
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

_SELF_SERVICE = frozenset({Intent.RESET_PASSWORD, Intent.UNLOCK_ACCOUNT})
"""Intents about the speaker's own account: no system or role belongs on them."""

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
    threshold = load_settings().intent_confidence_min

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

    extraction, draft = merge_draft(
        state.request_draft, _without_systems(answer.value), now=deps.now(), threshold=threshold
    )
    state = state.model_copy(update={"draft_to_save": draft})
    tokens_used = state.tokens_used + answer.usage.total_tokens

    stop = _fixed_stop(state, extraction)
    if stop is not None:
        return state.model_copy(
            update={
                "extraction": extraction,
                "prompt_version": prompt.sha256,
                "tokens_used": tokens_used,
                "outcome": TurnOutcome.STOP,
                "reply": stop,
            }
        )

    if extraction.intent is Intent.FOLLOW_UP and extraction.confidence >= threshold:
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

    asked = _ask_by_the_rule(state, extraction, threshold)
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

    outcome = gates.extraction_outcome(extraction, state.asked_before, threshold=threshold)
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
            "best_reading": gates.needs_clarification_at_extraction(
                extraction, threshold=threshold
            ),
        }
    )


def _without_systems(extraction: IntentExtraction) -> IntentExtraction:
    """A reset or unlock with its system and role blanked; anything else as it is."""
    if extraction.intent not in _SELF_SERVICE:
        return extraction
    entities = extraction.entities.model_copy(update={"system": None, "role": None})
    return extraction.model_copy(update={"entities": entities})


def _fixed_stop(state: TurnState, extraction: IntentExtraction) -> str | None:
    """The fixed reply a request ends on before anything is looked up, or `None`."""
    if extraction.intent is not Intent.REQUEST_ACCESS:
        return None
    if wants_everything(state.user_message):
        return EVERYTHING_REPLY
    person = other_person(extraction.entities)
    if person is not None and rbac.Tool.CREATE_ACCESS_REQUEST_ON_BEHALF not in rbac.tools_for(
        state.principals
    ):
        return only_for_you_reply(person)
    return None


def _ask_by_the_rule(
    state: TurnState, extraction: IntentExtraction, threshold: float
) -> Ask | None:
    """A clear intent missing something the intent step needs asks by the
    required-field rule: the normal question, then why it is required, then
    stop. An unclear intent keeps the model's question and the one-question
    cap (`test_question_kinds.py`)."""
    if extraction.confidence < threshold:
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
