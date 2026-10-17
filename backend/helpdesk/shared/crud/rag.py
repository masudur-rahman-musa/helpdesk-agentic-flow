"""RAG-table reads: chunk ids, document titles and document registration.

Every statement against `policy_chunks` and `policy_documents` that the RAG
ingest and retrieval paths need lives here, so the service modules under
`mcp_server/services/rag/` build no statement of their own.  The tenant guard
in `helpdesk.core.db` scopes every read to the bound session.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.models import PolicyChunk, PolicyDocument


async def fetch_chunk_ids_for_document(
    session: AsyncSession, document_id: uuid.UUID
) -> list[uuid.UUID]:
    """The ids of every chunk row belonging to this document version.

    Used by `replace_chunks` to delete the old set before writing the new one.
    Scoped to ONE `policy_documents` row so no other document's chunks are
    touched.  Pinned by
    `test_rag_chunks.py::test_replacing_one_document_leaves_another_documents_chunks_alone`.
    """
    result = await session.execute(
        select(PolicyChunk.id).where(PolicyChunk.document_id == document_id)
    )
    return list(result.scalars().all())


async def fetch_document_titles(
    session: AsyncSession, document_ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """The title of each named document, in ONE statement for the whole page.

    A per-hit read returns exactly the same citations; this batches them so a
    page over three documents issues one query.  An empty `document_ids`
    issues no statement.  Pinned by
    `test_rag_citations.py::test_a_page_over_three_documents_issues_one_title_query`.
    """
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
    """The registered document row matching (slug, version, sha256), or None.

    The idempotence check for `register_document_version`: a re-ingested
    unchanged file has the same digest, so the same (slug, version, sha256)
    triple already exists and no new row is needed.  The session guard adds the
    tenant filter.  Pinned by
    `test_rag_store.py::test_re_registering_the_same_sha256_writes_nothing`.
    """
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
    """Every current version of this slug that is not the newly written document.

    Used by `register_document_version` to flip the `is_current` flag on every
    superseded row after a new version is created.  A per (tenant, slug) flip,
    never a per-tenant one: another document of the same tenant keeps its rows.
    The session guard adds the tenant filter.  Pinned by
    `test_rag_store.py::test_a_second_version_of_one_document_supersedes_the_first`.
    """
    result = await session.execute(
        select(PolicyDocument).where(
            PolicyDocument.slug == slug,
            PolicyDocument.id != new_document_id,
            PolicyDocument.is_current.is_(True),
        )
    )
    return list(result.scalars().all())
