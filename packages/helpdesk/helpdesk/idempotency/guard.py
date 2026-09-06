"""`run_once`: the guard every mutating tool runs its effect inside.

A retried request must never grant or revoke twice (the design). The effect
and the stored response are written in the caller's transaction, so they
commit together: there is no window where the action happened but the
response that describes it did not. That is why the API is a callback
wrapper rather than a claim-then-complete pair — the crash case the spec
names is exactly the gap a two-phase API leaves open.

Read-then-insert is a read-modify-write, so two concurrent calls with one
key could both read "absent" and both run the effect. A transaction-scoped
advisory lock on (tenant, key) serialises them; the loser blocks, and once
the winner commits it sees the row and replays. The unique index on
(tenant_id, key) is the backstop if the lock is ever bypassed.

The lock uses the two-integer `pg_advisory_xact_lock(classid, objid)` form,
which Postgres keeps in a different key space from the single-bigint form
`audit/writer.py` uses: an idempotency lock can never block an audit append
for the same tenant.

Like the audit writer, this serialises correctly only because the read
after the lock sees rows committed since the lock was granted, which holds
under READ COMMITTED (the engine default). Under a stricter isolation level
the snapshot would predate the lock and two callers could both run the
effect; do not raise the isolation level on this session without revisiting
the read below.
"""

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.core.canonical import canonical_json
from helpdesk.core.db import bound_tenant_id
from helpdesk.core.repository import (
    create_idempotency_key,
    delete_expired_idempotency_key,
    find_idempotency_key,
)
from helpdesk.errors import ErrorCode, IdempotencyConflictError, ToolError
from helpdesk.idempotency.fingerprint import fingerprint
from helpdesk.models.idempotency import KEY_LENGTH, TOOL_NAME_LENGTH

KEY_TTL = timedelta(days=7)
"""How long a key is honoured (the design). Past it, the key is free again."""

_ADVISORY_LOCK = text("SELECT pg_advisory_xact_lock(:classid, :objid)")

# Namespace for idempotency locks. Only has to be stable; it shares no key
# space with the audit writer's single-argument locks.
_LOCK_CLASSID = 0x1DE3

_SIGNED_32_BIT_MASK = (1 << 31) - 1


@dataclass(frozen=True, slots=True)
class RunOnceResult:
    """What `run_once` hands back.

    `replayed` is False when `fn` ran in this call and True when a stored row
    answered, so the caller can record the two honestly — a replay must never
    be written to the audit log as the declared event happening again.
    Pinned by `test_replay_returns_the_stored_response_without_running_the_effect`.
    """

    response: Mapping[str, Any]
    replayed: bool


class TenantMismatchError(RuntimeError):
    """The requested tenant is not the one bound to the session."""


class InvalidIdempotencyKeyError(ToolError):
    """The key or tool name cannot be stored as given.

    Checked before the lock and before the effect: left to the column
    lengths, an oversized key would abort the flush *after* the grant or
    revoke had run, and surface as a free-text database error instead of a
    typed code (the design).
    """

    code = ErrorCode.VALIDATION


class UnstorableResponseError(ToolError):
    """A tool returned a response that cannot be stored as JSON.

    Raised at the call site rather than left to fail at commit time, where
    the traceback would point at the flush and not at the tool that built
    the value.
    """

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


def _lock_object_id(tenant_id: uuid.UUID, key: str) -> int:
    """A stable positive int4 for this (tenant, key).

    Two different keys that collide here only wait for each other; the
    correctness of a replay comes from the stored row, never from the lock.
    """
    digest = hashlib.sha256(tenant_id.bytes + key.encode()).digest()
    return int.from_bytes(digest[:4], "big") & _SIGNED_32_BIT_MASK


def _storable(response: Mapping[str, Any], tool_name: str) -> dict[str, Any]:
    """The response as it will be stored and replayed.

    Round-tripped through the canonical encoder so that what the caller
    receives on the first call is byte-identical to what a replay returns:
    a value the encoder renders (a UUID, a datetime) becomes its rendered
    form once, here, instead of differing between the two paths.

    Applied to BOTH paths — the fresh response and the stored row alike —
    because the round trip normalises key ORDER as well as values, and the
    two sources order keys differently. `canonical_json` sorts; a `jsonb`
    column read back from Postgres does not, it orders by key length and then
    bytewise. So a response whose sorted order differs from that one came
    back with the same values in a different byte string on replay, which is
    exactly what byte-identity promises not to do. Pinned by
    `test_mcp_reviews.py::test_a_replayed_key_returns_the_same_response_and_opens_no_second_review`,
    whose `flag_counts` is the first dict-valued field any tool output has and
    is what exposed this; every earlier mutating tool happened to have a key
    set the two orders agree on.
    """
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

    `fn` takes no arguments and closes over the same session, so its writes
    join this transaction. The tenant actually used is the session's bound
    one and a mismatched argument is refused rather than honoured (hard rule
    6): silently ignoring it would let a caller believe a key was scoped to
    a tenant it was not.

    Returns a `RunOnceResult` whose `replayed` says whether the stored row
    answered instead of `fn`. Raises `IdempotencyConflictError` when the key
    was already spent on a different request. An expired key is treated as
    absent: the effect runs again and its row replaces the stale one.
    """
    bound = bound_tenant_id(session)
    if bound != tenant_id:
        raise TenantMismatchError(
            f"session is bound to tenant {bound}, refusing to run for {tenant_id}"
        )

    _check_storable_identifiers(key, tool_name)
    digest = fingerprint(tool_name, args)

    await session.execute(
        _ADVISORY_LOCK, {"classid": _LOCK_CLASSID, "objid": _lock_object_id(bound, key)}
    )
    now = datetime.now(UTC)

    existing = await find_idempotency_key(session, key, now)
    if existing is not None:
        if existing.request_fingerprint != digest:
            raise IdempotencyConflictError(
                f"idempotency key {key!r} was already used for a different request"
            )
        # Normalised on the way out, not trusted as stored: see `_storable`.
        # The values are already canonical (this row was written through it),
        # so this only restores the key order Postgres's `jsonb` dropped.
        return RunOnceResult(response=_storable(existing.response, tool_name), replayed=True)

    response = _storable(await fn(), tool_name)

    # The stale row is invisible to the read above but still holds the
    # unique index, so it has to go before the key can be re-claimed.
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
