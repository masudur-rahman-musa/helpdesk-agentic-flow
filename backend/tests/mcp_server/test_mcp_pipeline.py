"""An earlier change, section 1 criteria 5, 6, 7 and 8, section 6: `dispatch()`.

Surface under test (written before it exists):

``mcp_server.tools.dispatch``
    ``async def dispatch(spec: ToolSpec[Any, Any], request: Request | None,
    arguments: Mapping[str, Any]) -> mcp.types.CallToolResult``

    Fixed order, pinned in both directions by the criterion-5 pair below:
    identity -> registry lookup -> ``KillSwitch.check`` -> ``rbac.authorize``
    -> input model validation -> ``tenant_session`` bound from the JWT ->
    handler -> audit -> output.

    ``request`` is the HTTP request the transport attached to the SDK's
    ``ServerRequestContext`` (``ctx.request``), so it is ``None`` under stdio,
    where there is no ``Authorization`` header and the call is refused.

    Every failure returns ``CallToolResult(is_error=True,
    content=[TextContent(text=<CODE>)])`` where ``<CODE>`` is EXACTLY one of
    the eight ``helpdesk.errors.ErrorCode`` values and nothing else: no colon,
    no detail, no text drawn from the underlying exception. Success returns
    ``is_error=False`` with ``structured_content ==
    output.model_dump(mode="json")``.

    Non-``ToolError`` escapes map to ``INTERNAL``.
    ``asyncio.CancelledError`` is re-raised, never converted: converting it
    would turn a shutdown into a fabricated tool failure and write an audit
    event for a call that never finished.

    Seams: ``pipeline._session_factory(settings)`` and
    ``pipeline._redis(settings)``.

``mcp_server.tools.registry.ToolSpec`` — a frozen dataclass whose ``handler`` is
``(AsyncSession, Identity, I) -> Awaitable[O]``. The synthetic specs below are
built directly rather than registered, so the pipeline's guarantees can be
tested with a handler that raises, or an input model that can be violated,
without either leaking into the real tool surface.

Audit event types (criterion 7): success writes ``spec.event_type``, an RBAC
denial writes ``registry.DENIED_EVENT_TYPE``, a tripped switch writes
``registry.DISABLED_EVENT_TYPE``, a raising tool writes
``registry.FAILED_EVENT_TYPE`` — the last one from a fresh session, so it
survives the rollback that takes the tool's own writes with it (decision D).

No pytest-asyncio: each case is a sync function driving one ``asyncio.run``.
"""

import asyncio
import logging
import ssl
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import pytest
import redis.exceptions
from pydantic import BaseModel, ValidationError
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from starlette.requests import Request

from helpdesk.mcp_server.core.identity import Identity
from helpdesk.mcp_server.tools import dispatch as pipeline
from helpdesk.mcp_server.tools import registry
from helpdesk.mcp_server.tools.dispatch import dispatch
from helpdesk.mcp_server.tools.registry import ToolSpec
from helpdesk.shared.audit.writer import TenantMismatchError
from helpdesk.shared.auth import rbac
from helpdesk.shared.core.repository import Repository
from helpdesk.shared.core.settings import load_settings
from helpdesk.shared.errors import ErrorCode, ToolError
from helpdesk.shared.killswitch import GLOBAL, KillSwitch
from helpdesk.shared.models import Ticket
from tests.support.mcp import (  # noqa: F401 - fixtures are resolved by name
    SeededMcp,
    bearer,
    call_tool,
    error_code,
    mcp_env,
    mcp_pg,
    mcp_redis_base_url,
    mcp_redis_url,
    seeded_mcp_db,
    token_for,
    token_with_claims,
)

# A message no code path may ever copy into a response. Every synthetic failure
# below carries it, so "the response contains only the code" is checked against
# a string that would be visibly wrong if it leaked.
LEAK_MARKER = "leaky-detail-that-must-never-cross-the-boundary"

# The killswitch tool name the synthetic specs authorise and stop against.
SPEC_TOOL = rbac.Tool.GET_ME

# A caller no principal in the matrix recognises: `authorize` ignores unknown
# principal strings, so an empty allowing set denies. This is the "unpermitted
# caller" of the criterion-5 order pair, since both 10a tools allow every real
# principal.
NO_REAL_PRINCIPALS: tuple[str, ...] = ("not_a_principal",)


class NoArgs(BaseModel):
    pass


class NeedsCount(BaseModel):
    """An input a caller can actually violate, so VALIDATION is reachable."""

    count: int


class Echo(BaseModel):
    ok: bool


async def _ok_handler(session: AsyncSession, identity: Identity, args: Any) -> Echo:
    return Echo(ok=True)


def _spec(
    handler: Callable[[AsyncSession, Identity, Any], Awaitable[Echo]],
    *,
    input_model: type[BaseModel] = NoArgs,
    event_type: str = "tool.succeeded",
) -> ToolSpec[Any, Any]:
    """A synthetic registry entry, so the pipeline can be tested apart from a tool."""
    return ToolSpec(
        name=SPEC_TOOL.value,
        tool=SPEC_TOOL,
        description="synthetic spec, tests only",
        input_model=input_model,
        output_model=Echo,
        event_type=event_type,
        mutating=False,
        handler=handler,
    )


def _request(headers: Mapping[str, str]) -> Request:
    """A Starlette request carrying exactly these headers, no server needed."""
    scope: dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "root_path": "",
        "headers": [(name.lower().encode(), value.encode()) for name, value in headers.items()],
        "client": ("127.0.0.1", 54321),
        "server": ("127.0.0.1", 8000),
    }
    return Request(scope)


def _dispatch(
    spec: ToolSpec[Any, Any],
    headers: Mapping[str, str],
    arguments: Mapping[str, Any] | None = None,
) -> Any:
    async def scenario() -> Any:
        try:
            return await dispatch(spec, _request(headers), dict(arguments or {}))
        finally:
            # The loop dies with this call; its pooled connections must not
            # outlive it (see `pipeline.close_shared_connections`).
            await pipeline.close_shared_connections()

    return asyncio.run(scenario())


def _trip_global_switch(redis_url: str, tool: str) -> None:
    async def scenario() -> None:
        client: Redis = Redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
        try:
            await KillSwitch(client).trip(
                GLOBAL, tool, "fake incident 0001, tools paused", "fake-admin@example.invalid"
            )
        finally:
            await client.aclose()

    asyncio.run(scenario())


def _audit_event_types(database_url: str, tenant_id: uuid.UUID) -> list[str]:
    """That tenant's audit event types in seq order, read with raw SQL."""

    async def scenario() -> list[str]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as conn:
                rows = await conn.execute(
                    text(
                        "SELECT event_type FROM audit_events WHERE tenant_id = :tenant ORDER BY seq"
                    ),
                    {"tenant": tenant_id},
                )
                return [str(row[0]) for row in rows]
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _audit_payloads(database_url: str, tenant_id: uuid.UUID) -> list[dict[str, Any]]:
    """That tenant's audit payloads in seq order."""

    async def scenario() -> list[dict[str, Any]]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as conn:
                rows = await conn.execute(
                    text("SELECT payload FROM audit_events WHERE tenant_id = :tenant ORDER BY seq"),
                    {"tenant": tenant_id},
                )
                return [dict(row[0]) for row in rows]
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _audit_count_all(database_url: str) -> int:
    async def scenario() -> int:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(text("SELECT count(*) FROM audit_events"))
                return int(result.scalar_one())
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _ticket_count(database_url: str, title: str) -> int:
    async def scenario() -> int:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(
                    text("SELECT count(*) FROM tickets WHERE title = :title"), {"title": title}
                )
                return int(result.scalar_one())
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


# --- criterion 5: the order pair, both directions -----------------------------


def test_a_tripped_switch_beats_an_rbac_denial(
    mcp_env: SeededMcp,  # noqa: F811
    mcp_redis_url: str,  # noqa: F811
) -> None:
    """Kill switch runs BEFORE RBAC: the stop is unconditional.

    A stop that first asks whether the caller was allowed is not a stop, so
    the disabled tool must answer TOOL_DISABLED even to a caller who would
    otherwise be refused FORBIDDEN.
    """
    _trip_global_switch(mcp_redis_url, SPEC_TOOL.value)
    token = token_for(mcp_env.tenant_a, principals=NO_REAL_PRINCIPALS)

    result = _dispatch(_spec(_ok_handler), bearer(token))

    assert error_code(result) == "TOOL_DISABLED"


def test_a_tripped_switch_beats_an_rbac_denial_through_the_transport(
    mcp_env: SeededMcp,  # noqa: F811
    mcp_redis_url: str,  # noqa: F811
) -> None:
    _trip_global_switch(mcp_redis_url, "get_me")
    token = token_for(mcp_env.tenant_a, principals=NO_REAL_PRINCIPALS)

    result = call_tool(bearer(token), "get_me", {})

    assert error_code(result) == "TOOL_DISABLED"


def test_an_rbac_denial_beats_invalid_arguments(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """RBAC runs BEFORE input validation: a caller who may not call the tool
    learns nothing about the tool's argument shape."""
    token = token_for(mcp_env.tenant_a, principals=NO_REAL_PRINCIPALS)

    result = _dispatch(_spec(_ok_handler, input_model=NeedsCount), bearer(token), {"count": "no"})

    assert error_code(result) == "FORBIDDEN"


def test_a_permitted_caller_sending_invalid_arguments_gets_validation(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """The other half of the pair: VALIDATION is reachable, so the test above
    pins an order rather than a code that could never appear."""
    token = token_for(mcp_env.tenant_a)

    result = _dispatch(_spec(_ok_handler, input_model=NeedsCount), bearer(token), {"count": "no"})

    assert error_code(result) == "VALIDATION"


def test_extra_arguments_are_ignored_not_rejected(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """`GetMeInput` has no fields and ignores extras: identity
    arrives on the token, so an argument naming a user is data to drop, not a
    reason to fail."""
    token = token_for(mcp_env.tenant_a)

    result = _dispatch(_spec(_ok_handler), bearer(token), {"user_id": str(uuid.uuid4())})

    assert result.is_error is False, result.content
    assert result.structured_content == {"ok": True}


# --- criterion 6: the error-mapping table -------------------------------------


def _raiser(exc: BaseException) -> Callable[[AsyncSession, Identity, Any], Awaitable[Echo]]:
    async def handler(session: AsyncSession, identity: Identity, args: Any) -> Echo:
        raise exc

    return handler


def _typed_error(code: ErrorCode) -> type[ToolError]:
    """A `ToolError` subclass declared for exactly one code.

    Declared per code rather than importing the existing subclasses so the
    table is exhaustive over the eight §8.1 values: a ninth code added to the
    enum fails here rather than silently going unmapped.
    """
    return type(f"_Test{code.value.title().replace('_', '')}Error", (ToolError,), {"code": code})


TOOL_ERROR_CASES = [(code, _typed_error(code)) for code in ErrorCode]


class _NeedsFlag(BaseModel):
    flag: bool


def _pydantic_validation_error() -> BaseException:
    """A pydantic ValidationError of the kind a tool body raises when it
    parses something of its own, not the caller's arguments."""
    try:
        _NeedsFlag.model_validate({"flag": LEAK_MARKER})
    except ValidationError as exc:
        return exc
    raise AssertionError("expected a ValidationError")


ESCAPE_CASES: tuple[tuple[str, BaseException], ...] = (
    ("TenantMismatchError from either tenant guard", TenantMismatchError(LEAK_MARKER)),
    ("a ValidationError raised inside the tool body", _pydantic_validation_error()),
    ("a bare RedisError", redis.exceptions.RedisError(LEAK_MARKER)),
    ("an SSL error reaching a dependency", ssl.SSLError(LEAK_MARKER)),
    ("an unexpected RuntimeError", RuntimeError(LEAK_MARKER)),
)


@pytest.mark.parametrize(("code", "error_type"), TOOL_ERROR_CASES, ids=[c.value for c in ErrorCode])
def test_every_tool_error_passes_its_own_code_through(
    mcp_env: SeededMcp,  # noqa: F811
    code: ErrorCode,
    error_type: type[ToolError],
) -> None:
    token = token_for(mcp_env.tenant_a)

    result = _dispatch(_spec(_raiser(error_type(LEAK_MARKER))), bearer(token))

    assert error_code(result) == code.value


@pytest.mark.parametrize(("label", "exc"), ESCAPE_CASES, ids=[label for label, _ in ESCAPE_CASES])
def test_a_non_tool_error_escape_maps_to_internal(
    mcp_env: SeededMcp,  # noqa: F811
    label: str,
    exc: BaseException,
) -> None:
    token = token_for(mcp_env.tenant_a)

    result = _dispatch(_spec(_raiser(exc)), bearer(token))

    assert error_code(result) == "INTERNAL", label


@pytest.mark.parametrize(
    ("label", "exc"),
    [
        *ESCAPE_CASES,
        *[(code.value, error_type(LEAK_MARKER)) for code, error_type in TOOL_ERROR_CASES],
    ],
    ids=[label for label, _ in ESCAPE_CASES] + [code.value for code in ErrorCode],
)
def test_no_response_ever_carries_text_from_the_raised_exception(
    mcp_env: SeededMcp,  # noqa: F811
    label: str,
    exc: BaseException,
) -> None:
    """The content text is the code and nothing else: no colon, no detail."""
    token = token_for(mcp_env.tenant_a)

    result = _dispatch(_spec(_raiser(exc)), bearer(token))

    text_out = error_code(result)
    assert text_out in {code.value for code in ErrorCode}, label
    assert LEAK_MARKER not in text_out, label
    assert text_out.strip() == text_out, label


def test_cancelled_error_is_re_raised_never_converted(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """A cancelled call did not fail; converting it fabricates a tool failure
    and writes an audit event for something that never finished."""
    token = token_for(mcp_env.tenant_a)

    with pytest.raises(asyncio.CancelledError):
        _dispatch(_spec(_raiser(asyncio.CancelledError())), bearer(token))


def test_a_call_with_no_http_request_is_refused(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Under stdio the transport attaches no request, so there is no header
    and therefore no identity. The stdio entrypoint lists tools; it cannot
    call one."""

    async def scenario() -> Any:
        try:
            return await dispatch(_spec(_ok_handler), None, {})
        finally:
            await pipeline.close_shared_connections()

    assert error_code(asyncio.run(scenario())) == "FORBIDDEN"


def test_a_failed_audit_write_is_logged_and_changes_no_answer(
    mcp_env: SeededMcp,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Decision D's second half: if the failure event cannot be written
    either, the caller still gets its typed code and the loss is logged,
    never silently dropped."""
    token = token_for(mcp_env.tenant_a)

    def unusable(settings: Any) -> Any:
        raise OSError(LEAK_MARKER)

    with (
        caplog.at_level(logging.ERROR, logger="helpdesk.mcp_server.tools.dispatch"),
        pytest.MonkeyPatch.context() as patch,
    ):
        patch.setattr(pipeline, "_session_factory", unusable)
        result = _dispatch(_spec(_ok_handler), bearer(token))

    assert error_code(result) == "INTERNAL"
    assert any("audit event" in record.getMessage() for record in caplog.records), (
        "the lost audit event must be logged"
    )


# --- criterion 7: exactly one audit event per call ----------------------------


def test_a_successful_call_writes_exactly_one_success_event(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    tenant = mcp_env.tenant_a.tenant_id
    before = _audit_event_types(mcp_env.database_url, tenant)

    result = call_tool(bearer(token_for(mcp_env.tenant_a)), "get_me", {})

    assert result.is_error is False, result.content
    after = _audit_event_types(mcp_env.database_url, tenant)
    assert after == [*before, "tool.succeeded"]


def test_an_rbac_denial_writes_exactly_one_denied_event(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    tenant = mcp_env.tenant_a.tenant_id
    before = _audit_event_types(mcp_env.database_url, tenant)
    token = token_for(mcp_env.tenant_a, principals=NO_REAL_PRINCIPALS)

    result = call_tool(bearer(token), "get_me", {})

    assert error_code(result) == "FORBIDDEN"
    after = _audit_event_types(mcp_env.database_url, tenant)
    assert after == [*before, registry.DENIED_EVENT_TYPE]
    assert registry.DENIED_EVENT_TYPE == "tool.denied"


def test_a_tripped_switch_writes_exactly_one_disabled_event(
    mcp_env: SeededMcp,  # noqa: F811
    mcp_redis_url: str,  # noqa: F811
) -> None:
    tenant = mcp_env.tenant_a.tenant_id
    _trip_global_switch(mcp_redis_url, "get_me")
    before = _audit_event_types(mcp_env.database_url, tenant)

    result = call_tool(bearer(token_for(mcp_env.tenant_a)), "get_me", {})

    assert error_code(result) == "TOOL_DISABLED"
    after = _audit_event_types(mcp_env.database_url, tenant)
    assert after == [*before, registry.DISABLED_EVENT_TYPE]
    assert registry.DISABLED_EVENT_TYPE == "tool.disabled"


def test_a_raising_tool_writes_exactly_one_failed_event(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    tenant = mcp_env.tenant_a.tenant_id
    before = _audit_event_types(mcp_env.database_url, tenant)
    token = token_for(mcp_env.tenant_a)

    result = _dispatch(_spec(_raiser(RuntimeError(LEAK_MARKER))), bearer(token))

    assert error_code(result) == "INTERNAL"
    after = _audit_event_types(mcp_env.database_url, tenant)
    assert after == [*before, registry.FAILED_EVENT_TYPE]
    assert registry.FAILED_EVENT_TYPE == "tool.failed"


def test_the_failure_event_survives_the_rolled_back_transaction(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Decision D: an event that rolls back with the failure it records is not
    a record. The tool writes a row and then raises; the row must be gone and
    the event must be there."""
    title = f"fake rollback probe {uuid.uuid4()}"
    tenant = mcp_env.tenant_a.tenant_id

    async def writes_then_raises(session: AsyncSession, identity: Identity, args: Any) -> Echo:
        await Repository(session, Ticket).create(
            user_id=identity.user_id,
            kind="general",
            title=title,
            body="written by a tool that is about to raise",
            status="open",
            session_id=None,
        )
        raise RuntimeError(LEAK_MARKER)

    before = _audit_event_types(mcp_env.database_url, tenant)

    result = _dispatch(_spec(writes_then_raises), bearer(token_for(mcp_env.tenant_a)))

    assert error_code(result) == "INTERNAL"
    assert _ticket_count(mcp_env.database_url, title) == 0, "the tool's write must roll back"
    after = _audit_event_types(mcp_env.database_url, tenant)
    assert after == [*before, registry.FAILED_EVENT_TYPE], "the failure event must survive"


def test_the_success_event_records_the_authorized_scope(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """An earlier change decision G: the scope the matrix allowed is recorded now and
    enforced at row 11. A decision no test holds is not a decision."""
    tenant = mcp_env.tenant_a.tenant_id

    result = call_tool(bearer(token_for(mcp_env.tenant_a)), "get_me", {})

    assert result.is_error is False, result.content
    latest = _audit_payloads(mcp_env.database_url, tenant)[-1]
    assert latest["tool"] == "get_me"
    assert latest["scope"] == rbac.Scope.SELF_ONLY.value
    assert latest["scope"] == "self_only"


def test_a_misconfigured_process_answers_internal_not_the_sdks_own_words(
    mcp_env: SeededMcp,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real misconfiguration, not a faked seam: a JWT secret under the
    32-byte floor makes `load_settings()` raise. Outside the guarded block
    that escaped `dispatch` and the SDK answered with its own text about the
    client's params, which is neither one of the eight codes nor true."""
    monkeypatch.setenv("JWT_SECRET", "too-short")

    result = _dispatch(_spec(_ok_handler), bearer(token_for(mcp_env.tenant_a)))

    assert error_code(result) == "INTERNAL"


def test_the_redis_client_carries_the_pinned_timeouts(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """A blackholed Redis must fail the call, not hang it forever.

    Asserted on the client the pipeline actually builds, and at the exact
    value: a timeout that is merely "set" tells you nothing about whether it
    is 2 seconds or 2 hours.
    """

    async def scenario() -> dict[str, Any]:
        try:
            client = pipeline._redis(load_settings())
            kwargs: dict[str, Any] = client.connection_pool.connection_kwargs
            return dict(kwargs)
        finally:
            await pipeline.close_shared_connections()

    kwargs = asyncio.run(scenario())

    assert pipeline.REDIS_TIMEOUT_SECONDS == 2
    assert kwargs["socket_connect_timeout"] == 2
    assert kwargs["socket_timeout"] == 2


def test_an_unverifiable_token_writes_no_audit_row_anywhere(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Decision E: an unverifiable token names no tenant, so there is no chain
    to append to. The cost (a token flood leaves no trace) is a decision, not
    an oversight."""
    before = _audit_count_all(mcp_env.database_url)

    result = call_tool({"Authorization": "Bearer not-a-jwt"}, "get_me", {})

    assert error_code(result) == "FORBIDDEN"
    assert _audit_count_all(mcp_env.database_url) == before


def test_a_token_whose_tenant_claim_is_not_a_uuid_writes_nothing(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Same reasoning one step further in: the signature verified but the
    tenant did not parse, so there is still no chain to append to."""
    before = _audit_count_all(mcp_env.database_url)
    token = token_with_claims(mcp_env.tenant_a, {"tenant_id": "not-a-uuid"}, now=datetime.now(UTC))

    result = call_tool(bearer(token), "get_me", {})

    assert error_code(result) == "FORBIDDEN"
    assert _audit_count_all(mcp_env.database_url) == before
