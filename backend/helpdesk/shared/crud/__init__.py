"""Tenant-scoped repository helpers: the only database path for services.

A `Repository` never takes a tenant id. Reads are filtered by the session's
bound tenant and `create` stamps it, so another tenant's row reads as missing.
"""

import uuid
from collections.abc import Collection, Sequence
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.crud.numbers import next_number
from helpdesk.shared.models import AccessRequest, PolicyChunk, TenantScoped, Ticket
from helpdesk.shared.models.numbers import NumberKind


async def count_chunks_by_document(
    session: AsyncSession, document_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """Chunks per document for the bound tenant, in one statement.

    A document with no chunks is left out; the caller treats that as zero.
    The tenant filter is written out because the session guard only covers
    queries that load whole rows, and this one loads none.
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


# Models that carry a readable number, and which count each one draws from.
_NUMBERED: dict[type[TenantScoped], NumberKind] = {
    Ticket: NumberKind.TICKET,
    AccessRequest: NumberKind.ACCESS_REQUEST,
}


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
        """The rows behind these ids, keyed by id, in one statement.

        Another tenant's ids are simply absent. An empty `ids` skips the query.
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
        kind = _NUMBERED.get(self._model)
        if kind is not None and "number" not in fields:
            # Drawn here, so no path can store a numbered row without one.
            fields["number"] = await next_number(self._session, kind)
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
