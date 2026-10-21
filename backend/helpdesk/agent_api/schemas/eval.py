"""Request and answer models for the eval routes the companion console calls."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EvalConversationRequest(BaseModel):
    """One conversation case: the person's messages only, played in one new chat.
    Identity is looked up from `tenant` and `principal_user` as a seat in the
    seeded data; nothing else in the body names a person."""

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
