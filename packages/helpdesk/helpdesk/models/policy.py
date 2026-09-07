"""The two policy corpus tables (the design, §12, an earlier change).

`policy_documents` holds one row per (tenant, slug, version) of a corpus
file; `policy_chunks` holds the spans that version was split into. The chunk
table ships here rather than in 14c so one migration covers the whole of the
RAG schema, but no chunk row is written until 14c can write it together with
its `qdrant_point_id` (decision F) — a chunk without a point is a retrievable
span that no search can reach.

Column widths are imported from `helpdesk.rag.naming`, never repeated as
literals: the pre-check and the column it guards are one number, so widening
one without the other cannot happen. `rag.naming` is pure (`re` and the error
codes), so this import direction adds no cycle; `rag/__init__.py` deliberately
re-exports nothing for the same reason. Pinned by
`test_policy_models.py::test_the_document_column_widths_are_the_naming_constants`.

`heading_path` is `text[]` (decision C): a joined string needs a separator no
heading may contain, and the Qdrant payload takes a list either way. Pinned by
`test_policy_models.py::test_heading_path_is_a_text_array`.

Both inherit `TenantScoped`, which is what the per-model ORM guard in
`helpdesk.core.db` keys on: a `tenant_id` column alone would leave the corpus
readable across tenants, which the design makes the isolation the whole
retrieval path rests on. Pinned by
`test_policy_models.py::test_another_tenants_documents_and_chunks_are_invisible`.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, Uuid
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from helpdesk.models.base import TenantScoped
from helpdesk.rag.naming import (
    SLUG_LENGTH,
    SOURCE_PATH_LENGTH,
    TITLE_LENGTH,
    VERSION_LENGTH,
)

SHA256_LENGTH = 64
"""The hex digest width of `policy_documents.sha256`.

Fixed by the digest, not a policy choice: `hashlib.sha256().hexdigest()` is
always 64 characters. Pinned by
`test_policy_models.py::test_the_document_column_widths_are_the_naming_constants`.
"""


class PolicyDocument(TenantScoped):
    """One ingested version of one corpus document (the design).

    `is_current` is per `(tenant, slug)` (decision E), not per version
    directory: a partial re-ingest then never leaves a document with no
    current copy, at the cost of a tenant sitting on a mix of v1 and v2
    documents — which is what the corpus already is between ingests.

    `ingested_at` and the inherited `created_at` deliberately overlap.
    `created_at` is when this ROW was written; `ingested_at` is the design's
    "when this version's source was last read", and a re-registration of an
    unchanged source writes neither, which
    `test_rag_store.py::test_re_registering_an_unchanged_source_writes_nothing`
    pins.
    """

    __tablename__ = "policy_documents"
    __table_args__ = (
        # Leads on (tenant_id, slug): every read this store makes is "the
        # versions of this document", and the `is_current` flip is a write
        # over exactly that key.
        Index("ix_policy_documents_tenant_id_slug", "tenant_id", "slug"),
    )

    slug: Mapped[str] = mapped_column(String(SLUG_LENGTH))
    title: Mapped[str] = mapped_column(String(TITLE_LENGTH))
    version: Mapped[str] = mapped_column(String(VERSION_LENGTH))
    is_current: Mapped[bool] = mapped_column(Boolean)
    source_path: Mapped[str] = mapped_column(String(SOURCE_PATH_LENGTH))
    sha256: Mapped[str] = mapped_column(String(SHA256_LENGTH))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PolicyChunk(TenantScoped):
    """One span of one document version (the design, §12.1.3).

    `tenant_id` is carried even though `document_id` implies it: the ORM
    guard is per-model, so a child table without the column is an unguarded
    cross-tenant read.

    `qdrant_point_id` is nullable only so the column exists ahead of 14c;
    no row is written by this plan, and 14c writes the point id with the row.
    """

    __tablename__ = "policy_chunks"
    __table_args__ = (Index("ix_policy_chunks_tenant_id_document_id", "tenant_id", "document_id"),)

    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("policy_documents.id"))
    chunk_index: Mapped[int] = mapped_column(Integer)
    heading_path: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    char_start: Mapped[int] = mapped_column(Integer)
    char_end: Mapped[int] = mapped_column(Integer)
    qdrant_point_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    text: Mapped[str] = mapped_column(Text)
