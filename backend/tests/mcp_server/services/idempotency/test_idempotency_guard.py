"""`run_once`, the guard around every mutating tool.

Expected implementation surface:

``helpdesk.idempotency.guard.run_once(session, tenant_id, key, tool_name, args, fn)``
    Takes a per-(tenant, key) transaction-scoped Postgres advisory lock on a
    classid distinct from the audit writer's, reads the tenant's live row
    through the repository, then either

    * replays: returns the stored ``response`` without calling ``fn``;
    * refuses: same key, different fingerprint → ``IdempotencyConflictError``
      (the contract ``IDEMPOTENCY_CONFLICT``), ``fn`` never called, row untouched;
    * runs: awaits ``fn()`` and writes ``(key, tool_name, fingerprint,
      response, expires_at = now + 7 days)`` in the caller's transaction, so
      the effect and its stored response commit together.

    ``fn`` is a zero-argument async callable returning a JSON-serialisable
    mapping; it closes over the same session, so its writes join the same
    transaction. Returns a frozen result whose
    ``.response`` is that mapping (fresh or stored) and whose ``.replayed``
    is ``False`` when ``fn`` ran and ``True`` when the stored row answered —
    so the pipeline can write ``tool.replayed`` instead of falsely repeating
    the declared event.

An expired row is absent to the guard: the effect runs again and the row is
replaced, leaving exactly one row per (tenant, key).

Style follows ``test_audit_writer.py``: schema from ``Base.metadata`` via
``_seed.schema_engine``, one ``asyncio.run`` per scenario, real concurrency
through ``asyncio.gather`` over separate sessions.
"""

import asyncio
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from helpdesk.mcp_server.services.idempotency.fingerprint import fingerprint
from helpdesk.mcp_server.services.idempotency.guard import (
    KEY_TTL,
    InvalidIdempotencyKeyError,
    TenantMismatchError,
    run_once,
)
from helpdesk.shared.audit import AuditEventInput
from helpdesk.shared.audit.writer import append_event
from helpdesk.shared.core.db import tenant_session
from helpdesk.shared.core.errors import ErrorCode, IdempotencyConflictError, ToolError
from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.idempotency import create_idempotency_key, find_idempotency_key
from helpdesk.shared.models import IdempotencyKey, Ticket
from helpdesk.shared.models.idempotency import KEY_LENGTH, TOOL_NAME_LENGTH
from tests.support.seed import seed_tenant

TOOL = "revoke_access"
KEY = "fake-idempotency-key-1"
ARGS: dict[str, Any] = {"grant_id": str(uuid.UUID(int=42)), "reason": "fake reason"}
OTHER_ARGS: dict[str, Any] = {"grant_id": str(uuid.UUID(int=42)), "reason": "different reason"}
RESPONSE: dict[str, Any] = {"revoked": True, "grant_id": str(uuid.UUID(int=42))}


class Effect:
    """A counted stand-in for a tool's side effect."""

    def __init__(self, response: Mapping[str, Any] | None = None, delay: float = 0.0) -> None:
        self.calls = 0
        self._response: Mapping[str, Any] = RESPONSE if response is None else response
        self._delay = delay

    async def __call__(self) -> Mapping[str, Any]:
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        return dict(self._response)


async def _stored(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, key: str = KEY
) -> IdempotencyKey | None:
    async with tenant_session(factory, tenant_id) as session:
        return await find_idempotency_key(session, key, datetime.now(UTC))


async def _all_rows(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> list[IdempotencyKey]:
    async with tenant_session(factory, tenant_id) as session:
        return list(await Repository(session, IdempotencyKey).list())


# --- the promised lifetime ----------------------------------------------------


def test_a_key_is_honoured_for_seven_days() -> None:
    """The contract names the window; a shorter one silently stops honouring
    retries the system promises to honour."""
    assert timedelta(days=7) == KEY_TTL


# --- rejected before anything runs --------------------------------------------


@pytest.mark.parametrize(
    ("key", "tool_name"),
    [
        pytest.param("", TOOL, id="empty-key"),
        pytest.param("k" * (KEY_LENGTH + 1), TOOL, id="oversized-key"),
        pytest.param(KEY, "", id="empty-tool-name"),
        pytest.param(KEY, "t" * (TOOL_NAME_LENGTH + 1), id="oversized-tool-name"),
    ],
)
def test_an_unstorable_key_or_tool_name_is_refused_before_the_effect(
    session_factory: async_sessionmaker[AsyncSession], key: str, tool_name: str
) -> None:
    """Left to the column length this would abort the flush after the effect
    had run, as a free-text database error rather than a typed code."""

    async def scenario() -> None:
        factory = session_factory
        seeded = await seed_tenant(factory, f"guard-bad-key-{len(key)}-{len(tool_name)}")
        effect = Effect()

        with pytest.raises(InvalidIdempotencyKeyError) as excinfo:
            async with tenant_session(factory, seeded.tenant_id) as session:
                await run_once(session, seeded.tenant_id, key, tool_name, ARGS, effect)

        assert excinfo.value.code == ErrorCode.VALIDATION
        assert effect.calls == 0
        assert await _all_rows(factory, seeded.tenant_id) == []

    asyncio.run(scenario())


def test_a_key_of_the_maximum_length_is_accepted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The boundary is inclusive: the check must match the column, not undercut it."""

    async def scenario() -> None:
        factory = session_factory
        seeded = await seed_tenant(factory, "guard-max-key")
        effect = Effect()
        key = "k" * KEY_LENGTH

        async with tenant_session(factory, seeded.tenant_id) as session:
            await run_once(session, seeded.tenant_id, key, TOOL, ARGS, effect)

        assert effect.calls == 1
        row = await _stored(factory, seeded.tenant_id, key)
        assert row is not None and row.key == key

    asyncio.run(scenario())


# --- first call ---------------------------------------------------------------


def test_first_call_runs_the_effect_and_stores_the_response(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def scenario() -> None:
        factory = session_factory
        seeded = await seed_tenant(factory, "guard-first")
        effect = Effect()
        before = datetime.now(UTC)

        async with tenant_session(factory, seeded.tenant_id) as session:
            result: Any = await run_once(session, seeded.tenant_id, KEY, TOOL, ARGS, effect)

        assert effect.calls == 1
        assert result.response == RESPONSE
        assert result.replayed is False

        row = await _stored(factory, seeded.tenant_id)
        assert row is not None
        assert row.tenant_id == seeded.tenant_id
        assert row.key == KEY
        assert row.tool_name == TOOL
        assert row.request_fingerprint == fingerprint(TOOL, ARGS)
        assert row.response == RESPONSE
        assert before + timedelta(days=6) < row.expires_at < before + timedelta(days=8), (
            f"keys are kept 7 days (SPEC §13.2), got {row.expires_at}"
        )

    asyncio.run(scenario())


def test_the_effect_and_the_stored_response_commit_together(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Same transaction: no committed effect without its response, or the reverse."""

    async def scenario() -> None:
        factory = session_factory
        seeded = await seed_tenant(factory, "guard-same-tx")
        ticket_ids: list[uuid.UUID] = []

        async with tenant_session(factory, seeded.tenant_id) as session:

            async def effect() -> Mapping[str, Any]:
                ticket = await Repository(session, Ticket).create(
                    user_id=seeded.user_id,
                    kind="general",
                    title="fake guarded ticket",
                    body="fake body",
                    status="open",
                    session_id=None,
                )
                ticket_ids.append(ticket.id)
                return {"ticket_id": str(ticket.id)}

            await run_once(session, seeded.tenant_id, KEY, "create_ticket", ARGS, effect)

        async with tenant_session(factory, seeded.tenant_id) as session:
            ticket = await Repository(session, Ticket).get(ticket_ids[0])
        assert ticket is not None, "the effect must be committed"

        row = await _stored(factory, seeded.tenant_id)
        assert row is not None and row.response == {"ticket_id": str(ticket_ids[0])}

    asyncio.run(scenario())


def test_a_rolled_back_transaction_leaves_neither_the_effect_nor_the_key(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The key must not survive a failure, or the retry would replay a response
    for an effect that never happened."""

    async def scenario() -> None:
        factory = session_factory
        seeded = await seed_tenant(factory, "guard-rollback")
        effect = Effect()

        with pytest.raises(RuntimeError, match="fake failure after the effect"):
            async with tenant_session(factory, seeded.tenant_id) as session:
                await run_once(session, seeded.tenant_id, KEY, TOOL, ARGS, effect)
                raise RuntimeError("fake failure after the effect")

        assert effect.calls == 1
        assert await _all_rows(factory, seeded.tenant_id) == []

        # The retry is a first call again, not a replay.
        async with tenant_session(factory, seeded.tenant_id) as session:
            result: Any = await run_once(session, seeded.tenant_id, KEY, TOOL, ARGS, effect)
        assert effect.calls == 2
        assert result.response == RESPONSE
        assert result.replayed is False

    asyncio.run(scenario())


# --- replay -------------------------------------------------------------------


def test_replay_returns_the_stored_response_without_running_the_effect(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def scenario() -> None:
        factory = session_factory
        seeded = await seed_tenant(factory, "guard-replay")
        effect = Effect()

        async with tenant_session(factory, seeded.tenant_id) as session:
            first: Any = await run_once(session, seeded.tenant_id, KEY, TOOL, ARGS, effect)
        assert first.replayed is False

        for _ in range(3):
            async with tenant_session(factory, seeded.tenant_id) as session:
                replayed: Any = await run_once(session, seeded.tenant_id, KEY, TOOL, ARGS, effect)
            assert replayed.response == first.response
            assert replayed.replayed is True

        assert effect.calls == 1, "a retry must never double-revoke"
        assert len(await _all_rows(factory, seeded.tenant_id)) == 1

    asyncio.run(scenario())


def test_replay_is_insensitive_to_argument_key_order(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The same request serialised differently is still the same request."""

    async def scenario() -> None:
        factory = session_factory
        seeded = await seed_tenant(factory, "guard-order")
        effect = Effect()

        async with tenant_session(factory, seeded.tenant_id) as session:
            await run_once(session, seeded.tenant_id, KEY, TOOL, ARGS, effect)

        shuffled = dict(reversed(list(ARGS.items())))
        async with tenant_session(factory, seeded.tenant_id) as session:
            replayed: Any = await run_once(session, seeded.tenant_id, KEY, TOOL, shuffled, effect)

        assert effect.calls == 1
        assert replayed.response == RESPONSE
        assert replayed.replayed is True

    asyncio.run(scenario())


def test_a_committed_call_replays_even_when_its_result_was_lost(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The contract's crash case: the process dies between commit and response;
    the client retries and must get the committed result, not a second effect."""

    async def scenario() -> None:
        factory = session_factory
        seeded = await seed_tenant(factory, "guard-crash")
        effect = Effect()

        async with tenant_session(factory, seeded.tenant_id) as session:
            # The caller "crashes" here: the return value is discarded and
            # never reaches the client, but the transaction committed.
            await run_once(session, seeded.tenant_id, KEY, TOOL, ARGS, effect)

        async with tenant_session(factory, seeded.tenant_id) as session:
            retried: Any = await run_once(session, seeded.tenant_id, KEY, TOOL, ARGS, effect)

        assert retried.response == RESPONSE
        assert retried.replayed is True
        assert effect.calls == 1

    asyncio.run(scenario())


# --- conflict (unhappy path) --------------------------------------------------


def test_same_key_different_fingerprint_raises_idempotency_conflict(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def scenario() -> None:
        factory = session_factory
        seeded = await seed_tenant(factory, "guard-conflict")
        effect = Effect()

        async with tenant_session(factory, seeded.tenant_id) as session:
            await run_once(session, seeded.tenant_id, KEY, TOOL, ARGS, effect)

        with pytest.raises(IdempotencyConflictError) as excinfo:
            async with tenant_session(factory, seeded.tenant_id) as session:
                await run_once(session, seeded.tenant_id, KEY, TOOL, OTHER_ARGS, effect)

        assert excinfo.value.code == ErrorCode.IDEMPOTENCY_CONFLICT
        assert isinstance(excinfo.value, ToolError)
        assert effect.calls == 1, "a conflicting request must not run the effect"

        rows = await _all_rows(factory, seeded.tenant_id)
        assert len(rows) == 1
        assert rows[0].request_fingerprint == fingerprint(TOOL, ARGS), "row was overwritten"
        assert rows[0].response == RESPONSE
        assert rows[0].tool_name == TOOL

    asyncio.run(scenario())


def test_the_same_key_reused_for_another_tool_is_a_conflict(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The tool name is part of the fingerprint, so one key cannot mean two actions."""

    async def scenario() -> None:
        factory = session_factory
        seeded = await seed_tenant(factory, "guard-conflict-tool")
        effect = Effect()

        async with tenant_session(factory, seeded.tenant_id) as session:
            await run_once(session, seeded.tenant_id, KEY, TOOL, ARGS, effect)

        with pytest.raises(IdempotencyConflictError):
            async with tenant_session(factory, seeded.tenant_id) as session:
                await run_once(session, seeded.tenant_id, KEY, "approve_request", ARGS, effect)

        assert effect.calls == 1

    asyncio.run(scenario())


def test_a_response_that_cannot_be_stored_fails_at_the_call_site(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A non-serialisable response must raise a typed error here, not at commit
    time far from the cause."""

    async def scenario() -> None:
        factory = session_factory
        seeded = await seed_tenant(factory, "guard-unserialisable")
        effect = Effect(response={"opaque": object()})

        with pytest.raises(ToolError):
            async with tenant_session(factory, seeded.tenant_id) as session:
                await run_once(session, seeded.tenant_id, KEY, TOOL, ARGS, effect)

    asyncio.run(scenario())


# --- concurrency --------------------------------------------------------------


def test_two_concurrent_calls_with_the_same_key_run_the_effect_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The loser blocks on the advisory lock, then replays the winner's row:
    not a second execution, and not an error either."""

    async def scenario() -> None:
        factory = session_factory
        seeded = await seed_tenant(factory, "guard-race")
        effect = Effect(delay=0.05)

        async def call() -> Any:
            async with tenant_session(factory, seeded.tenant_id) as session:
                return await run_once(session, seeded.tenant_id, KEY, TOOL, ARGS, effect)

        results = await asyncio.gather(call(), call(), return_exceptions=True)

        failures = [r for r in results if isinstance(r, BaseException)]
        assert not failures, f"a concurrent retry must not error: {failures!r}"
        outcomes = [r for r in results if not isinstance(r, BaseException)]
        assert [outcome.response for outcome in outcomes] == [RESPONSE, RESPONSE]
        # The winner ran the effect; the loser replayed the winner's row.
        assert sorted(outcome.replayed for outcome in outcomes) == [False, True]
        assert effect.calls == 1, "the effect ran twice under contention"
        assert len(await _all_rows(factory, seeded.tenant_id)) == 1

    asyncio.run(scenario())


def test_concurrent_calls_with_different_keys_both_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The lock is per (tenant, key): unrelated requests must not serialise into one."""

    async def scenario() -> None:
        factory = session_factory
        seeded = await seed_tenant(factory, "guard-race-keys")
        effect = Effect(delay=0.05)

        async def call(key: str) -> Any:
            async with tenant_session(factory, seeded.tenant_id) as session:
                return await run_once(session, seeded.tenant_id, key, TOOL, ARGS, effect)

        await asyncio.gather(call("fake-key-a"), call("fake-key-b"))

        assert effect.calls == 2
        assert len(await _all_rows(factory, seeded.tenant_id)) == 2

    asyncio.run(scenario())


def test_an_idempotency_lock_does_not_block_an_audit_append(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Distinct advisory-lock classids: while a
    guarded effect holds its lock, the same tenant's audit writer must proceed.
    A shared key space would deadlock this scenario into the timeout."""

    async def scenario() -> None:
        factory = session_factory
        seeded = await seed_tenant(factory, "guard-lock-classid")
        holding = asyncio.Event()
        appended = asyncio.Event()

        async def guarded() -> None:
            async with tenant_session(factory, seeded.tenant_id) as session:

                async def effect() -> Mapping[str, Any]:
                    holding.set()
                    await asyncio.wait_for(appended.wait(), timeout=10)
                    return RESPONSE

                await run_once(session, seeded.tenant_id, KEY, TOOL, ARGS, effect)

        async def audit() -> None:
            await asyncio.wait_for(holding.wait(), timeout=10)
            async with tenant_session(factory, seeded.tenant_id) as session:
                await append_event(
                    session,
                    seeded.tenant_id,
                    AuditEventInput(
                        occurred_at=None,
                        actor_kind="system",
                        actor_ref=None,
                        session_id=None,
                        trace_id="trace-fake-lock",
                        event_type="ticket.created",
                        subject_type="ticket",
                        subject_id=str(seeded.ticket_id),
                        payload={"fake": True},
                    ),
                )
            appended.set()

        await asyncio.wait_for(asyncio.gather(guarded(), audit()), timeout=20)

    asyncio.run(scenario())


# --- tenant scoping -----------------------------------------------------------


def test_a_tenant_argument_that_is_not_the_bound_one_is_refused(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The identity-from-token rule, as `append_event` does it: the tenant comes from the bound
    session, and a mismatched argument is refused rather than quietly ignored."""

    async def scenario() -> None:
        factory = session_factory
        seeded_a = await seed_tenant(factory, "guard-mismatch-a")
        seeded_b = await seed_tenant(factory, "guard-mismatch-b")
        effect = Effect()

        with pytest.raises(TenantMismatchError):
            async with tenant_session(factory, seeded_a.tenant_id) as session:
                await run_once(session, seeded_b.tenant_id, KEY, TOOL, ARGS, effect)

        assert effect.calls == 0
        assert await _all_rows(factory, seeded_a.tenant_id) == []
        assert await _all_rows(factory, seeded_b.tenant_id) == []

    asyncio.run(scenario())


def test_the_same_key_in_another_tenant_runs_independently(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def scenario() -> None:
        factory = session_factory
        seeded_a = await seed_tenant(factory, "guard-tenant-a")
        seeded_b = await seed_tenant(factory, "guard-tenant-b")
        effect = Effect()

        for seeded in (seeded_a, seeded_b):
            async with tenant_session(factory, seeded.tenant_id) as session:
                result: Any = await run_once(session, seeded.tenant_id, KEY, TOOL, ARGS, effect)
            assert result.response == RESPONSE
            assert result.replayed is False

        assert effect.calls == 2, "another tenant's key must not suppress the effect"
        assert len(await _all_rows(factory, seeded_a.tenant_id)) == 1
        assert len(await _all_rows(factory, seeded_b.tenant_id)) == 1

    asyncio.run(scenario())


# --- expiry -------------------------------------------------------------------


def test_an_expired_key_re_runs_the_effect_and_replaces_the_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def scenario() -> None:
        factory = session_factory
        seeded = await seed_tenant(factory, "guard-expired")
        effect = Effect()

        async with tenant_session(factory, seeded.tenant_id) as session:
            await create_idempotency_key(
                session,
                key=KEY,
                tool_name=TOOL,
                request_fingerprint=fingerprint(TOOL, ARGS),
                response={"revoked": "stale-fake-response"},
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )

        async with tenant_session(factory, seeded.tenant_id) as session:
            result: Any = await run_once(session, seeded.tenant_id, KEY, TOOL, ARGS, effect)

        assert effect.calls == 1, "an expired key must not replay"
        assert result.response == RESPONSE
        assert result.replayed is False

        rows = await _all_rows(factory, seeded.tenant_id)
        assert len(rows) == 1, f"the expired row must be replaced, not duplicated: {rows}"
        assert rows[0].response == RESPONSE
        assert rows[0].expires_at > datetime.now(UTC)

    asyncio.run(scenario())


def test_an_expired_key_with_a_different_fingerprint_is_not_a_conflict(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Expired means absent, so the key is free for a new request."""

    async def scenario() -> None:
        factory = session_factory
        seeded = await seed_tenant(factory, "guard-expired-conflict")
        effect = Effect()

        async with tenant_session(factory, seeded.tenant_id) as session:
            await create_idempotency_key(
                session,
                key=KEY,
                tool_name=TOOL,
                request_fingerprint=fingerprint(TOOL, OTHER_ARGS),
                response={"revoked": "stale-fake-response"},
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )

        async with tenant_session(factory, seeded.tenant_id) as session:
            result: Any = await run_once(session, seeded.tenant_id, KEY, TOOL, ARGS, effect)

        assert effect.calls == 1
        assert result.response == RESPONSE
        assert result.replayed is False

    asyncio.run(scenario())
