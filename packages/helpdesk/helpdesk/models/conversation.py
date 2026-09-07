"""The two conversation tables (the design, §10, an earlier change).

`sessions` is one conversation; `messages` is its transcript. the design's
observability rule makes `sessions` the ONLY mapping from a session id to a
user, which is why every span and metric elsewhere may carry the session and
never the person.

Three things here are not the design's literal column list, each for a reason a
later reader will otherwise undo:

`messages.tenant_id` is an earlier change decision B (12c decision B's reasoning,
not re-opened): the ORM guard in `helpdesk.core.db` is per-model, so a child
table without the column is an unguarded cross-tenant read whose only
protection is that every query happens to join its parent. Pinned by
`test_conversation_models.py::test_another_tenants_sessions_and_messages_are_invisible`.

`messages.ordinal` is a table-wide identity integer and the only total order
the history window may use. Postgres's `now()` is TRANSACTION time, so the
user message and the assistant message of one turn carry an identical
`created_at` and no ordering by it can tell them apart. Pinned by
`test_conversation_models.py::test_list_recent_messages_returns_the_last_twenty_oldest_first`,
whose premise assertion proves all twenty-five planted rows share one
timestamp before it asserts the order.

`sessions.metadata` keeps the design's column name while the ATTRIBUTE is
`session_metadata`: `metadata` is SQLAlchemy's own declarative attribute and
cannot be a mapped name. Pinned by
`test_conversation_models.py::test_the_session_metadata_column_is_named_metadata_in_the_database`.

`MAX_MESSAGE_CHARS` and `check_message_content` live here rather than in a
second naming module because the cap IS the `content` column width — the same
arrangement `helpdesk.rag.naming` has with `policy_documents`, one number
shared by the check and the column it guards, so widening one without the
other cannot happen.
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

from helpdesk.errors import ErrorCode, ToolError
from helpdesk.models.base import TenantScoped

MAX_MESSAGE_CHARS = 4000
"""The cap on one message's content, and the `messages.content` column width.

A chat turn, not a document: the corpus has its own path and its own caps.
The value bounds a string that reaches the gateway inside a prompt, so it is
pre-checked before any row is written rather than left to the column, which
would raise half-way through a turn that has already stored the session.
a project rule; the boundary pair is pinned by
`test_conversation_models.py::test_check_message_content_accepts_content_of_exactly_the_cap`
and `::test_check_message_content_refuses_one_character_over_the_cap`.
"""


class MessageTooLongError(ToolError):
    """The message is empty, or longer than the transcript can hold.

    One type for both ends: from a caller's side they are the same answer —
    fix the input, the same request cannot succeed unchanged. `VALIDATION`,
    pinned by `test_conversation_models.py::test_the_message_error_is_a_typed_tool_error`.
    """

    code = ErrorCode.VALIDATION


def check_message_content(value: str) -> str:
    """The accepted content, or a typed refusal before any effect runs.

    The empty string is refused as well as an oversized one: an empty turn
    would be stored and then sent to the model as a message with no content,
    which is a prompt the gateway bills for and no user asked for. Pinned by
    `test_conversation_models.py::test_check_message_content_refuses_the_empty_string`.
    """
    if not value:
        raise MessageTooLongError("a message must not be empty")
    if len(value) > MAX_MESSAGE_CHARS:
        raise MessageTooLongError(
            f"a message longer than {MAX_MESSAGE_CHARS} characters cannot be stored: {len(value)}"
        )
    return value


class MessageRole(StrEnum):
    """the design's `messages.role` vocabulary. Exactly three, closed.

    The VALUE is what is stored and what the history window renders into a
    prompt, so the spelling is the contract. Pinned by
    `test_conversation_models.py::test_message_role_has_exactly_the_three_spec_members`.
    """

    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class Session(TenantScoped):
    """One conversation (the design).

    The primary key is the token's `sid` claim, minted fresh at login and
    passed in explicitly rather than defaulted here: the id the gateway sees
    as `X-Session` and the id this row is stored under have to be one value,
    or a trace and a transcript name different conversations. Pinned by
    `test_conversation_models.py::test_get_or_create_session_creates_the_row_under_the_bound_tenant`.

    `started_at` is written once; `last_seen_at` moves on every turn, which is
    what makes an idle conversation findable at all.
    """

    __tablename__ = "sessions"
    __table_args__ = (Index("ix_sessions_tenant_id_user_id", "tenant_id", "user_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # the design names the column `metadata`; the attribute cannot be, because
    # `Base.metadata` is SQLAlchemy's own.
    session_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)


class Message(TenantScoped):
    """One turn of one conversation (the design).

    `tool_calls` and `citations` are nullable: most messages have neither, and
    an empty JSON array stored for every user turn would say "this turn cited
    nothing" where the truth is "this turn could not cite".
    """

    __tablename__ = "messages"
    __table_args__ = (
        # Leads on (tenant_id, session_id, ordinal): every read of this table
        # is one session's window, taken newest-first off the tail.
        Index("ix_messages_tenant_id_session_id_ordinal", "tenant_id", "session_id", "ordinal"),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"))
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(String(MAX_MESSAGE_CHARS))
    tool_calls: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    citations: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)
    ordinal: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)


class SessionConflictError(ToolError):
    """The session id in the token is already a row belonging to ANOTHER tenant.

    `sessions.id` is the table-wide primary key and the token's `sid`, so the
    two ways here are a uuid4 collision and a forged claim. `INTERNAL` rather
    than `VALIDATION` or `FORBIDDEN`: the caller supplied nothing it could
    fix, and the tenant guard has already refused to hand over the foreign
    row, so the only thing left to say is that this turn cannot proceed.
    `get_or_create_session` never loads or overwrites the other tenant's row.
    """

    code = ErrorCode.INTERNAL
