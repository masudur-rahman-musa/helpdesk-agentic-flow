"""Request and answer models for the eval routes the companion console calls."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EvalMessage(BaseModel):
    """One message of a golden case. Only the user's are replayed."""

    role: str
    content: str


class EvalTurnRequest(BaseModel):
    """One golden case. Identity is looked up from `tenant` and `principal_user`
    as a seat in the seeded data; nothing else in the body names a person."""

    case_id: str
    tenant: str
    principal_user: str
    messages: list[EvalMessage]


class EvalToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    args: dict[str, Any]


class EvalEnvelope(BaseModel):
    """Exactly the console's envelope: the console rejects any other field."""

    model_config = ConfigDict(extra="forbid")

    intent: str | None
    tool_calls: list[EvalToolCall]
    decision: str | None
    risk_class: str | None
    refused: bool
    clarified: bool
    citations: list[str]
    reply: str
    latency_ms: int
    cost_usd: str
    provider: str
    region: str


class EvalConversationRequest(BaseModel):
    """One conversation case: the person's messages only, played in one new chat.
    Identity is looked up from `tenant` and `principal_user` as a seat, as for
    `EvalTurnRequest`."""

    case_id: str = Field(pattern=r"^conv-[0-9]{3}$")
    tenant: str
    principal_user: str
    turns: list[str] = Field(min_length=1, max_length=10)


class EvalConversationCall(BaseModel):
    """One tool call of a turn and what it answered. A refused call has `ok` false,
    its typed `error_code` and an empty `result`."""

    model_config = ConfigDict(extra="forbid")

    name: str
    args: dict[str, Any]
    ok: bool
    result: dict[str, Any]
    error_code: str | None


class EvalConversationTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn: int
    reply: str
    tool_calls: list[EvalConversationCall]


class EvalConversationAnswer(BaseModel):
    """One record per user turn, counted from 1, in the order they were played."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    turns: list[EvalConversationTurn]
