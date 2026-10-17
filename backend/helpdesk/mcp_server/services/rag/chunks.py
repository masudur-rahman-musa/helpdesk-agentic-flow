"""Writes the `policy_chunks` rows that point at the vector store points.

The pipeline writes points first, then these rows, in one transaction, so
a chunk row never exists without its point. Nothing here commits, and the
tenant comes from the session, never an argument.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.mcp_server.services.rag.chunking import Chunk
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.rag import fetch_chunk_ids_for_document
from helpdesk.shared.models import PolicyChunk


class ChunkPointMismatchError(ToolError):
    """`chunks` and `point_ids` are not the same length.

    Raised before any write. A bare `zip` would drop the last chunk or pair a
    row with another chunk's point, giving a citation to the wrong text.
    """

    code = ErrorCode.VALIDATION


async def write_chunks(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    chunks: Sequence[Chunk],
    point_ids: Sequence[uuid.UUID],
) -> None:
    """Write one `policy_chunks` row per chunk, in chunk order.

    `point_ids[i]` is the point of `chunks[i]`. An empty list writes nothing.
    """
    if len(chunks) != len(point_ids):
        raise ChunkPointMismatchError(f"{len(chunks)} chunks were given {len(point_ids)} point ids")

    repository = Repository(session, PolicyChunk)
    for index, chunk in enumerate(chunks):
        await repository.create(
            document_id=document_id,
            chunk_index=index,
            heading_path=list(chunk.heading_path),
            char_start=chunk.char_start,
            char_end=chunk.char_end,
            qdrant_point_id=point_ids[index],
            text=chunk.text,
        )


async def replace_chunks(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    chunks: Sequence[Chunk],
    point_ids: Sequence[uuid.UUID],
) -> None:
    """Make this document version's rows exactly `chunks`, deleting the rest.

    Scoped to one document version only. Older versions keep their rows,
    because past answers cite them as evidence. The length check runs before
    the delete.
    """
    if len(chunks) != len(point_ids):
        raise ChunkPointMismatchError(f"{len(chunks)} chunks were given {len(point_ids)} point ids")

    repository = Repository(session, PolicyChunk)
    existing_ids = await fetch_chunk_ids_for_document(session, document_id)
    for row_id in existing_ids:
        await repository.delete(row_id)

    await write_chunks(session, document_id=document_id, chunks=chunks, point_ids=point_ids)
