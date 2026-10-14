"""Request and reply models for the chat routes (the design)."""

import uuid
from typing import Any

from pydantic import BaseModel


class ChatRequest(BaseModel):
    """One message. Deliberately NOT `extra="forbid"`.

    A body naming a user, a tenant or a session must be accepted and must
    change nothing: refusing it would answer 422 and prove nothing about where
    identity came from. Pinned by
    `test_chat_route.py::test_identity_comes_from_the_token_and_not_from_the_body`,
    which reads the stored rows rather than the status.

    The message is not length-checked here either: `run_turn` runs
    `check_message_content` before any row, any model call and any MCP
    handshake, and a second cap on this model would be a second number to
    drift from `MAX_MESSAGE_CHARS` (a project rule's "one gate, one
    constant").
    """

    message: str


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatHistoryResponse(BaseModel):
    messages: list[ChatMessage]


class ChatResponse(BaseModel):
    """What one turn answers with, paused or finished.

    `request_id` is the access request a PAUSED turn is waiting on, and None on
    every other outcome: it is what lets a client show the user what they are
    waiting for without reading a checkpoint (decision E1).

    `decision` is this turn's decision card, or null where the turn decided
    nothing about access. It rides as the card's own
    JSON rather than as the model, because the body below is dumped in python
    mode and a uuid or a datetime is not something `JSONResponse` can write.
    """

    reply: str | None
    outcome: str
    citations: list[dict[str, Any]]
    request_id: str | None
    decision: dict[str, Any] | None


class _Identity(BaseModel):
    """The three ids one request acts under, all read off the verified token."""

    tenant_id: uuid.UUID
    user_id: uuid.UUID
    session_id: uuid.UUID
