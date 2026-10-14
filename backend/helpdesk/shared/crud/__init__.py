"""Tenant-scoped repository helpers: the only DB path for services.

A `Repository` wraps a session produced by `tenant_session` and never takes
a tenant id as an argument: reads are filtered by the session guard in
`helpdesk.core.db`, and `create` stamps the bound tenant. Ids belonging to
another tenant behave as not-found (`get`/`update` return None, `delete`
returns False) — never the row, never a cross-tenant effect.

The generic base lives here; the per-table reads and writes are cut into the
sibling modules (`tenants`, `users`, `grants`, `idempotency`, `sessions`,
`messages`, `access_requests`), each importing this `Repository` when it
needs the generic path. `count_chunks_by_document` stays here because it
crosses tables rather than belonging to any one of them.
"""

import uuid
from collections.abc import Collection, Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.models import PolicyChunk, TenantScoped


async def count_chunks_by_document(
    session: AsyncSession, document_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """How many chunks the bound tenant holds per document version.

    The grouped half of `read_policy_documents`: `chunk_count` is the only
    thing on `/admin/policies` that separates a version which ingested from
    one that registered a document and wrote no span, and asking per version
    would be a round trip per row of the corpus. One statement over exactly
    the ids just read, pinned with the read itself by
    `test_rag_documents.py::test_the_whole_read_issues_exactly_two_selects`.

    A document with no chunk row is simply ABSENT from the mapping rather
    than carrying a zero: the caller defaults it, which is the same value a
    registered-but-unchunked version legitimately has and the same value a
    document written between the two statements gets. Neither is an error, and the kept row is pinned by
    `::test_each_versions_chunk_count_is_exact_and_a_version_with_no_chunk_is_kept`.

    An empty `document_ids` runs no statement at all, exactly as
    `Repository.get_many` does: `IN ()` is not valid SQL, and a tenant with no
    corpus is a normal answer rather than a broken page
    (`::test_a_tenant_with_no_corpus_answers_an_empty_tuple`).

    The tenant clause is written OUT rather than left to the session guard,
    for the same reason `count_live_grants` writes it out: the guard injects
    its criteria through `with_loader_criteria`, which applies to the ENTITIES
    a statement loads, and this statement loads none. `bound_tenant_id` raises
    with no tenant bound, so the unbound case stays a loud failure rather than
    a corpus-wide count, and the scoping is pinned by
    `::test_the_same_slug_in_two_tenants_never_mixes_titles_counts_or_digests`.
    """
    if not document_ids:
        return {}
    result = await session.execute(
        select(PolicyChunk.document_id, func.count(PolicyChunk.id))
        .where(
            PolicyChunk.tenant_id == bound_tenant_id(session),
            PolicyChunk.document_id.in_(list(document_ids)),
        )
        .group_by(PolicyChunk.document_id)
    )
    return {document_id: int(count) for document_id, count in result.all()}


# Set by the session/mixin, never by callers: accepting them in **fields
# would let an update move a row across tenants behind the query guard.
_PROTECTED_FIELDS = frozenset({"id", "tenant_id"})


def _reject_protected(fields: dict[str, Any]) -> None:
    protected = _PROTECTED_FIELDS & fields.keys()
    if protected:
        raise ValueError(
            f"protected fields cannot be set through the repository: {sorted(protected)}"
        )


class Repository[M: TenantScoped]:
    def __init__(self, session: AsyncSession, model: type[M]) -> None:
        self._session = session
        self._model = model

    async def get(self, id: uuid.UUID, /) -> M | None:
        result = await self._session.execute(select(self._model).where(self._model.id == id))
        return result.scalars().one_or_none()

    async def get_many(self, ids: Collection[uuid.UUID], /) -> dict[uuid.UUID, M]:
        """The rows behind these ids, keyed by id, in ONE statement.

        The batched half of `get`, added by an earlier change: resolving a page of
        audit subjects one id at a time is a round trip per row against the
        one table the design never prunes. Ids belonging to another tenant
        are simply absent from the result, exactly as `get` returns None for
        one — the session guard filters the SELECT, so a caller cannot learn
        that a foreign id exists by finding it here.

        An empty `ids` runs no statement at all: `IN ()` is not valid SQL and
        a query for nothing has no answer worth a round trip.
        """
        if not ids:
            return {}
        result = await self._session.execute(
            select(self._model).where(self._model.id.in_(list(ids)))
        )
        return {row.id: row for row in result.scalars().all()}

    async def list(self) -> Sequence[M]:
        result = await self._session.execute(select(self._model))
        return result.scalars().all()

    async def create(self, **fields: Any) -> M:
        _reject_protected(fields)
        obj = self._model(tenant_id=bound_tenant_id(self._session), **fields)
        self._session.add(obj)
        await self._session.flush()
        return obj

    async def update(self, id: uuid.UUID, /, **fields: Any) -> M | None:
        _reject_protected(fields)
        obj = await self.get(id)
        if obj is None:
            return None
        for name, value in fields.items():
            setattr(obj, name, value)
        await self._session.flush()
        return obj

    async def delete(self, id: uuid.UUID, /) -> bool:
        obj = await self.get(id)
        if obj is None:
            return False
        await self._session.delete(obj)
        await self._session.flush()
        return True
