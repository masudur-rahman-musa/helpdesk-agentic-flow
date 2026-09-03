"""The append-only audit event table (the design, §13.1).

`seq` is assigned by the database, not the writer: it orders the verify walk
and stays outside the hash. UPDATE and DELETE are
rejected by a trigger created in the migration, so nothing this ORM model
declares can weaken the append-only guarantee.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Identity, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from helpdesk.models.base import TenantScoped

HASH_LENGTH = 64


class AuditEvent(TenantScoped):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_tenant_id_seq", "tenant_id", "seq"),)

    seq: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False, unique=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    actor_kind: Mapped[str] = mapped_column(String(16))  # user | agent | system
    actor_ref: Mapped[str | None] = mapped_column(String(256))
    session_id: Mapped[uuid.UUID | None] = mapped_column()
    trace_id: Mapped[str | None] = mapped_column(String(128))
    event_type: Mapped[str] = mapped_column(String(64))
    subject_type: Mapped[str] = mapped_column(String(64))
    subject_id: Mapped[str | None] = mapped_column(String(256))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(HASH_LENGTH))
    hash: Mapped[str] = mapped_column(String(HASH_LENGTH))
