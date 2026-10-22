"""The turn's typed state, and the schemas the model answers with. All
frozen, so a node returns a new state and a recorded decision cannot change."""

import json
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, WithJsonSchema

from helpdesk.shared.core.errors import ErrorCode


class Intent(StrEnum):
    """The fixed set of intents. The values go into the model's JSON schema,
    so their spelling is the contract."""

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
    ASK_CAPABILITIES = "ask_capabilities"
    APPROVALS = "approvals"
    REVIEW = "review"
    FOLLOW_UP = "follow_up"
    OTHER = "other"


class IntentEntities(BaseModel):
    """What the message named, as typed. Nullable but required, as strict mode
    needs; `matching.py` resolves names later so the model never guesses."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    system: str | None
    role: str | None
    target_user: str | None
    department: str | None
    expiry_days: int | None
    justification: str | None


class IntentExtraction(BaseModel):
    """One classification; the request schema is built from this model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    intent: Intent
    entities: IntentEntities
    confidence: float
    clarifying_question: str | None
    second_request: Intent | None
    """A second request in the same message, named by its intent. The turn
    handles the first; code says the second needs its own message."""


class RequestDraft(BaseModel):
    """The request the chat is building, kept on the chat row between messages
    (`draft.py`) and written by the model through `note_request`. Role and
    person stay as words, so each turn still finds their ids by lookup."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: str | None = Field(default=None, max_length=200)
    person: str | None = Field(default=None, max_length=200)
    reason: str | None = Field(default=None, max_length=500)
    days: int | None = Field(default=None, ge=1, le=365)
    saved_at: datetime


class TurnOutcome(StrEnum):
    """Where the turn stands; graph edges read only this. `DONE` means reply
    now; `AWAIT_APPROVAL` means paused, read back later from a checkpoint.
    `CONFIRM` shows "I'll file this: ..." before filing; `CONFIRMED` is the yes.
    `ANSWER` skips the tools and composes now: a question was just asked and
    something required is still missing. `STOP` ends in fixed words: a
    required field was asked twice and is still missing."""

    CONTINUE = "continue"
    CLARIFY = "clarify"
    DISAMBIGUATE = "disambiguate"
    REFUSE = "refuse"
    FAILED = "failed"
    DONE = "done"
    AWAIT_APPROVAL = "await_approval"
    CONFIRM = "confirm"
    CONFIRMED = "confirmed"
    ANSWER = "answer"
    STOP = "stop"


class PostCheckOutcome(StrEnum):
    """What the decision-word check did, so fallbacks can be counted. Values
    are saved in checkpoints: do not rename."""

    PASSED = "passed"
    REGENERATED = "regenerated"
    FALLBACK = "fallback"


class ResolvedEntities(BaseModel):
    """The caller's own directory rows: ids for tools, slugs for people.
    `role_name` is the plain name ("Internal Wiki editor", `role_words.py`)
    the summary before filing shows, never the stored `wiki.editor`.
    `other_role_names` are the plain words of the resolved role's siblings on
    its system, sorted by slug, so a reply offers another role only when one
    exists (`next_steps.py`)."""

    model_config = ConfigDict(frozen=True)

    system_id: uuid.UUID | None = None
    system_slug: str | None = None
    system_name: str | None = None
    role_id: uuid.UUID | None = None
    role_slug: str | None = None
    role_name: str | None = None
    other_role_names: tuple[str, ...] = ()


class Citation(BaseModel):
    """One cited policy passage, without the raw score used for refusal, so
    that never reaches a prompt."""

    model_config = ConfigDict(frozen=True)

    document: str
    title: str
    version: str
    heading_path: tuple[str, ...]
    chunk_id: uuid.UUID
    text: str
    score: float


class ProposedCall(BaseModel):
    """One tool call as the model asked, unchecked and never run directly."""

    model_config = ConfigDict(frozen=True)

    tool: str
    arguments: dict[str, Any]


class ToolPlan(BaseModel):
    """One round's proposals; an empty list is the normal end of a turn."""

    model_config = ConfigDict(frozen=True)

    calls: list[ProposedCall] = []


def _decoded_object(value: object) -> dict[str, Any]:
    """`arguments` decoded to a JSON object, or a `ValueError` so the client
    retries once."""
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
"""Arguments differ per tool, and strict mode needs fixed keys, so they
travel as a string and are checked in `validate_tool_calls`."""


class PlannedCallAnswer(BaseModel):
    """One proposed call as the model answers it, with encoded arguments."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    arguments: EncodedArguments


class ToolPlanAnswer(BaseModel):
    """One round's answer; no default, so an empty round is an explicit `[]`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    calls: list[PlannedCallAnswer]


class ComposedReply(BaseModel):
    """The reply writer's answer: the text, and whether it asks the person
    something. The writer marks its own question, so no check reads the text
    for one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    asks_question: bool
    used_passages: list[int]
    """The 1-based numbers of the passages the text relies on."""


class ValidatedCall(BaseModel):
    """A call the agent will make, with any idempotency key derived by the
    agent, never the model."""

    model_config = ConfigDict(frozen=True)

    tool: str
    arguments: dict[str, Any]


class ToolResult(BaseModel):
    """What one executed call answered."""

    model_config = ConfigDict(frozen=True)

    tool: str
    ok: bool
    payload: dict[str, Any] = {}
    error_code: ErrorCode | None = None


class TurnState(BaseModel):
    """Everything one turn knows. Identity comes from the verified token only.
    `history` is text, since the state is saved to checkpoints."""

    model_config = ConfigDict(frozen=True)

    tenant_id: uuid.UUID
    user_id: uuid.UUID
    session_id: uuid.UUID
    tenant_slug: str
    user_message: str
    history: tuple[str, ...] = ()
    principals: tuple[str, ...] = ()
    """The caller's principals from their own directory row, read by
    `load_session`; empty when no row is found. Never from the message."""

    template_role_ids: frozenset[uuid.UUID] = frozenset()
    """The role ids of the caller's own department template, read by
    `load_session` from their directory row; empty when the user or the
    department is not found. A role in it asks no details question."""

    held_role_ids: frozenset[uuid.UUID] = frozenset()
    """The caller's live role ids; read by the graph right before
    `resolve_entities`, and only for a request about the caller."""

    request_draft: RequestDraft | None = None
    """The draft on the chat row as this turn knows it, read at the start and
    changed by `note_request`; `None` when there is none or it is too old."""

    extraction: IntentExtraction | None = None
    prompt_version: str | None = None
    outcome: TurnOutcome = TurnOutcome.CONTINUE
    postcheck: PostCheckOutcome = PostCheckOutcome.PASSED
    """What the decision-word check did. `PASSED` until a reply is composed."""

    reply: str | None = None
    options: tuple[str, ...] = ()
    resolved: ResolvedEntities | None = None
    citations: tuple[Citation, ...] = ()
    reply_citations: tuple[Citation, ...] | None = None
    """The passages the composed reply marked as used; `None` when not known,
    so the Sources box shows every passage (`sources.py`)."""

    error_code: ErrorCode | None = None
    proposed: tuple[ProposedCall, ...] = ()
    validated: tuple[ValidatedCall, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()
    tool_calls_made: int = 0
    tokens_used: int = 0
    started_at: datetime | None = None
    round_number: int = 0
    planning_prompt_version: str | None = None
    confirmed: bool = False
    """The person said yes to the open question, so a create round may run."""

    asked_before: bool = False
    """The turn before asked a clarifying question, so this one may not ask again."""

    asked_options: tuple[str, ...] = ()
    """The options the question this message answers offered, in order; a
    pick ("1", "mail") names one of them."""

    asks: dict[str, int] | None = {}
    """How often each required field was asked in the questions just before
    this message. `None` is an older stored row: each counts as asked once."""

    asks_made: dict[str, int] = {}
    """The asks per field to store with the question this turn ends on."""

    best_reading: bool = False
    """The cap let an unclear message go on, so the reply must ask nothing and
    ends with the correct-me line."""

    awaiting_request_id: uuid.UUID | None = None
    """The request a paused turn waits on, so a resume can refuse a mismatch.
Set by `execute_tools`, since LangGraph drops the pausing node's update."""
