"""Stored idempotency keys, so a retry gets the saved response instead of a second change.

The unique index backs up the per-key lock: a second insert fails loudly.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from helpdesk.shared.models.base import TenantScoped

FINGERPRINT_LENGTH = 64

# Wider than a UUID, so a client's other token shape is stored, not cut.
KEY_LENGTH = 128

TOOL_NAME_LENGTH = 64


class IdempotencyKey(TenantScoped):
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        Index("uq_idempotency_keys_tenant_id_key", "tenant_id", "key", unique=True),
        Index("ix_idempotency_keys_expires_at", "expires_at"),
    )

    key: Mapped[str] = mapped_column(String(KEY_LENGTH))
    tool_name: Mapped[str] = mapped_column(String(TOOL_NAME_LENGTH))
    request_fingerprint: Mapped[str] = mapped_column(String(FINGERPRINT_LENGTH))
    response: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
