"""Registering one version of one corpus document in Postgres.

Answers "is this file already registered, and which version is current".
Runs in the caller's transaction, so the insert and the current-flag flip
commit together and nobody sees two current rows. `tenant_slug` is only a
cross-check against the session's tenant, never where the row is written.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.core.naming import (
    check_slug,
    check_source_path,
    check_title,
    check_version,
)
from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.rag import fetch_current_documents_except, find_document_by_digest
from helpdesk.shared.crud.tenants import find_tenant_by_slug
from helpdesk.shared.models import PolicyDocument


class TenantSlugMismatchError(ToolError):
    """`tenant_slug` does not name the tenant this session is bound to.

    Either way the ingest is aimed at the wrong tenant. `VALIDATION`, since
    the caller's own two arguments disagree.
    """

    code = ErrorCode.VALIDATION


@dataclass(frozen=True)
class RegistrationResult:
    """The row this version is registered under, and whether it was written.

    `created` is False when the same bytes were already registered; the
    caller then skips the rest of the ingest for that file.
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

    All checks run before any write. `ingested_at` comes from the caller, so
    one run shares one instant. An unchanged source writes nothing, not even
    a new `ingested_at`.
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

    # Match on the digest, not just (slug, version), so a re-cut version can
    # be a second row.
    unchanged = await find_document_by_digest(session, slug=slug, version=version, sha256=digest)
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

    # Changed bytes make a new row, never an update: old content stays for
    # audit. `is_current` is per (tenant, slug), so every other version
    # loses it, even ones this run did not touch.
    for row in await fetch_current_documents_except(
        session, slug=slug, new_document_id=document.id
    ):
        await repository.update(row.id, is_current=False)

    return RegistrationResult(document=document, created=True)
