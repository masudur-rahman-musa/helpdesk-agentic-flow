"""Request and answer models for the eval routes the companion console calls."""

from typing import Any

from pydantic import BaseModel, ConfigDict


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
