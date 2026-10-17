"""The policy corpus read as documents, for the admin policies page.

Shows which documents a tenant has, their versions, which one is live and
whether a version landed any chunks. Two queries whatever the corpus size.
Decides and writes nothing.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.crud import Repository, count_chunks_by_document
from helpdesk.shared.models import PolicyDocument


@dataclass(frozen=True, slots=True)
class PolicyVersion:
    """One ingested version of one document.

    `source_path` is relative to the corpus, so nothing about the ingest
    host leaks.
    """

    version: str
    is_current: bool
    ingested_at: datetime
    chunk_count: int
    """How many chunks this version has. Zero is kept: it is the only sign
of an ingest that wrote no span."""

    source_path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class PolicyDocumentDetail:
    """One document slug and every version of it, grouped here so callers
    do not regroup a flat list."""

    slug: str
    title: str
    """The current version's title, or the newest version's when none is
current."""

    versions: tuple[PolicyVersion, ...]


async def read_policy_documents(session: AsyncSession) -> tuple[PolicyDocumentDetail, ...]:
    """The bound tenant's policy corpus, grouped by slug, in a fixed order.

    Slugs ascending; versions newest-ingested first, then by version
    descending. Never by the version string alone, which sorts `v10` before
    `v2`. A document with no current version is kept, since a half-finished
    ingest is what this page must show.
    """
    rows = await Repository(session, PolicyDocument).list()
    counts = await count_chunks_by_document(session, [row.id for row in rows])

    by_slug: dict[str, list[PolicyDocument]] = {}
    for row in rows:
        by_slug.setdefault(row.slug, []).append(row)

    details = []
    for slug, planted in by_slug.items():
        ordered = sorted(planted, key=lambda row: (row.ingested_at, row.version), reverse=True)
        current = next((row for row in ordered if row.is_current), None)
        newest = ordered[0]
        details.append(
            PolicyDocumentDetail(
                slug=slug,
                title=current.title if current is not None else newest.title,
                versions=tuple(
                    PolicyVersion(
                        version=row.version,
                        is_current=row.is_current,
                        ingested_at=row.ingested_at,
                        chunk_count=counts.get(row.id, 0),
                        source_path=row.source_path,
                        sha256=row.sha256,
                    )
                    for row in ordered
                ),
            )
        )
    return tuple(sorted(details, key=lambda detail: detail.slug))
