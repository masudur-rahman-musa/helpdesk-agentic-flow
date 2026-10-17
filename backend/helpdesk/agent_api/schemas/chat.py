"""Request and reply models for the chat routes."""

import uuid
from typing import Any

from pydantic import BaseModel


class ChatRequest(BaseModel):
    """One message. Extra fields are allowed on purpose.

    A body naming a user, tenant or session is accepted and changes nothing;
    identity comes from the token. The length check runs once, in `run_turn`.
    """

    message: str


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatHistoryResponse(BaseModel):
    messages: list[ChatMessage]


class ChatResponse(BaseModel):
    """What one turn answers with, paused or finished.

    `request_id` is the request a paused turn waits on, else None. `decision`
    is the turn's decision card as JSON, or None when it decided nothing.
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
