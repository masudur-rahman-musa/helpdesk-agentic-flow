"""Policy document and chunk reads for ingestion and search."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.models import PolicyChunk, PolicyDocument


async def fetch_chunk_ids_for_document(
    session: AsyncSession, document_id: uuid.UUID
) -> list[uuid.UUID]:
    """Chunk ids of this one document version, so a replace touches nothing else."""
    result = await session.execute(
        select(PolicyChunk.id).where(PolicyChunk.document_id == document_id)
    )
    return list(result.scalars().all())


async def fetch_document_titles(
    session: AsyncSession, document_ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """Titles for a page of search hits, in one query."""
    if not document_ids:
        return {}
    result = await session.execute(
        select(PolicyDocument.id, PolicyDocument.title).where(PolicyDocument.id.in_(document_ids))
    )
    return {row.id: row.title for row in result}


async def find_document_by_digest(
    session: AsyncSession,
    *,
    slug: str,
    version: str,
    sha256: str,
) -> PolicyDocument | None:
    """The matching row, or None. An unchanged file re-ingested finds its row and writes nothing."""
    result = await session.execute(
        select(PolicyDocument).where(
            PolicyDocument.slug == slug,
            PolicyDocument.version == version,
            PolicyDocument.sha256 == sha256,
        )
    )
    return result.scalars().one_or_none()


async def fetch_current_documents_except(
    session: AsyncSession,
    *,
    slug: str,
    new_document_id: uuid.UUID,
) -> list[PolicyDocument]:
    """Older current versions of this slug, to be marked as replaced."""
    result = await session.execute(
        select(PolicyDocument).where(
            PolicyDocument.slug == slug,
            PolicyDocument.id != new_document_id,
            PolicyDocument.is_current.is_(True),
        )
    )
    return list(result.scalars().all())


async def fetch_current_documents(session: AsyncSession) -> list[PolicyDocument]:
    """The live version of each document, by title and then slug.

    The session guard already filters whole rows by tenant; the tenant is
    named here too, so the read stays scoped if the select ever narrows.
    """
    result = await session.execute(
        select(PolicyDocument)
        .where(
            PolicyDocument.tenant_id == bound_tenant_id(session),
            PolicyDocument.is_current.is_(True),
        )
        .order_by(PolicyDocument.title, PolicyDocument.slug)
    )
    return list(result.scalars().all())


async def fetch_document_chunks(session: AsyncSession, document_id: uuid.UUID) -> list[PolicyChunk]:
    """Every chunk of one document version, in the order they were cut."""
    result = await session.execute(
        select(PolicyChunk)
        .where(
            PolicyChunk.tenant_id == bound_tenant_id(session),
            PolicyChunk.document_id == document_id,
        )
        .order_by(PolicyChunk.chunk_index)
    )
    return list(result.scalars().all())
