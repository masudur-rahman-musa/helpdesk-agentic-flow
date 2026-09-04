"""`dispatch()`: the one seam every MCP tool call passes through.

The order is fixed and pinned in both directions by the criterion-5 pair in
`test_mcp_pipeline.py`:

    identity -> kill switch -> RBAC -> input validation -> bound session ->
    tool -> audit -> output

Kill switch before RBAC is deliberate. The cost is that an unauthorised
caller learns a tool is stopped; the alternative is a stop that first asks
whether the caller was allowed, which is not a stop. RBAC before input
validation is the other half: a caller who may not call a tool learns
nothing about its argument shape.

Every failure leaves as one of the eight the design codes and NOTHING else —
no colon, no detail, no text drawn from the raised exception. That is what
`test_no_response_ever_carries_text_from_the_raised_exception` asserts, with
a leak marker planted in every synthetic failure. The detail is not thrown
away: it goes on the audit event, which is an internal record, not a
response. Anything that is not a `ToolError` maps to `INTERNAL`, because a
`TenantMismatchError`, a bare `RedisError` or an SSL error crossing this
boundary is a bug or an outage, not a decision about the caller.

`asyncio.CancelledError` is re-raised rather than converted: a cancelled
call did not fail, and converting it would fabricate a tool failure and
write an audit event for something that never finished.

Exactly one audit event per call (the design). The success event is written
inside the tool's own transaction, so a tool that fails after writing takes
its event with it; the failure event is then written from a FRESH session,
because an event that rolls back with the failure it records is not a record. A call whose token never verified writes nothing at
all: an unverifiable token names no tenant, and the audit log is partitioned
by tenant, so there is no chain to append to (decision E). The cost, stated
so it is a decision and not an oversight, is that a flood of invalid tokens
leaves no trace in the audit log.
"""

import asyncio
import enum
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from weakref import WeakKeyDictionary

import mcp.types as types
from helpdesk.audit import AuditEventInput, append_event
from helpdesk.auth import rbac
from helpdesk.core.db import (
    create_session_factory,
    dispose_shared_engines,
    shared_engine,
    tenant_session,
)
from helpdesk.core.settings import Settings, load_settings
from helpdesk.errors import ErrorCode, ToolError
from helpdesk.killswitch import KillSwitch
from pydantic import BaseModel, ValidationError
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request

from mcp_server.identity import Identity, identity_from_header
from mcp_server.registry import (
    DENIED_EVENT_TYPE,
    DISABLED_EVENT_TYPE,
    FAILED_EVENT_TYPE,
    ToolSpec,
)

logger = logging.getLogger(__name__)

_ACTOR_KIND = "user"
_SUBJECT_TYPE = "tool"


class _Stage(enum.Enum):
    """Where in the fixed order a call stopped.

    The audit event type is chosen from the stage, not from the exception
    type: a Redis outage raised while checking the switch is a failure, not
    an admin's stop, and calling it `tool.disabled` would make an outage read
    as a governance decision in the log.
    """

    KILL_SWITCH = "kill_switch"
    RBAC = "rbac"
    VALIDATION = "validation"
    BODY = "body"


def error_result(code: ErrorCode) -> types.CallToolResult:
    """An errored tool result carrying the code and nothing else."""
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=code.value)],
        is_error=True,
    )


def _success_result(output: BaseModel) -> types.CallToolResult:
    payload: dict[str, Any] = output.model_dump(mode="json")
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=output.model_dump_json())],
        structured_content=payload,
        is_error=False,
    )


async def dispatch(
    spec: ToolSpec[Any, Any],
    request: Request | None,
    arguments: Mapping[str, Any],
) -> types.CallToolResult:
    """Run one tool call through every guard, in the fixed order.

    `request` is the HTTP request the transport attached to the SDK's
    request context, so it is `None` under stdio — where there is no
    `Authorization` header and the call is therefore refused.
    """
    try:
        settings = load_settings()
        identity = identity_from_header(
            _authorization(request), settings.jwt_secret, datetime.now(UTC)
        )
    except ValidationError:
        # The process is misconfigured (no JWT_SECRET, no DATABASE_URL). No
        # identity, so no tenant and no audit chain; answering with the SDK's
        # own words would report a config outage as the client's mistake.
        logger.exception("the mcp server is misconfigured, refusing the call")
        return error_result(ErrorCode.INTERNAL)
    except ToolError as exc:
        # Decision E: no verified tenant, so no chain to append to, so no
        # audit event and no database session at all.
        return error_result(exc.code)

    stage = _Stage.KILL_SWITCH
    scope: str | None = None
    try:
        await _check_switch(settings, identity, spec)

        stage = _Stage.RBAC
        scope = rbac.authorize(identity.claims, spec.tool).scope.value

        stage = _Stage.VALIDATION
        args = spec.input_model.model_validate(dict(arguments))

        stage = _Stage.BODY
        result = await _call_tool(spec, settings, identity, args, scope)
    except ToolError as exc:
        return await _refuse(settings, identity, spec, stage, exc.code, scope, exc)
    except ValidationError as exc:
        # Only the argument parse earns VALIDATION. A tool body that parses
        # something of its own (a provider reply, a stored payload) and finds
        # it malformed has an upstream problem, not a caller with bad
        # arguments; calling that VALIDATION would tell the user their input
        # was wrong and record the same lie in the audit event.
        code = ErrorCode.VALIDATION if stage is _Stage.VALIDATION else ErrorCode.INTERNAL
        return await _refuse(settings, identity, spec, stage, code, scope, exc)
    except Exception as exc:
        # asyncio.CancelledError is a BaseException in 3.12, so it passes
        # through here untouched; that is the behaviour, not an oversight.
        return await _refuse(settings, identity, spec, stage, ErrorCode.INTERNAL, scope, exc)
    return result


def _authorization(request: Request | None) -> str | None:
    if request is None:
        return None
    return request.headers.get("authorization")


async def _check_switch(settings: Settings, identity: Identity, spec: ToolSpec[Any, Any]) -> None:
    """Is this tool on at all, for this tenant (the design)?

    The tenant comes from the token, never from an argument, so a caller
    cannot ask about (or trip) another tenant's switch.
    """
    await KillSwitch(_redis(settings)).check(identity.tenant_id, spec.name)


async def _call_tool(
    spec: ToolSpec[Any, Any],
    settings: Settings,
    identity: Identity,
    args: BaseModel,
    scope: str | None,
) -> types.CallToolResult:
    """The tool body and its success event, in one transaction.

    The event is appended inside the tool's own transaction on purpose: a
    call that fails after writing must not leave a "succeeded" event behind,
    and a committed action must never exist without its event.
    """
    factory = _session_factory(settings)
    async with tenant_session(factory, identity.tenant_id) as session:
        # `spec` is erased to `ToolSpec[Any, Any]` here; the handler's real
        # return type is checked against `output_model` where the spec is
        # declared, so this annotation narrows rather than assumes.
        output: BaseModel = await spec.handler(session, identity, args)
        # Rendered before the event is appended, not after the transaction
        # closes: an output that cannot be serialised must roll the success
        # event back with it, not leave `tool.succeeded` behind an error.
        result = _success_result(output)
        await _append(session, identity, spec, spec.event_type, scope, None, None)
        return result


async def _refuse(
    settings: Settings,
    identity: Identity,
    spec: ToolSpec[Any, Any],
    stage: _Stage,
    code: ErrorCode,
    scope: str | None,
    exc: BaseException,
) -> types.CallToolResult:
    """Record the failure, then answer with the code alone."""
    await _record_failure(settings, identity, spec, _event_type_for(stage, code), code, scope, exc)
    return error_result(code)


def _event_type_for(stage: _Stage, code: ErrorCode) -> str:
    """Which of the four event types this call earned.

    Only a stop at the kill switch is `tool.disabled` and only a refusal by
    the matrix is `tool.denied`; everything else, including a Redis outage
    while reading the switch, is `tool.failed`.
    """
    if stage is _Stage.KILL_SWITCH and code is ErrorCode.TOOL_DISABLED:
        return DISABLED_EVENT_TYPE
    if stage is _Stage.RBAC and code is ErrorCode.FORBIDDEN:
        return DENIED_EVENT_TYPE
    return FAILED_EVENT_TYPE


async def _record_failure(
    settings: Settings,
    identity: Identity,
    spec: ToolSpec[Any, Any],
    event_type: str,
    code: ErrorCode,
    scope: str | None,
    exc: BaseException,
) -> None:
    """Write the failure event from a fresh session (decision D).

    The tool's own transaction is already rolled back by the time this runs,
    so this session is the only one that can carry the record. If this write
    also fails the caller still gets its typed code and the loss is logged —
    never swallowed, and never turned into a different answer.
    """
    try:
        factory = _session_factory(settings)
        async with tenant_session(factory, identity.tenant_id) as session:
            await _append(session, identity, spec, event_type, scope, code, exc)
    except Exception:
        logger.exception("could not write the %s audit event for tool %s", event_type, spec.name)


async def _append(
    session: AsyncSession,
    identity: Identity,
    spec: ToolSpec[Any, Any],
    event_type: str,
    scope: str | None,
    code: ErrorCode | None,
    exc: BaseException | None,
) -> None:
    """One audit event for one call.

    `detail` is the only place the underlying exception's own words are kept.
    That is deliberate: the response carries the code alone, and the record
    of why has to live somewhere an operator can read it.
    """
    payload: dict[str, Any] = {"tool": spec.name, "scope": scope}
    if code is not None:
        payload["code"] = code.value
    if exc is not None:
        payload["detail"] = f"{type(exc).__name__}: {exc}"
    await append_event(
        session,
        identity.tenant_id,
        AuditEventInput(
            occurred_at=datetime.now(UTC),
            actor_kind=_ACTOR_KIND,
            actor_ref=str(identity.user_id),
            session_id=identity.session_id,
            trace_id=None,
            event_type=event_type,
            subject_type=_SUBJECT_TYPE,
            subject_id=spec.name,
            payload=payload,
        ),
    )


def _session_factory(settings: Settings) -> async_sessionmaker[AsyncSession]:
    """The session factory for this process (and this event loop).

    A named module-level function rather than an inline call so a test can
    spy on it: criterion 2 says a refused call opens no session, and a spy
    that is never called is the only way to prove that.
    """
    return create_session_factory(shared_engine(settings.database_url))


REDIS_TIMEOUT_SECONDS = 2
"""Connect and read timeout for the kill-switch client.

Without it a blackholed Redis (packets dropped rather than refused) hangs
every tool call forever inside `KillSwitch.check`, instead of the `INTERNAL`
the fail-closed design promises. Pinned exactly by
`test_mcp_pipeline.py::test_the_redis_client_carries_the_pinned_timeouts`."""

_SHARED_REDIS: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, Redis]] = WeakKeyDictionary()


def _redis(settings: Settings) -> Redis:
    """A process-shared Redis client for the running event loop.

    Keyed by (event loop, URL) for the same reason `helpdesk.core.db`'s
    `shared_engine` is: a pooled connection belongs to the loop that opened
    it. Under a server this is one client per process; under a test that
    drives each call through its own `asyncio.run`, it is one per loop.
    Entries drop with their loop.
    """
    loop = asyncio.get_running_loop()
    clients = _SHARED_REDIS.setdefault(loop, {})
    client = clients.get(settings.redis_url)
    if client is None:
        client = Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=REDIS_TIMEOUT_SECONDS,
            socket_timeout=REDIS_TIMEOUT_SECONDS,
        )
        clients[settings.redis_url] = client
    return client


async def close_shared_connections() -> None:
    """Close the running loop's pooled connections, Postgres and Redis alike.

    A server calls this never (its loop outlives the process's work); a test
    driving each call through its own `asyncio.run` calls it at the end of
    every scenario, because a pool belonging to a dead loop is a connection
    nothing will ever close.
    """
    loop = asyncio.get_running_loop()
    for client in _SHARED_REDIS.pop(loop, {}).values():
        await client.aclose()
    await dispose_shared_engines()


__all__ = ["close_shared_connections", "dispatch", "error_result"]
