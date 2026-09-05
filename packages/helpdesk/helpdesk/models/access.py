"""The access request table (the design, an earlier change).

One row per request for one role for one user. Column types stay plain
(String for enum-valued fields) per the an earlier change convention; the allowed
values live in the enums below and are enforced at the Pydantic boundaries.
An earlier change only reads this table (`list_my_requests`, the snapshot builder's
approved-pending set); the first writer is 11b's `create_access_request`.

`idempotency_key` shares `KEY_LENGTH` with `idempotency_keys.key`: the same
client-supplied string lands in both columns, so one constant sizes both.
"""

import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text, Uuid
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from helpdesk.models.base import TenantScoped
from helpdesk.models.idempotency import KEY_LENGTH


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
