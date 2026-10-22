"""The `notifications` table: one message to one person.

Every read asks "what is unread for this user", so each person gets their own
row. The length caps are also the column widths, shared with `notify`'s check.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from helpdesk.shared.models.base import TenantScoped

NOTIFICATION_TITLE_MAX_LENGTH = 200

NOTIFICATION_BODY_MAX_LENGTH = 2000


class NotificationKind(StrEnum):
    """What happened. Only kinds some job actually writes."""

    GRANT_EXPIRED = "grant_expired"
    GRANT_EXPIRING = "grant_expiring"
    REVIEW_OVERDUE = "review_overdue"
    REVIEW_ESCALATED = "review_escalated"
    APPROVAL_NEEDED = "approval_needed"
    REQUEST_PROGRESS = "request_progress"
    REQUEST_GRANTED = "request_granted"
    REQUEST_REJECTED = "request_rejected"
    REQUEST_FAILED = "request_failed"
    REQUEST_CANCELLED = "request_cancelled"
    ACCESS_REVOKED = "access_revoked"


class Notification(TenantScoped):
    """One message to one person. `subject_*` names the row it is about, for linking."""

    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_tenant_id_user_id_read_at", "tenant_id", "user_id", "read_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(NOTIFICATION_TITLE_MAX_LENGTH))
    body: Mapped[str] = mapped_column(String(NOTIFICATION_BODY_MAX_LENGTH))
    subject_type: Mapped[str | None] = mapped_column(String(64))
    subject_id: Mapped[str | None] = mapped_column(String(256))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
