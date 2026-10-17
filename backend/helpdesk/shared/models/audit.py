"""The append-only audit tables. A database trigger refuses UPDATE and DELETE.

`seq` comes from the database and stays outside the hash. An anchor records a
verified chain head, which a chain cannot prove about itself.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Identity, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from helpdesk.shared.models.base import TenantScoped

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


class AuditChainAnchor(TenantScoped):
    """One tenant's verified chain head.

    A unique index, so the tenant index and the one-head-per-seq rule are one object.
    """

    __tablename__ = "audit_chain_anchors"
    __table_args__ = (
        Index(
            "ix_audit_chain_anchors_tenant_id_audit_seq",
            "tenant_id",
            "audit_seq",
            unique=True,
        ),
    )

    audit_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    audit_hash: Mapped[str] = mapped_column(String(HASH_LENGTH), nullable=False)
    anchored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
