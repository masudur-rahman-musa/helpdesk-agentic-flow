"""The access request table (the design, an earlier change) and the review tables (12c).

One row per request for one role for one user. Column types stay plain
(String for enum-valued fields) per the an earlier change convention; the allowed
values live in the enums below and are enforced at the Pydantic boundaries.
An earlier change only reads this table (`list_my_requests`, the snapshot builder's
approved-pending set); the first writer is 11b's `create_access_request`.

`idempotency_key` shares `KEY_LENGTH` with `idempotency_keys.key`: the same
client-supplied string lands in both columns, so one constant sizes both.

An earlier change adds `access_reviews` and `review_items` below. They live here
rather than in `models/directory.py` because a review is an access artefact
like a request, not a directory fact: the directory says who exists and what
they hold, and these two tables say what somebody was asked to check.
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
    """the design's `access_requests.status` vocabulary.

    The snapshot builder reads `APPROVED` off this enum (approved but not
    yet executed counts toward SoD, the design), so a renamed member fails
    loudly there instead of silently emptying the pending set.
    """

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
    # FK target (sessions) lands in a later plan row.
    session_id: Mapped[uuid.UUID | None] = mapped_column()
    trace_id: Mapped[str | None] = mapped_column(String(128))
    idempotency_key: Mapped[str | None] = mapped_column(String(KEY_LENGTH))


PERIOD_LENGTH = 16
"""How long an `access_reviews.period` string may be.

The column's own size, imported by `mcp_server.tools.reviews` so the wire
pre-check and the storage are one number by construction:
`period` is client-supplied and lands in a sized column, and without a
pre-check an oversized value would abort the transaction with an asyncpg
`StringDataRightTruncation` AFTER the review and its items were built —
a free-text database error across a boundary whose contract is the eight
typed the design codes. Pinned inclusively by
`test_mcp_reviews.py::test_the_period_bounds_are_inclusive_and_reject_before_any_write`.

16 rather than the 7 a `YYYY-Qn` needs: the shape is a tenant-facing label
and row 21's scheduler may come to write `2026-H1` or a month, so the column
carries slack the validator does not."""


class ReviewStatus(StrEnum):
    """the design's `access_reviews.status` vocabulary.

    Two values only: an earlier change opens reviews and 12e completes them. A review
    is never `cancelled` — nothing in the design withdraws one.
    """

    OPEN = "open"
    COMPLETED = "completed"


class ReviewFlag(StrEnum):
    """Why one grant is worth a reviewer's attention, or that it is not.

    the design gives `review_items` ONE flag column, so a grant that is stale,
    outside its holder's template AND half of an SoD pair still gets one
    word. An earlier change decision E fixes the precedence as the declaration order
    here — `sod_conflict` first, `ok` last — and
    `test_access_reviews.py::test_a_grant_that_is_all_three_reads_sod_conflict`
    pins it on a grant that is all three at once.
    """

    SOD_CONFLICT = "sod_conflict"
    NON_STANDARD = "non_standard"
    STALE = "stale"
    OK = "ok"


class ReviewDecision(StrEnum):
    """What a reviewer did with one item. Written by an earlier change, never here."""

    KEEP = "keep"
    REVOKE = "revoke"


class AccessReview(TenantScoped):
    """One review of one scope for one period (the design).

    `department_id` and `system_id` are an earlier change decision A: the design lists no
    scope at all, yet the design's tool takes `department_id | system_id`, and
    a scope derived from the items afterwards would be unknowable for exactly
    the review that most needs explaining — an empty scope has no items. The
    check constraint makes "exactly one" a property of the table rather than
    of the one code path that writes it; the tool refuses neither-or-both as
    `INVALID_INPUT` long before the constraint could fire.
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
    """One active grant put in front of a reviewer, with its flag (the design).

    `tenant_id` is an earlier change decision B: the design gives the table only
    `review_id`, but the guard in `helpdesk.core.db` is per-model — it filters
    ORM SELECT/UPDATE/DELETE on tenant-scoped models only — so a child table
    without the column would be an unguarded cross-tenant read whose only
    protection is that every query happens to join its parent. Pinned by
    `test_access_reviews.py::test_another_tenants_review_items_are_invisible`.

    `decision` and `decided_at` stay null here: an earlier change opens the review and
    12e's `decide_review_item` is the only writer of the keep-or-revoke half.
    """

    __tablename__ = "review_items"
    __table_args__ = (Index("ix_review_items_tenant_id_review_id", "tenant_id", "review_id"),)

    review_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("access_reviews.id"))
    grant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("grants.id"))
    flag: Mapped[str] = mapped_column(String(16))  # ReviewFlag
    decision: Mapped[str | None] = mapped_column(String(16))  # ReviewDecision
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
