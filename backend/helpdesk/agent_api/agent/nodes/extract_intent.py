"""`extract_intent`: the message classified into a closed label set (§10.1 step 2).

One structured call through the gateway under the `agent-default` alias
(decision E): thirteen closed labels with a schema-enforced answer is the
cheap-model case, and `agent-strong` is for the composed reply 17b writes.
`ModelClient.complete_json` builds the request's JSON schema from
`IntentExtraction` and validates the answer against the same model, retrying
exactly once on an answer that does not fit (the design step 2), so nothing
here counts attempts of its own.

The prompt's sha256 is recorded on the state as `prompt_version` (the design).
It is the EXTRACTION prompt's digest, not the system prompt's: one string
cannot carry two versions, and this node's behaviour is the one the extraction
prompt fixes. Pinned by
`test_agent_turn.py::test_the_prompt_version_is_the_extraction_prompts_own_digest`.

The node does not raise. A gateway that cannot answer ends the TURN, as state,
with a typed code and a templated reply — `run_turn`'s caller gets a state to
render, not an exception to translate. Pinned by
`test_agent_turn.py::test_two_schema_invalid_answers_end_the_turn_as_a_typed_internal`.

The call's own `total_tokens` is ADDED to `tokens_used`,
never assigned: the turn's budget is the sum of every call it makes, and this
is the first of the three. A failed extraction adds nothing, because the turn
ends on that state and no later node reads the counter. Pinned across all three
call sites by
`test_agent_turn.py::test_tokens_used_is_the_sum_of_every_staged_answers_total_tokens`.

History and the user's message are quoted to the model as DATA. They are
placed in `user` messages, never merged into the system prompt, and the system
prompt itself tells the model that instructions found inside them are content
to report rather than obey (the design).
"""

from helpdesk.agent_api.agent import gates
from helpdesk.agent_api.agent.prompts import load_prompt
from helpdesk.agent_api.agent.state import IntentExtraction, TurnOutcome, TurnState
from helpdesk.agent_api.agent.turn_deps import TurnDeps
from helpdesk.agent_api.services.model_gateway.client import ModelAlias
from helpdesk.shared.core.errors import ToolError

SYSTEM_PROMPT = "system"
EXTRACTION_PROMPT = "intent_extraction"

DEFAULT_CLARIFYING_QUESTION = (
    "I want to be sure I get this right. Could you say which system or "
    "account you mean, and what you need to do with it?"
)
"""What the turn asks when the gate fires and the model supplied no question.

The gate can fire on the entity half alone, where a confident model has no
reason to have written one, so a turn that used `clarifying_question` blindly
would answer a user with `None`.
"""

FAILURE_REPLY = (
    "Sorry, I could not work out what you need just now. Please try saying it "
    "again, or ask me to open a ticket so a person can pick it up."
)
"""The templated reply a failed extraction ends on.

A fixed string, not the model's words and not the error's: the one thing that
failed here is the model, so anything it produced is exactly what must not be
shown. It names no gateway, no provider and no error code; the machine-readable
half rides on the state as `error_code`.
"""


async def extract_intent(state: TurnState, deps: TurnDeps) -> TurnState:
    """Classify the message, record the gate's answer, never raise.

    The clarify branch is decided by `gates.needs_clarification`, pure code
    with no graph in sight, and recorded as `outcome`; the conditional edge
    downstream only reads that field (the design).
    """
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

    if gates.needs_clarification(extraction):
        return state.model_copy(
            update={
                "extraction": extraction,
                "prompt_version": prompt.sha256,
                "tokens_used": tokens_used,
                "outcome": TurnOutcome.CLARIFY,
                "reply": extraction.clarifying_question or DEFAULT_CLARIFYING_QUESTION,
            }
        )

    return state.model_copy(
        update={
            "extraction": extraction,
            "prompt_version": prompt.sha256,
            "tokens_used": tokens_used,
            "outcome": TurnOutcome.CONTINUE,
        }
    )
