"""Ingestion: one corpus document into both stores, in a fixed order.

Redact first, so raw text is never stored. Register the document row
before vector writes, since points need its id. Write points, then chunk
rows. Mark other versions not current last. Nothing here commits.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from helpdesk.mcp_server.services.rag import search_cache
from helpdesk.mcp_server.services.rag.chunking import chunk_document
from helpdesk.mcp_server.services.rag.chunks import write_chunks
from helpdesk.mcp_server.services.rag.embedding import Embedder
from helpdesk.mcp_server.services.rag.markdown import split_sections
from helpdesk.mcp_server.services.rag.redaction_client import RedactedDocument
from helpdesk.mcp_server.services.rag.store import register_document_version
from helpdesk.mcp_server.services.rag.vectors import (
    ensure_collection,
    supersede_points,
    upsert_chunks,
)
from helpdesk.shared.core.db import tenant_session
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.core.naming import check_slug, check_version
from helpdesk.shared.models import PolicyDocument

MARKDOWN_SUFFIX = ".md"
"""The only files a version directory is walked for."""


class Redactor(Protocol):
    """What ingestion needs from the redaction gateway. A protocol so tests
    run without a gateway."""

    async def redact_document(self, text: str, tenant_slug: str) -> RedactedDocument: ...


class CorpusDirectoryNotFoundError(ToolError):
    """The tenant or version directory does not exist. Otherwise a typo would
    "succeed" with no documents."""

    code = ErrorCode.VALIDATION


class CorpusDocumentNotFoundError(ToolError):
    """The corpus file this re-ingest was pointed at is not on disk."""

    code = ErrorCode.NOT_FOUND


@dataclass(frozen=True)
class IngestResult:
    """What one document's ingest did. `created` False means unchanged, and
    `chunk_count` is then 0."""

    document: PolicyDocument
    created: bool
    chunk_count: int


async def ingest_document(
    session: AsyncSession,
    client: AsyncQdrantClient,
    *,
    tenant_slug: str,
    slug: str,
    title: str,
    version: str,
    source_path: str,
    source: bytes,
    redactor: Redactor,
    embedder: Embedder,
    ingested_at: datetime,
) -> IngestResult:
    """Ingest one version of one document into Qdrant and Postgres."""
    redacted = await redactor.redact_document(source.decode("utf-8"), tenant_slug)
    sections = split_sections(redacted.text)
    chunks = chunk_document(sections, embedder)

    registration = await register_document_version(
        session,
        tenant_slug=tenant_slug,
        slug=slug,
        title=title,
        version=version,
        source_path=source_path,
        source=source,
        ingested_at=ingested_at,
    )
    if not registration.created:
        return IngestResult(document=registration.document, created=False, chunk_count=0)

    document = registration.document
    await ensure_collection(client, tenant_slug)
    point_ids = await upsert_chunks(
        client,
        tenant_slug,
        tenant_id=document.tenant_id,
        document_id=document.id,
        document_slug=slug,
        version=version,
        chunks=chunks,
    )
    await write_chunks(session, document_id=document.id, chunks=chunks, point_ids=point_ids)
    await supersede_points(client, tenant_slug, document_slug=slug, keep_document_id=document.id)
    # Last, so no search between the upsert and the supersede can save an old
    # page under the new generation. A failure here fails the ingest: a silent
    # one would serve the old policy for a day.
    await search_cache.drop(client, tenant_slug)
    return IngestResult(document=document, created=True, chunk_count=len(chunks))


async def ingest_version(
    factory: async_sessionmaker[AsyncSession],
    client: AsyncQdrantClient,
    *,
    tenant_id: uuid.UUID,
    corpus_root: Path,
    tenant_slug: str,
    version: str,
    redactor: Redactor,
    embedder: Embedder,
    ingested_at: datetime,
) -> list[IngestResult]:
    """Ingest every markdown file of one version directory, in name order.
    One transaction per file, so a failure leaves no file half-written."""
    check_slug(tenant_slug)
    check_version(version)

    directory = corpus_root / tenant_slug / version
    if not directory.is_dir():
        raise CorpusDirectoryNotFoundError(
            f"no corpus directory for tenant {tenant_slug!r} version {version!r}"
        )

    results: list[IngestResult] = []
    for path in sorted(directory.glob(f"*{MARKDOWN_SUFFIX}")):
        source = path.read_bytes()
        async with tenant_session(factory, tenant_id) as session:
            results.append(
                await ingest_document(
                    session,
                    client,
                    tenant_slug=tenant_slug,
                    slug=path.stem,
                    title=_title_of(source, fallback=path.stem),
                    version=version,
                    source_path=f"{tenant_slug}/{version}/{path.name}",
                    source=source,
                    redactor=redactor,
                    embedder=embedder,
                    ingested_at=ingested_at,
                )
            )
    return results


async def ingest_one_document(
    session: AsyncSession,
    client: AsyncQdrantClient,
    *,
    corpus_root: Path,
    tenant_slug: str,
    slug: str,
    version: str,
    redactor: Redactor,
    embedder: Embedder,
    ingested_at: datetime,
) -> IngestResult:
    """Ingest one corpus document, in the caller's transaction.

    Names are checked before the path is built, so a slug cannot climb out
    of the corpus folder.
    """
    check_slug(tenant_slug)
    check_slug(slug)
    check_version(version)

    name = f"{slug}{MARKDOWN_SUFFIX}"
    path = corpus_root / tenant_slug / version / name
    if not path.is_file():
        raise CorpusDocumentNotFoundError(
            f"no corpus document {slug!r} in tenant {tenant_slug!r} version {version!r}"
        )

    source = path.read_bytes()
    return await ingest_document(
        session,
        client,
        tenant_slug=tenant_slug,
        slug=slug,
        title=_title_of(source, fallback=slug),
        version=version,
        source_path=f"{tenant_slug}/{version}/{name}",
        source=source,
        redactor=redactor,
        embedder=embedder,
        ingested_at=ingested_at,
    )


def _title_of(source: bytes, *, fallback: str) -> str:
    """The document's first heading, or the file's own name."""
    for section in split_sections(source.decode("utf-8")):
        if section.heading_path:
            return section.heading_path[0]
    return fallback
