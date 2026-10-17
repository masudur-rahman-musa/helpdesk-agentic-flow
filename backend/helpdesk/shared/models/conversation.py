"""Chat sessions and their messages.

`sessions` is the only link from a session id to a person, so telemetry may
carry the session and never the user. `messages` carries its own `tenant_id`
so the tenant guard covers it directly. `ordinal` orders the history, because
all messages of one turn share a `created_at`.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.models.base import TenantScoped

MAX_MESSAGE_CHARS = 4000
"""Cap on one message, and the `content` column width. Checked before any row is written."""


class MessageTooLongError(ToolError):
    """The message is empty, or longer than the transcript can hold."""

    code = ErrorCode.VALIDATION


def check_message_content(value: str) -> str:
    """Refuse empty or oversized content; an empty turn would still cost a model call."""
    if not value:
        raise MessageTooLongError("a message must not be empty")
    if len(value) > MAX_MESSAGE_CHARS:
        raise MessageTooLongError(
            f"a message longer than {MAX_MESSAGE_CHARS} characters cannot be stored: {len(value)}"
        )
    return value


class MessageRole(StrEnum):
    """The value is stored and shown in prompts, so the spelling must not change."""

    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class Session(TenantScoped):
    """One conversation. Its id is the token's `sid`, so traces and transcript match."""

    __tablename__ = "sessions"
    __table_args__ = (Index("ix_sessions_tenant_id_user_id", "tenant_id", "user_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The column is `metadata`, but SQLAlchemy reserves that attribute name.
    session_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)


class Message(TenantScoped):
    """One message. Null `citations` means "could not cite", unlike an empty list."""

    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_tenant_id_session_id_ordinal", "tenant_id", "session_id", "ordinal"),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"))
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(String(MAX_MESSAGE_CHARS))
    tool_calls: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    citations: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    ordinal: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)


class SessionConflictError(ToolError):
    """The token's session id already belongs to another tenant's row.

    Only a forged claim or a uuid clash gets here. `INTERNAL`: the caller has nothing to fix.
    """

    code = ErrorCode.INTERNAL
