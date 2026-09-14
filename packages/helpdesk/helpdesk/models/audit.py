"""The append-only audit tables (the design, §13.1).

`seq` is assigned by the database, not the writer: it orders the verify walk
and stays outside the hash. UPDATE and DELETE are
rejected by a trigger created in the migration, so nothing this ORM model
declares can weaken the append-only guarantee.

`AuditChainAnchor` is the second table and the same shape of guarantee: it
remembers one tenant's verified head, which is the fact a hash chain cannot
hold about itself. Its grants are SELECT and INSERT only, pinned by
`test_audit_db_role.py::test_expected_grant_map_covers_exactly_the_metadata_tables`.
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


class AuditChainAnchor(TenantScoped):
    """One tenant's verified chain head, remembered outside the walk.

    The uniqueness is a unique INDEX rather than a `UniqueConstraint`, so the
    composite index every tenant-scoped table owes and the constraint that
    stops two heads at one `audit_seq` are the same object; pinned by
    `test_models.py::test_tenant_scoped_tables_have_tenant_id_and_composite_index`
    and
    `test_audit_anchor.py::test_a_second_anchor_at_the_same_seq_is_refused_by_the_unique_constraint`.

    `anchored_at` carries a timezone: two anchors written either side of a
    clock change have to stay orderable, and `created_at` is not a substitute
    because it records the row, not the reading.
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
