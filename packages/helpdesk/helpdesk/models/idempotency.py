"""Stored idempotency keys (the design, §13.2).

One row per (tenant, key) holds the fingerprint of the request that claimed
the key and the response that was committed for it, so a retry returns that
response instead of running the effect again.

The unique index on (tenant_id, key) is the backstop behind the advisory
lock in `idempotency/guard.py`: if the lock were ever bypassed, a second
insert fails loudly rather than leaving two rows that disagree about what
the key means.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from helpdesk.models.base import TenantScoped

FINGERPRINT_LENGTH = 64

# A client-supplied UUID v4 (the design); the column is wider so a caller
# using another opaque token is stored rather than silently truncated.
KEY_LENGTH = 128

TOOL_NAME_LENGTH = 64


class IdempotencyKey(TenantScoped):
    __tablename__ = "idempotency_keys"
    __table_args__ = (
        Index("uq_idempotency_keys_tenant_id_key", "tenant_id", "key", unique=True),
        # Leads on expires_at: the purge scans by expiry across every key.
        Index("ix_idempotency_keys_expires_at", "expires_at"),
    )

    key: Mapped[str] = mapped_column(String(KEY_LENGTH))
    tool_name: Mapped[str] = mapped_column(String(TOOL_NAME_LENGTH))
    request_fingerprint: Mapped[str] = mapped_column(String(FINGERPRINT_LENGTH))
    response: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
