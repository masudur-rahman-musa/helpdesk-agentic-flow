"""Policy documents (one row per tenant, slug and version) and their chunks.

Column widths come from `core.naming`, so a check and its column match.
`heading_path` is an array, since a joined string would need a separator no
heading can contain.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, Uuid
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from helpdesk.shared.core.naming import (
    SLUG_LENGTH,
    SOURCE_PATH_LENGTH,
    TITLE_LENGTH,
    VERSION_LENGTH,
)
from helpdesk.shared.models.base import TenantScoped

SHA256_LENGTH = 64


class PolicyDocument(TenantScoped):
    """One ingested version of one policy document.

    `is_current` is per slug, so a partial re-ingest never leaves a document
    with no current copy.
    """

    __tablename__ = "policy_documents"
    __table_args__ = (Index("ix_policy_documents_tenant_id_slug", "tenant_id", "slug"),)

    slug: Mapped[str] = mapped_column(String(SLUG_LENGTH))
    title: Mapped[str] = mapped_column(String(TITLE_LENGTH))
    version: Mapped[str] = mapped_column(String(VERSION_LENGTH))
    is_current: Mapped[bool] = mapped_column(Boolean)
    source_path: Mapped[str] = mapped_column(String(SOURCE_PATH_LENGTH))
    sha256: Mapped[str] = mapped_column(String(SHA256_LENGTH))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PolicyChunk(TenantScoped):
    """One span of one document version. Has its own `tenant_id` so the guard covers it."""

    __tablename__ = "policy_chunks"
    __table_args__ = (Index("ix_policy_chunks_tenant_id_document_id", "tenant_id", "document_id"),)

    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("policy_documents.id"))
    chunk_index: Mapped[int] = mapped_column(Integer)
    heading_path: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    char_start: Mapped[int] = mapped_column(Integer)
    char_end: Mapped[int] = mapped_column(Integer)
    qdrant_point_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    text: Mapped[str] = mapped_column(Text)
