"""`run_once`: the guard every mutating tool runs its effect inside.

The effect and the stored response commit in one transaction, so a
crash cannot run the action and lose its answer. An advisory lock on
(tenant, key) makes a second caller wait and then replay. This needs
READ COMMITTED: do not raise the isolation level on this session.
"""

import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.mcp_server.services.idempotency.fingerprint import fingerprint
from helpdesk.shared.core.canonical import canonical_json
from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.core.errors import ErrorCode, IdempotencyConflictError, ToolError
from helpdesk.shared.crud.idempotency import (
    acquire_idempotency_lock,
    create_idempotency_key,
    delete_expired_idempotency_key,
    find_idempotency_key,
)
from helpdesk.shared.models.idempotency import KEY_LENGTH, TOOL_NAME_LENGTH

KEY_TTL = timedelta(days=7)
"""How long a key is honoured. Past it, the key is free again."""


@dataclass(frozen=True, slots=True)
class RunOnceResult:
    """What `run_once` hands back. `replayed` keeps a replay out of the
    audit log as a second event."""

    response: Mapping[str, Any]
    replayed: bool


class TenantMismatchError(RuntimeError):
    """The requested tenant is not the one bound to the session."""


class InvalidIdempotencyKeyError(ToolError):
    """The key or tool name cannot be stored. Checked before the effect, not
    left to fail at flush after the grant ran."""

    code = ErrorCode.VALIDATION


class UnstorableResponseError(ToolError):
    """A tool returned a response that cannot be stored as JSON."""

    code = ErrorCode.INTERNAL


def _check_storable_identifiers(key: str, tool_name: str) -> None:
    """Refuse a key or tool name the table cannot hold, before anything runs."""
    if not key or len(key) > KEY_LENGTH:
        raise InvalidIdempotencyKeyError(
            f"idempotency key must be 1..{KEY_LENGTH} characters, got {len(key)}"
        )
    if not tool_name or len(tool_name) > TOOL_NAME_LENGTH:
        raise InvalidIdempotencyKeyError(
            f"tool name must be 1..{TOOL_NAME_LENGTH} characters, got {len(tool_name)}"
        )


def _storable(response: Mapping[str, Any], tool_name: str) -> dict[str, Any]:
    """The response as it will be stored and replayed. Used on both paths,
    since `jsonb` does not keep key order."""
    try:
        encoded = canonical_json(response)
    except TypeError as exc:
        raise UnstorableResponseError(
            f"{tool_name} returned a non-storable response: {exc}"
        ) from exc
    decoded: dict[str, Any] = json.loads(encoded)
    return decoded


async def run_once(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    key: str,
    tool_name: str,
    args: Mapping[str, Any],
    fn: Callable[[], Awaitable[Mapping[str, Any]]],
) -> RunOnceResult:
    """Run `fn` at most once for this (tenant, key); replay it after that.

    A tenant other than the session's is refused, not ignored. An expired
    key counts as absent.
    """
    bound = bound_tenant_id(session)
    if bound != tenant_id:
        raise TenantMismatchError(
            f"session is bound to tenant {bound}, refusing to run for {tenant_id}"
        )

    _check_storable_identifiers(key, tool_name)
    digest = fingerprint(tool_name, args)

    await acquire_idempotency_lock(session, bound, key)
    now = datetime.now(UTC)

    existing = await find_idempotency_key(session, key, now)
    if existing is not None:
        if existing.request_fingerprint != digest:
            raise IdempotencyConflictError(
                f"idempotency key {key!r} was already used for a different request"
            )
        # Restores the key order `jsonb` dropped; see `_storable`.
        return RunOnceResult(response=_storable(existing.response, tool_name), replayed=True)

    response = _storable(await fn(), tool_name)

    # An expired row still holds the unique index, so remove it first.
    await delete_expired_idempotency_key(session, key, now)
    await create_idempotency_key(
        session,
        key=key,
        tool_name=tool_name,
        request_fingerprint=digest,
        response=response,
        expires_at=now + KEY_TTL,
    )
    return RunOnceResult(response=response, replayed=False)
