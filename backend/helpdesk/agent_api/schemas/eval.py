"""Request and answer models for the eval routes the companion console calls."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from helpdesk.shared.models.conversation import MAX_MESSAGE_CHARS


class EvalConversationRequest(BaseModel):
    """One turn of a conversation case. Identity is looked up from `tenant` and
    `principal_user` as a seat in the seeded data; nothing else in the body names
    a person. `chat_id` is null on the first turn and the answered id after."""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(pattern=r"^case-[0-9]{3}$")
    tenant: str
    principal_user: str
    chat_id: uuid.UUID | None = None
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class EvalConversationCall(BaseModel):
    """One tool call of a turn and what it answered. A refused call has `ok` false,
    its typed `error_code` and an empty `result`."""

    model_config = ConfigDict(extra="forbid")

    name: str
    args: dict[str, Any]
    ok: bool
    result: dict[str, Any]
    error_code: str | None


class WorldRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: int
    requester: str
    target: str
    role: str
    status: str


class WorldGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    person: str
    role: str
    status: str
    expires_at: datetime | None


class WorldTicket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: int
    person: str
    kind: str
    status: str


class WorldPerson(BaseModel):
    model_config = ConfigDict(extra="forbid")

    person: str
    department: str | None
    status: str
    reset_asked: bool
    unlocked: bool


class WorldReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    items_decided: int


class EvalWorld(BaseModel):
    """The case tenant's state. A seeded person is `<department lowercased>.<index>`,
    anyone added since is their email; a role is `<system>.<role>`."""

    model_config = ConfigDict(extra="forbid")

    requests: list[WorldRequest]
    grants: list[WorldGrant]
    tickets: list[WorldTicket]
    people: list[WorldPerson]
    reviews: list[WorldReview]


class EvalConversationAnswer(BaseModel):
    """One turn: the chat to send next time, the reply, its tool calls and the world
    before and after it."""

    model_config = ConfigDict(extra="forbid")

    chat_id: str
    reply: str
    tool_calls: list[EvalConversationCall]
    world_before: EvalWorld
    world_after: EvalWorld
