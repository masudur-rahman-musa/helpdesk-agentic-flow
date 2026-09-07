"""The turn's typed state and the schema the model answers with (the design).

Two kinds of thing live here and the difference matters. `IntentExtraction`
and the models under it are the SCHEMA sent to the gateway: their field names
and their JSON shape are a contract with a model, so a rename changes what a
model may answer. `TurnState` is this process's own record of the turn, which
LangGraph carries from node to node and never decides anything about.

Everything is frozen. A node produces the next state rather than editing the
one it was handed, so a decision recorded on the state — the outcome, the
extraction, the resolved ids — cannot be quietly rewritten by a later node,
and a state passed to a pure predicate cannot come back changed. That is what
makes the design's boundary rule enforceable: `gates.py` reads a value off a
frozen object, and the conditional edge reads the field the node wrote.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from helpdesk.errors import ErrorCode
from pydantic import BaseModel, ConfigDict


class Intent(StrEnum):
    """the design's closed label set. Exactly thirteen, in the spec's order.

    The VALUE is what the gateway's JSON schema encloses and what the model
    may answer with, so the spelling is the contract and not a style choice.
    Pinned by `test_agent_gates.py::test_intent_has_exactly_the_thirteen_spec_members`
    against a literal set, and
    `test_agent_prompts.py::test_the_intent_prompt_names_every_intent_the_enum_carries`
    fails when a member is added here and never described to the model.
    """

    RESET_PASSWORD = "reset_password"
    UNLOCK_ACCOUNT = "unlock_account"
    REQUEST_ACCESS = "request_access"
    REVOKE_ACCESS = "revoke_access"
    REPORT_MOVER = "report_mover"
    ONBOARD = "onboard"
    OFFBOARD = "offboard"
    OPEN_TICKET = "open_ticket"
    ASK_POLICY = "ask_policy"
    CHECK_MY_ACCESS = "check_my_access"
    APPROVALS = "approvals"
    REVIEW = "review"
    OTHER = "other"


class IntentEntities(BaseModel):
    """What the message named, in the user's own words (the design step 2).

    Every field is optional because a message that names nothing is the
    ordinary case, and an absent field is what `gates.missing_required_entities`
    reads. `system` and `role` are deliberately NOT resolved here: the prompt
    asks the model to copy the user's wording, and `matching.py` resolves it
    against the tenant's directory afterwards. A model that normalised the
    name itself would be guessing, which the design step 3 forbids.
    """

    model_config = ConfigDict(frozen=True)

    system: str | None = None
    role: str | None = None
    target_user: str | None = None
    department: str | None = None
    expiry_days: int | None = None
    justification: str | None = None


class IntentExtraction(BaseModel):
    """One classification of one message — the schema `complete_json` sends.

    `ModelClient.complete_json` builds the request's JSON schema from this
    model and validates the answer against the same one, so the shape asked
    for and the shape accepted cannot drift apart.
    """

    model_config = ConfigDict(frozen=True)

    intent: Intent
    entities: IntentEntities
    confidence: float
    clarifying_question: str | None = None


class TurnOutcome(StrEnum):
    """Where the turn stands after the node that just ran.

    The conditional edges read THIS and nothing else (the design): a node
    computes the branch with framework-free code and records it here, so the
    routing function has nothing left to decide.

    `DONE` is an earlier change's sixth member and means "stop looping, compose": the
    round that proposed nothing, and the budget spent by a turn that already
    called something (decision C). It exists as its own member rather than
    reusing `CONTINUE` because the edge after validation cannot otherwise tell
    an empty round from a round with calls to execute, and a turn with nothing
    left to do would execute an empty list and plan again until the recursion
    limit aborted it. Exactly six members, pinned by
    `test_agent_gates.py::test_turn_outcome_has_exactly_the_six_members`.
    """

    CONTINUE = "continue"
    CLARIFY = "clarify"
    DISAMBIGUATE = "disambiguate"
    REFUSE = "refuse"
    FAILED = "failed"
    DONE = "done"


class ResolvedEntities(BaseModel):
    """The directory rows the user's wording actually named.

    Ids and slugs together: the id is what a later tool call takes, the slug
    is what a reply and an audit line can be read by. Both halves come from
    the caller's OWN tenant, because the candidate read is tenant-scoped.
    """

    model_config = ConfigDict(frozen=True)

    system_id: uuid.UUID | None = None
    system_slug: str | None = None
    role_id: uuid.UUID | None = None
    role_slug: str | None = None


class Citation(BaseModel):
    """One cited policy chunk, exactly as `search_policy` puts it on the wire.

    The seven fields and no eighth: the design's six plus `title`, which exists
    because a citation the UI cannot render is a wrong answer (15a decision E).
    The dense cosine the refusal threshold reads is deliberately NOT here — it
    never crosses the tool boundary, so it can never reach a prompt.

    Frozen, like everything else on the state: 17c composes a reply FROM these
    and must not be able to edit one on the way. Pinned as an exact field set
    against the server's own advertised output schema by
    `test_agent_tool_catalogue.py::test_the_citation_fields_are_the_ones_the_server_returns`.
    """

    model_config = ConfigDict(frozen=True)

    document: str
    title: str
    version: str
    heading_path: tuple[str, ...]
    chunk_id: uuid.UUID
    text: str
    score: float


class ProposedCall(BaseModel):
    """One tool call the MODEL asked for, exactly as it answered.

    Unchecked by construction: the tool name may be invented and the arguments
    may be anything JSON can hold. Nothing on this object may be executed —
    `validate_tool_calls` turns an accepted round into `ValidatedCall`s, and a
    round with one bad proposal produces none at all.

    `arguments` deliberately carries NO `idempotency_key`: the planner-facing
    schema omits the field, so a model that names one is proposing an unknown
    argument. Pinned by
    `test_agent_planning.py::test_a_proposal_that_names_its_own_idempotency_key_is_rejected`.
    """

    model_config = ConfigDict(frozen=True)

    tool: str
    arguments: dict[str, Any]


class ToolPlan(BaseModel):
    """One planning round's answer — the schema `plan_tools` constrains.

    `ModelClient.complete_json` builds the request's JSON schema from this
    model and validates the answer against the same one, so what the model is
    asked for and what is accepted cannot drift. An empty `calls` list is the
    ordinary end of a turn: a planner with nothing left to do proposes
    nothing, and `validate_tool_calls` accepts that round
    (`test_agent_planning.py::test_an_empty_round_is_accepted_and_validates_nothing`).
    """

    model_config = ConfigDict(frozen=True)

    calls: list[ProposedCall] = []


class ValidatedCall(BaseModel):
    """One call the agent is prepared to make, arguments and all.

    The difference from `ProposedCall` is the whole of an earlier change's validation:
    the tool is catalogued AND in the intent's `INTENT_TOOLS` row, the
    arguments validated against that tool's model, and a mutating call's
    `idempotency_key` is IN `arguments` — derived by the agent from the turn's
    own values (decision D), never supplied by the model.
    """

    model_config = ConfigDict(frozen=True)

    tool: str
    arguments: dict[str, Any]


class ToolResult(BaseModel):
    """What one executed call answered. Written by an earlier change, carried here.

    The field lands with 17c although nothing fills it, because the planning
    prompt is shaped around "what the tools have said so far": adding it a
    plan later would change the prompt, its digest and every planning test
    written against it.
    """

    model_config = ConfigDict(frozen=True)

    tool: str
    ok: bool
    payload: dict[str, Any] = {}
    error_code: ErrorCode | None = None


class TurnState(BaseModel):
    """Everything one turn knows, carried between nodes.

    `tenant_id`, `user_id` and `session_id` come from the verified token and
    from nothing else; no node reads an identity out of the
    message or out of the extracted entities.

    `history` is already rendered to `"{role}: {content}"` lines rather than
    carrying ORM rows: state crosses a checkpoint boundary in an earlier change, and a
    detached SQLAlchemy instance is not something to checkpoint.
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: uuid.UUID
    user_id: uuid.UUID
    session_id: uuid.UUID
    tenant_slug: str
    user_message: str
    history: tuple[str, ...] = ()
    extraction: IntentExtraction | None = None
    prompt_version: str | None = None
    outcome: TurnOutcome = TurnOutcome.CONTINUE
    reply: str | None = None
    options: tuple[str, ...] = ()
    resolved: ResolvedEntities | None = None
    citations: tuple[Citation, ...] = ()
    error_code: ErrorCode | None = None
    proposed: tuple[ProposedCall, ...] = ()
    validated: tuple[ValidatedCall, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()
    tool_calls_made: int = 0
    tokens_used: int = 0
    started_at: datetime | None = None
    round_number: int = 0
    planning_prompt_version: str | None = None
