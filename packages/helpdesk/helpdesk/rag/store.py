"""Registering one version of one corpus document (the design, §12.1.7).

The Postgres half of ingestion. 14b adds the redaction and the embeddings,
14c the chunk rows and the Qdrant points; this module owns only the question
"is this file already registered, and which version is current".

Everything runs in the CALLER's transaction: the insert and the `is_current`
flip commit together or not at all, so no reader ever sees two current rows
for one (tenant, slug), and none sees a version whose predecessor is still
current. Pinned from a second concurrent transaction by
`test_rag_store.py::test_the_flip_is_invisible_until_the_registering_transaction_commits`,
which fails both for a store that commits the flip itself and for one that
never flips.

`tenant_slug` is the corpus DIRECTORY name and is only ever a cross-check:
the row is written through `Repository`, which stamps the tenant the session
is bound to, so a slug naming another tenant is a refusal
rather than a write into that tenant. Pinned by
`test_rag_store.py::test_a_tenant_slug_naming_another_tenant_is_refused`,
whose two slugs are both valid tenants, so only the cross-check can refuse
it.

The two `select`s below are the only statements this package makes against
its own table, and they live here for the reason `audit/search.py` gives:
the package that owns a table owns the statements against it, so no service
ever builds one. Every write still goes through `core.repository`.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.core.db import bound_tenant_id
from helpdesk.core.repository import Repository, find_tenant_by_slug
from helpdesk.errors import ErrorCode, ToolError
from helpdesk.models import PolicyDocument
from helpdesk.rag.naming import (
    check_slug,
    check_source_path,
    check_title,
    check_version,
)


class TenantSlugMismatchError(ToolError):
    """`tenant_slug` does not name the tenant this session is bound to.

    Covers a slug naming no tenant at all and a slug naming a different one:
    both mean the corpus directory being walked is not this session's, and
    both are the ingest running against the wrong tenant. `VALIDATION` rather
    than `FORBIDDEN` — nothing was denied to a principal, the caller's own
    two arguments disagree.
    """

    code = ErrorCode.VALIDATION


@dataclass(frozen=True)
class RegistrationResult:
    """The row this version is registered under, and whether it was written.

    `created` is False when the same sha256 was already registered for this
    (tenant, slug, version): the caller skips the rest of the ingest for that
    file, which is what the design's idempotence buys.
    """

    document: PolicyDocument
    created: bool


async def register_document_version(
    session: AsyncSession,
    *,
    tenant_slug: str,
    slug: str,
    title: str,
    version: str,
    source_path: str,
    source: bytes,
    ingested_at: datetime,
) -> RegistrationResult:
    """Register `source` as this (tenant, slug, version); flip older versions.

    The four `rag.naming` pre-checks and the tenant cross-check all run
    before any statement that writes, so a malformed name never half-lands a
    registration. Each refusal is pinned to leave `policy_documents`
    byte-identical by `test_rag_store.py::_assert_refused`, which plants a
    real row first so "unchanged" compares two populated snapshots rather
    than two empty ones.

    `ingested_at` is supplied by the caller rather than read from a clock
    here, so every document of one ingest run carries the same instant and a
    skipped file keeps the instant it was actually last read. A re-registered
    unchanged source writes NOTHING — not even a refreshed `ingested_at` —
    pinned by `test_re_registering_the_same_sha256_writes_nothing`.

    The digest is of the source BYTES (decision D, per file), so
    re-ingestion skips only the documents that did not change.
    """
    check_slug(slug)
    check_version(version)
    check_title(title)
    check_source_path(source_path)

    tenant = await find_tenant_by_slug(session, tenant_slug)
    if tenant is None or tenant.id != bound_tenant_id(session):
        raise TenantSlugMismatchError(
            f"tenant_slug {tenant_slug!r} is not the tenant this session is bound to"
        )

    digest = hashlib.sha256(source).hexdigest()

    # The idempotence question, asked as one statement: is THIS source already
    # registered under this (tenant, slug, version)? The session guard adds
    # the tenant filter. Matching on the digest rather than on (slug, version)
    # alone is what lets a re-cut version below be a second row without this
    # read ever seeing two.
    result = await session.execute(
        select(PolicyDocument).where(
            PolicyDocument.slug == slug,
            PolicyDocument.version == version,
            PolicyDocument.sha256 == digest,
        )
    )
    unchanged = result.scalars().one_or_none()
    if unchanged is not None:
        return RegistrationResult(document=unchanged, created=False)

    repository = Repository(session, PolicyDocument)
    document = await repository.create(
        slug=slug,
        title=title,
        version=version,
        is_current=True,
        source_path=source_path,
        sha256=digest,
        ingested_at=ingested_at,
    )

    # A version directory re-cut with different bytes therefore becomes a
    # SECOND row for the same (tenant, slug, version) rather than an update:
    # the design retains superseded content for audit and never deletes it,
    # and the flip below leaves exactly one of them current either way. The
    # plan does not name this case; it is decided here so it cannot be a
    # silent overwrite.

    # Decision E: `is_current` is per (tenant, slug), so every other version
    # of this document loses it — including one from a version directory this
    # run did not touch, which is what stops a partial re-ingest leaving a
    # document with no current copy.
    superseded = await session.execute(
        select(PolicyDocument).where(
            PolicyDocument.slug == slug,
            PolicyDocument.id != document.id,
            PolicyDocument.is_current.is_(True),
        )
    )
    for row in superseded.scalars().all():
        await repository.update(row.id, is_current=False)

    return RegistrationResult(document=document, created=True)
