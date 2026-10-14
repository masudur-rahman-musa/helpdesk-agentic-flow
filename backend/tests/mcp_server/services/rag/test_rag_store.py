"""An earlier change §6: `register_document_version` in `helpdesk/rag/store.py`.

Expected implementation surface (the implementer must provide exactly this):

``helpdesk.rag.store``
    ``@dataclass(frozen=True) class RegistrationResult`` — ``document:
        PolicyDocument`` and ``created: bool`` (False when the same sha256
        was already registered for that (tenant, slug, version)).
    ``TenantSlugMismatchError(ToolError)`` — ``code = ErrorCode.VALIDATION``;
        raised when ``tenant_slug`` does not name the tenant the session is
        bound to, which includes a slug naming no tenant at all.
    ``async def register_document_version(session, *, tenant_slug, slug,
        title, version, source_path, source: bytes, ingested_at) ->
        RegistrationResult``

    The session is a `core.db.tenant_session`; the row is written through
    `core.repository.Repository`, which stamps the BOUND tenant (hard rule
    6). `tenant_slug` is the corpus directory name and is only ever a
    cross-check against the bound tenant, never the source of the write —
    pinned by ``test_a_tenant_slug_naming_another_tenant_is_refused``.

    Order of effects: the four `rag.naming` pre-checks and the tenant
    cross-check run BEFORE any write (§1); then `sha256 =
    hashlib.sha256(source).hexdigest()` (decision D, per source file); an
    existing row for (bound tenant, slug, version) with that sha256 is
    returned untouched; otherwise one `policy_documents` row is inserted
    with ``is_current=True`` and every other row of the same (tenant, slug)
    is set ``is_current=False`` — decision E, in the CALLER's transaction,
    never committed separately.

a project rule: the database is `test_policy_models.py`'s session-scoped
`policy_db_url`, imported rather than rebuilt. Per-test isolation is a
namespace: every case registers under its own uuid-suffixed document slug,
so no two cases share a (tenant, slug) key.

No pytest-asyncio: every case is a sync function driving one `asyncio.run`.
"""

import asyncio
import hashlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from helpdesk.mcp_server.services.rag.store import (
    TenantSlugMismatchError,
    register_document_version,
)
from helpdesk.shared.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.shared.core.repository import Repository, find_tenant_by_slug
from helpdesk.shared.errors import ErrorCode
from helpdesk.shared.models import PolicyDocument
from helpdesk.shared.rag.naming import InvalidPolicyNameError

# Imported, not redefined: one database for the whole run, shared with
# `test_policy_models.py`.
from tests.shared.test_policy_models import (
    TENANT_A_SLUG,
    TENANT_B_SLUG,
    policy_db_url,  # noqa: F401
)

V1_SOURCE = b"# Fake Access Control Policy\n\n## 1. Purpose\n\nFake v1 body.\n"
V2_SOURCE = b"# Fake Access Control Policy\n\n## 1. Purpose\n\nFake v2 body, rewritten.\n"

INGESTED_V1 = datetime(2026, 5, 4, 9, 30, tzinfo=UTC)
INGESTED_V2 = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)
INGESTED_AGAIN = datetime(2026, 7, 2, 17, 45, tzinfo=UTC)


@dataclass(frozen=True)
class Row:
    """The fields a case compares; enough to see any write the store made."""

    id: uuid.UUID
    slug: str
    version: str
    is_current: bool
    sha256: str
    ingested_at: datetime


def _row(document: PolicyDocument) -> Row:
    return Row(
        id=document.id,
        slug=document.slug,
        version=document.version,
        is_current=document.is_current,
        sha256=document.sha256,
        ingested_at=document.ingested_at,
    )


def _drive[T](url: str, work: Callable[[async_sessionmaker[AsyncSession]], Awaitable[T]]) -> T:
    """Run one async scenario on its own engine and event loop."""

    async def scenario() -> T:
        engine = create_engine(url)
        try:
            return await work(create_session_factory(engine))
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


async def _tenant_id(factory: async_sessionmaker[AsyncSession], slug: str) -> uuid.UUID:
    async with factory() as session:
        tenant = await find_tenant_by_slug(session, slug)
    assert tenant is not None, slug
    return tenant.id


async def _documents(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, slug: str
) -> list[Row]:
    """Every version of one (tenant, slug), oldest ingest first."""
    async with tenant_session(factory, tenant_id) as session:
        rows = [_row(row) for row in await Repository(session, PolicyDocument).list()]
    return sorted((row for row in rows if row.slug == slug), key=lambda row: row.version)


async def _all_documents(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> list[Row]:
    async with tenant_session(factory, tenant_id) as session:
        rows = [_row(row) for row in await Repository(session, PolicyDocument).list()]
    return sorted(rows, key=lambda row: (row.slug, row.version))


def _namespace_slug() -> str:
    return f"fake-policy-{uuid.uuid4().hex[:12]}"


def _source_path(document_slug: str, version: str) -> str:
    return f"policies/{TENANT_A_SLUG}/{version}/{document_slug}.md"


# --- the happy paths ----------------------------------------------------------


def test_a_first_registration_writes_one_current_row(policy_db_url: str) -> None:  # noqa: F811
    """Catches a store that writes the row without `is_current=True`, or that
    stores a digest of anything but the source bytes (decision D: the sha256
    of the file, not of its path or its title) — either leaves 14c unable to
    tell which version to retrieve or unable to skip an unchanged file."""
    document_slug = _namespace_slug()

    async def work(factory: async_sessionmaker[AsyncSession]) -> list[Row]:
        tenant_id = await _tenant_id(factory, TENANT_A_SLUG)
        async with tenant_session(factory, tenant_id) as session:
            result = await register_document_version(
                session,
                tenant_slug=TENANT_A_SLUG,
                slug=document_slug,
                title="Fake Access Control Policy",
                version="v1",
                source_path=_source_path(document_slug, "v1"),
                source=V1_SOURCE,
                ingested_at=INGESTED_V1,
            )
            assert result.created is True
        return await _documents(factory, tenant_id, document_slug)

    rows = _drive(policy_db_url, work)

    assert len(rows) == 1
    assert rows[0].version == "v1"
    assert rows[0].is_current is True
    assert rows[0].sha256 == hashlib.sha256(V1_SOURCE).hexdigest()
    assert rows[0].ingested_at == INGESTED_V1


def test_a_second_version_flips_the_first_and_leaves_one_current_row(
    policy_db_url: str,  # noqa: F811
) -> None:
    """Catches a store that inserts the new version without clearing
    `is_current` on the old one (plan §5's risk: two current rows for one
    (tenant, slug)), and one that flips by version comparison rather than by
    (tenant, slug), which would leave v1 current under decision E."""
    document_slug = _namespace_slug()

    async def work(factory: async_sessionmaker[AsyncSession]) -> list[Row]:
        tenant_id = await _tenant_id(factory, TENANT_A_SLUG)
        for version, source, ingested_at in (
            ("v1", V1_SOURCE, INGESTED_V1),
            ("v2", V2_SOURCE, INGESTED_V2),
        ):
            async with tenant_session(factory, tenant_id) as session:
                await register_document_version(
                    session,
                    tenant_slug=TENANT_A_SLUG,
                    slug=document_slug,
                    title="Fake Access Control Policy",
                    version=version,
                    source_path=_source_path(document_slug, version),
                    source=source,
                    ingested_at=ingested_at,
                )
        return await _documents(factory, tenant_id, document_slug)

    rows = _drive(policy_db_url, work)

    assert [row.version for row in rows] == ["v1", "v2"]
    assert [row.is_current for row in rows] == [False, True]
    assert len([row for row in rows if row.is_current]) == 1


def test_the_flip_is_invisible_until_the_registering_transaction_commits(
    policy_db_url: str,  # noqa: F811
) -> None:
    """Catches a store that commits, or opens its own session/transaction for,
    the `is_current` flip: read from a SECOND concurrent transaction, an
    uncommitted flip must still show v1 current, and after the writer commits
    the same read must show v2 — an implementation that flips outside the
    caller's transaction fails the first half, one that never flips fails the
    second."""
    document_slug = _namespace_slug()

    async def work(factory: async_sessionmaker[AsyncSession]) -> tuple[list[Row], list[Row]]:
        tenant_id = await _tenant_id(factory, TENANT_A_SLUG)
        async with tenant_session(factory, tenant_id) as session:
            await register_document_version(
                session,
                tenant_slug=TENANT_A_SLUG,
                slug=document_slug,
                title="Fake Access Control Policy",
                version="v1",
                source_path=_source_path(document_slug, "v1"),
                source=V1_SOURCE,
                ingested_at=INGESTED_V1,
            )

        async with tenant_session(factory, tenant_id) as writer:
            await register_document_version(
                writer,
                tenant_slug=TENANT_A_SLUG,
                slug=document_slug,
                title="Fake Access Control Policy",
                version="v2",
                source_path=_source_path(document_slug, "v2"),
                source=V2_SOURCE,
                ingested_at=INGESTED_V2,
            )
            during = await _documents(factory, tenant_id, document_slug)

        after = await _documents(factory, tenant_id, document_slug)
        return during, after

    during, after = _drive(policy_db_url, work)

    assert [(row.version, row.is_current) for row in during] == [("v1", True)]
    assert [(row.version, row.is_current) for row in after] == [("v1", False), ("v2", True)]


def test_re_registering_the_same_sha256_writes_nothing(policy_db_url: str) -> None:  # noqa: F811
    """Catches a store that inserts a second row (or refreshes `ingested_at`)
    for an unchanged source: the design's skip is what makes re-ingesting
    the corpus cheap, and a new `ingested_at` would claim the file was read
    again when nothing about it changed."""
    document_slug = _namespace_slug()

    async def work(factory: async_sessionmaker[AsyncSession]) -> tuple[list[Row], list[Row], bool]:
        tenant_id = await _tenant_id(factory, TENANT_A_SLUG)
        async with tenant_session(factory, tenant_id) as session:
            await register_document_version(
                session,
                tenant_slug=TENANT_A_SLUG,
                slug=document_slug,
                title="Fake Access Control Policy",
                version="v1",
                source_path=_source_path(document_slug, "v1"),
                source=V1_SOURCE,
                ingested_at=INGESTED_V1,
            )
        before = await _documents(factory, tenant_id, document_slug)

        async with tenant_session(factory, tenant_id) as session:
            result = await register_document_version(
                session,
                tenant_slug=TENANT_A_SLUG,
                slug=document_slug,
                title="Fake Access Control Policy",
                version="v1",
                source_path=_source_path(document_slug, "v1"),
                source=V1_SOURCE,
                ingested_at=INGESTED_AGAIN,
            )
            created = result.created
        after = await _documents(factory, tenant_id, document_slug)
        return before, after, created

    before, after, created = _drive(policy_db_url, work)

    assert created is False
    assert before == after
    assert len(after) == 1
    assert after[0].ingested_at == INGESTED_V1


# --- the refusals, each proven to leave the table unchanged -------------------


def test_an_unknown_tenant_slug_refuses_before_any_write(policy_db_url: str) -> None:  # noqa: F811
    """Catches a store that never resolves the corpus directory's tenant slug
    and writes into whatever tenant the session happens to be bound to, so a
    typo'd or deleted directory silently ingests one tenant's policies as
    another's."""
    _assert_refused(
        policy_db_url,
        error=TenantSlugMismatchError,
        tenant_slug=f"no-such-tenant-{uuid.uuid4().hex[:8]}",
        slug=_namespace_slug(),
        version="v1",
    )


def test_a_tenant_slug_naming_another_tenant_is_refused(policy_db_url: str) -> None:  # noqa: F811
    """Catches a store that derives the tenant from its ARGUMENT instead of
    the session: both tenants exist and both are valid slugs,
    so the only thing that can refuse this call is the cross-check against
    the bound tenant — a store that trusted the argument would happily write
    tenant B's row from tenant A's session."""
    _assert_refused(
        policy_db_url,
        error=TenantSlugMismatchError,
        tenant_slug=TENANT_B_SLUG,
        slug=_namespace_slug(),
        version="v1",
    )


def test_a_document_slug_that_is_not_a_lowercase_hyphen_slug_refuses_before_any_write(
    policy_db_url: str,  # noqa: F811
) -> None:
    """Catches a store that skips the `rag.naming` slug pre-check: the value
    would reach the sized column and either be stored as a second spelling of
    one document or abort the transaction as a driver-level truncation error
    after the write had started."""
    _assert_refused(
        policy_db_url,
        error=InvalidPolicyNameError,
        tenant_slug=TENANT_A_SLUG,
        slug="Fake Access Control Policy",
        version="v1",
    )


def test_a_version_that_is_not_vn_refuses_before_any_write(policy_db_url: str) -> None:  # noqa: F811
    """Catches a store that skips the `rag.naming` version pre-check: `1.0`
    is the corpus front-matter spelling decision G rejected, and a row
    carrying it can never be matched by the `vN` payload filter 14c
    builds."""
    _assert_refused(
        policy_db_url,
        error=InvalidPolicyNameError,
        tenant_slug=TENANT_A_SLUG,
        slug=_namespace_slug(),
        version="1.0",
    )


def _assert_refused(
    url: str,
    *,
    error: type[Exception],
    tenant_slug: str,
    slug: str,
    version: str,
) -> None:
    """Register once validly, then make the bad call and prove nothing moved.

    The valid registration first is what gives "unchanged" something to say:
    the tenant's document table is snapshotted with a current row in it, so a
    refusal that had already inserted, flipped or touched `ingested_at` shows
    up as a difference rather than as two empty lists.
    """
    planted_slug = _namespace_slug()

    async def work(factory: async_sessionmaker[AsyncSession]) -> tuple[list[Row], list[Row]]:
        tenant_id = await _tenant_id(factory, TENANT_A_SLUG)
        async with tenant_session(factory, tenant_id) as session:
            await register_document_version(
                session,
                tenant_slug=TENANT_A_SLUG,
                slug=planted_slug,
                title="Fake Access Control Policy",
                version="v1",
                source_path=_source_path(planted_slug, "v1"),
                source=V1_SOURCE,
                ingested_at=INGESTED_V1,
            )
        before = await _all_documents(factory, tenant_id)

        with pytest.raises(error) as excinfo:
            async with tenant_session(factory, tenant_id) as session:
                await register_document_version(
                    session,
                    tenant_slug=tenant_slug,
                    slug=slug,
                    title="Fake Access Control Policy",
                    version=version,
                    source_path=_source_path("fake-refused", "v1"),
                    source=V2_SOURCE,
                    ingested_at=INGESTED_V2,
                )
        assert getattr(excinfo.value, "code", None) is ErrorCode.VALIDATION

        after = await _all_documents(factory, tenant_id)
        return before, after

    before, after = _drive(url, work)
    assert before == after, "the refused call changed policy_documents"
