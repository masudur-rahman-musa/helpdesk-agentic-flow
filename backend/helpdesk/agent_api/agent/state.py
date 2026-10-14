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

import json
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, WithJsonSchema

from helpdesk.shared.core.errors import ErrorCode


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

    Every field is nullable because a message that names nothing is the
    ordinary case, and a null field is what `gates.missing_required_entities`
    reads. Nullable but REQUIRED, with no default and no extra keys: strict
    mode refuses a schema with an optional or defaulted field, so the model
    answers every key, null where the message named nothing
    (`test_agent_strict_schemas.py::test_every_schema_sent_to_complete_json_meets_strict_mode`).
    `system` and `role` are deliberately NOT resolved here: the prompt
    asks the model to copy the user's wording, and `matching.py` resolves it
    against the tenant's directory afterwards. A model that normalised the
    name itself would be guessing, which the design step 3 forbids.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    system: str | None
    role: str | None
    target_user: str | None
    department: str | None
    expiry_days: int | None
    justification: str | None


class IntentExtraction(BaseModel):
    """One classification of one message — the schema `complete_json` sends.

    `ModelClient.complete_json` builds the request's JSON schema from this
    model and validates the answer against the same one, so the shape asked
    for and the shape accepted cannot drift apart. Strict like
    `IntentEntities`: every field required, none defaulted, no extra keys.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    intent: Intent
    entities: IntentEntities
    confidence: float
    clarifying_question: str | None


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
    limit aborted it.

    `AWAIT_APPROVAL` is an earlier change's seventh member and means "this turn is
    paused, not finished": a request the policy engine sent to an approval
    chain is filed, nothing more can be decided in this process, and the state
    sits in Postgres until somebody approves or rejects it. Its own member
    rather than `DONE` because the two are opposite answers to the only
    question the route asks — is there more to come — and because the VALUE is
    what a later process reads back out of a checkpoint.

    Exactly seven members, pinned by
    `test_agent_gates.py::test_turn_outcome_has_exactly_the_seven_members`.
    """

    CONTINUE = "continue"
    CLARIFY = "clarify"
    DISAMBIGUATE = "disambiguate"
    REFUSE = "refuse"
    FAILED = "failed"
    DONE = "done"
    AWAIT_APPROVAL = "await_approval"


class PostCheckOutcome(StrEnum):
    """What the decision-word post-check made the composition do.

    Recorded so a fallback is countable without reading the reply: row 20's
    metrics need a number for "compositions that contradicted the engine", and
    prose is not something to grep at runtime.

    The VALUE is what crosses the checkpointer's serde, so a member renamed
    after a checkpoint was written is a state nothing can read back. Exactly
    three members, pinned by value by
    `test_agent_postcheck.py::test_post_check_outcome_has_exactly_the_three_members_by_value`.
    """

    PASSED = "passed"
    REGENERATED = "regenerated"
    FALLBACK = "fallback"


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
    """One planning round's proposals, as `validate_tool_calls` judges them.

    `plan_tools` builds it from the model's `ToolPlanAnswer`, the strict wire
    shape, so everything downstream of planning sees the objects it always
    did. An empty `calls` list is the ordinary end of a turn: a planner with
    nothing left to do proposes nothing, and `validate_tool_calls` accepts that round
    (`test_agent_planning.py::test_an_empty_round_is_accepted_and_validates_nothing`).
    """

    model_config = ConfigDict(frozen=True)

    calls: list[ProposedCall] = []


def _decoded_object(value: object) -> dict[str, Any]:
    """`arguments` as the object it encodes, or a refusal `complete_json` retries.

    Only a string is accepted, and only one that decodes to a JSON object: an
    array, a string, a number or `null` is a bad answer, not an empty call.
    Raising `ValueError` makes it a `ValidationError`, so the client's one
    retry runs. Pinned by
    `test_agent_strict_schemas.py::test_planned_call_arguments_that_are_not_a_json_object_fail_validation`
    and
    `test_agent_turn.py::test_a_planner_answer_with_non_object_arguments_is_retried_once`.
    """
    if not isinstance(value, str):
        raise ValueError("arguments must be one JSON object encoded as a string")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("arguments is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError("arguments must encode a JSON object")
    return decoded


EncodedArguments = Annotated[
    dict[str, Any],
    BeforeValidator(_decoded_object),
    WithJsonSchema({"type": "string", "description": "One JSON object, encoded as a string."}),
]
"""A tool's arguments as they travel: a string on the wire, a dict once validated.

Strict mode needs every object closed with its keys listed, and a tool's
arguments are an open object whose keys depend on the tool. Sending them as a
string keeps the schema strict and the argument check where it already is,
in `validate_tool_calls`. Decoded to a dict here, so
`plan_tools` hands `ProposedCall` the same object it always received
(`test_agent_strict_schemas.py::test_planned_call_arguments_decode_to_a_dict`).
"""


class PlannedCallAnswer(BaseModel):
    """One proposed call as the model answers it: a tool name and its encoded arguments.

    The wire twin of `ProposedCall`, which keeps its shape. Pinned strict by
    `test_agent_strict_schemas.py::test_every_schema_sent_to_complete_json_meets_strict_mode`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    arguments: EncodedArguments


class ToolPlanAnswer(BaseModel):
    """One planning round's answer — the strict schema `plan_tools` sends.

    `ModelClient.complete_json` builds the request's JSON schema from this
    model and validates the answer against the same one, so what the model is
    asked for and what is accepted cannot drift. No default on `calls`: an
    empty round is the model answering `[]`, not leaving the key out.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    calls: list[PlannedCallAnswer]


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
    postcheck: PostCheckOutcome = PostCheckOutcome.PASSED
    """What the composition's decision-word check did.

    Defaults to `PASSED` so that every state constructed before composition —
    which is every state on every path that never gets there — reads as "no
    contradiction", and no existing construction has to move. Pinned by
    `test_agent_postcheck.py::test_a_turn_records_a_passed_post_check_until_something_says_otherwise`.
    """

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
    awaiting_request_id: uuid.UUID | None = None
    """The access request a paused turn is waiting on.

    the design keys the checkpoint thread by session AND request, which cannot
    be implemented as written: the thread id is fixed when the graph is
    invoked, and the request id only exists after `create_access_request`
    answers mid-run. a design note amends it — the thread is the session, and the
    request the pause is waiting on is carried HERE, so an earlier change's resume can
    refuse a decision whose request id does not match the paused state.

    Written by `execute_tools` from `gates.approval_request_id`, before the
    interrupt rather than by the node that raises it: LangGraph discards the
    interrupting node's own state update, so a value written there would never
    reach the checkpoint a resume reads. Pinned out of Postgres by
    `test_agent_approval_interrupt.py::test_the_paused_state_is_readable_from_a_second_checkpointer`.
    """
