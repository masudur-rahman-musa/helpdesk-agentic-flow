"""Access requests and access reviews.

Enum-valued columns are plain strings; the enums below hold the allowed
values, checked at the Pydantic edges.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, Uuid
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from helpdesk.shared.models.base import TenantScoped
from helpdesk.shared.models.idempotency import KEY_LENGTH


class RequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    EXPIRED = "expired"


class AccessRequest(TenantScoped):
    __tablename__ = "access_requests"
    __table_args__ = (
        Index(
            "ix_access_requests_tenant_id_requester_user_id",
            "tenant_id",
            "requester_user_id",
        ),
        Index(
            "ix_access_requests_tenant_id_target_user_id",
            "tenant_id",
            "target_user_id",
        ),
    )

    requester_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    target_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    role_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("roles.id"))
    justification: Mapped[str] = mapped_column(Text)
    requested_expiry_days: Mapped[int | None] = mapped_column()
    risk_class: Mapped[str] = mapped_column(String(16))  # low | elevated | privileged | critical
    decision: Mapped[str] = mapped_column(String(16))  # auto | approval | blocked
    decision_reasons: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    citations: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(16))  # RequestStatus
    approver_user_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid), default=list)
    approvals: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    # No foreign key to `sessions` yet.
    session_id: Mapped[uuid.UUID | None] = mapped_column()
    trace_id: Mapped[str | None] = mapped_column(String(128))
    idempotency_key: Mapped[str | None] = mapped_column(String(KEY_LENGTH))


PERIOD_LENGTH = 16
"""Size of `period`, shared with the tool's input check. Room for labels beyond `YYYY-Qn`."""


class ReviewStatus(StrEnum):
    OPEN = "open"
    COMPLETED = "completed"


class ReviewFlag(StrEnum):
    """Why a grant needs a reviewer's eye. One flag per item; earlier members win."""

    SOD_CONFLICT = "sod_conflict"
    NON_STANDARD = "non_standard"
    STALE = "stale"
    OK = "ok"


class ReviewDecision(StrEnum):
    KEEP = "keep"
    REVOKE = "revoke"


class AccessReview(TenantScoped):
    """One review of one scope (a department or a system) for one period.

    The scope is stored, since an empty review has no items to infer it from.
    """

    __tablename__ = "access_reviews"
    __table_args__ = (
        CheckConstraint(
            "(department_id IS NULL) <> (system_id IS NULL)",
            name="exactly_one_scope",
        ),
        Index("ix_access_reviews_tenant_id_status", "tenant_id", "status"),
    )

    reviewer_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    department_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("departments.id"))
    system_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("systems.id"))
    period: Mapped[str] = mapped_column(String(PERIOD_LENGTH))
    status: Mapped[str] = mapped_column(String(16))  # ReviewStatus
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReviewItem(TenantScoped):
    """One grant put in front of a reviewer, with its flag.

    Carries its own `tenant_id` so the tenant guard covers it directly.
    """

    __tablename__ = "review_items"
    __table_args__ = (Index("ix_review_items_tenant_id_review_id", "tenant_id", "review_id"),)

    review_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("access_reviews.id"))
    grant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("grants.id"))
    flag: Mapped[str] = mapped_column(String(16))  # ReviewFlag
    decision: Mapped[str | None] = mapped_column(String(16))  # ReviewDecision
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
