"""`dispatch()`: the one path every MCP tool call passes through.

    identity -> kill switch -> RBAC -> input validation -> bound session ->
    tool -> audit -> output

The kill switch comes first: a stop that asks who you are is not a stop.
RBAC comes before validation, so a refused caller learns nothing about a
tool's inputs. Failures answer with one typed code and no other text.
A call with an unverified token writes no audit event: there is no
tenant to file it under.
"""

import enum
import logging
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import mcp.types as types
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request

from helpdesk.mcp_server.core.identity import Identity, identity_from_header
from helpdesk.mcp_server.services.idempotency.guard import run_once
from helpdesk.mcp_server.tools.base import MutatingToolInput
from helpdesk.mcp_server.tools.context import ToolClients, ToolContext
from helpdesk.mcp_server.tools.registry import (
    DENIED_EVENT_TYPE,
    DISABLED_EVENT_TYPE,
    FAILED_EVENT_TYPE,
    REPLAYED_EVENT_TYPE,
    ToolSpec,
)
from helpdesk.shared.access.directory import SimulatedDirectory
from helpdesk.shared.audit import AuditEventInput, append_event
from helpdesk.shared.auth import rbac
from helpdesk.shared.core.config import Settings, load_settings
from helpdesk.shared.core.db import (
    create_session_factory,
    dispose_shared_engines,
    shared_engine,
    tenant_session,
)
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.core.observability import metrics
from helpdesk.shared.core.observability.context import bind_context
from helpdesk.shared.core.redis import close_loop_clients, get_redis
from helpdesk.shared.services.killswitch import KillSwitch

logger = logging.getLogger(__name__)

_OK_STATUS = "ok"
"""The `status` metric label for success; every other value is a code."""

_ACTOR_KIND = "user"
_SUBJECT_TYPE = "tool"


class _Stage(enum.Enum):
    """Where in the fixed order a call stopped. The audit event type follows
    the stage, so an outage is never logged as an admin's stop."""

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
    """Run one tool call through every guard. Under stdio `request` is None."""
    # One clock read for the whole call, so its steps cannot drift apart.
    now = datetime.now(UTC)
    try:
        settings = load_settings()
        identity = identity_from_header(_authorization(request), settings.jwt_secret, now)
    except ValidationError:
        # The server is misconfigured. Answer INTERNAL, not the client's fault.
        logger.exception("the mcp server is misconfigured, refusing the call")
        return error_result(ErrorCode.INTERNAL)
    except ToolError as exc:
        # No verified tenant, so no audit event and no session.
        return error_result(exc.code)

    stage = _Stage.KILL_SWITCH
    scope: str | None = None
    # Log lines carry this call's tenant and session, refusals included. The
    # user id is left out on purpose: telemetry never names a person.
    with bind_context(
        tenant_id=str(identity.tenant_id),
        session_id=str(identity.session_id),
        principal_kind=_principal_kind(identity),
    ):
        # Metrics are recorded once, in `finally`, for every exit. `status`
        # stays None for a cancelled call, which keeps it out of the numbers.
        status: str | None = None
        started = time.perf_counter()
        try:
            await _check_switch(settings, identity, spec)

            stage = _Stage.RBAC
            scope = rbac.authorize(identity.claims, spec.tool).scope.value

            stage = _Stage.VALIDATION
            args = spec.input_model.model_validate(dict(arguments))

            stage = _Stage.BODY
            clients = _tool_clients(request, settings)
            result = await _call_tool(spec, settings, identity, args, scope, now, clients)
            status = _OK_STATUS
        except ToolError as exc:
            status = exc.code.value
            return await _refuse(settings, identity, spec, stage, exc.code, scope, exc, now)
        except ValidationError as exc:
            # Only the argument parse earns VALIDATION. A bad parse inside the
            # tool body is our problem, not the caller's input.
            code = ErrorCode.VALIDATION if stage is _Stage.VALIDATION else ErrorCode.INTERNAL
            status = code.value
            return await _refuse(settings, identity, spec, stage, code, scope, exc, now)
        except Exception as exc:
            # CancelledError is a BaseException, so it passes through on purpose.
            status = ErrorCode.INTERNAL.value
            return await _refuse(
                settings, identity, spec, stage, ErrorCode.INTERNAL, scope, exc, now
            )
        finally:
            if status is not None:
                metrics.record_tool_call(
                    tool=spec.name,
                    status=status,
                    duration_seconds=time.perf_counter() - started,
                )
                if status == ErrorCode.SOD_CONFLICT.value:
                    # Counted from the answered code: the policy code is pure,
                    # and the raising transaction may still roll back.
                    metrics.record_sod_block()
        return result


def _principal_kind(identity: Identity) -> str | None:
    """The caller's principals, sorted and joined, as one log field."""
    if not identity.claims.principals:
        return None
    return ",".join(sorted(identity.claims.principals))


def _authorization(request: Request | None) -> str | None:
    if request is None:
        return None
    return request.headers.get("authorization")


def _tool_clients(request: Request | None, settings: Settings) -> ToolClients:
    """The app's shared clients if a server opened them, else a fresh copy.
    `request.app` raises with no app in scope, so read the scope."""
    if request is not None:
        app = request.scope.get("app")
        if app is not None:
            clients = getattr(app.state, "tool_clients", None)
            if isinstance(clients, ToolClients):
                return clients
    return ToolClients(settings)


async def _check_switch(settings: Settings, identity: Identity, spec: ToolSpec[Any, Any]) -> None:
    """Is this tool on at all, for the token's tenant?"""
    await KillSwitch(get_redis(settings)).check(identity.tenant_id, spec.name)


async def _call_tool(
    spec: ToolSpec[Any, Any],
    settings: Settings,
    identity: Identity,
    args: BaseModel,
    scope: str | None,
    now: datetime,
    clients: ToolClients,
) -> types.CallToolResult:
    """The tool body and its success event, in one transaction, so a failed
    tool leaves no success event."""
    factory = _session_factory(settings)
    async with tenant_session(factory, identity.tenant_id) as session:
        ctx = ToolContext(
            session=session,
            identity=identity,
            directory=SimulatedDirectory(session),
            now=now,
            settings=settings,
            clients=clients,
        )
        if spec.mutating:
            return await _run_mutating_tool(spec, ctx, args, scope)
        # The return type is checked against `output_model` where the spec
        # is declared.
        output: BaseModel = await spec.handler(ctx, args)
        # Render first, so an output that cannot serialise rolls back too.
        result = _success_result(output)
        await _append(session, identity, spec, spec.event_type, scope, None, None, now)
        return result


NOTHING_DONE_FIELD = "already_requested"
"""A mutating output with this field true changed nothing, so it writes no event."""


async def _run_mutating_tool(
    spec: ToolSpec[Any, Any],
    ctx: ToolContext,
    args: BaseModel,
    scope: str | None,
) -> types.CallToolResult:
    """The tool body inside `run_once`, so a retry never repeats the effect.

    Runs after the kill switch and RBAC, so a refused call spends no key.
    A replay logs `tool.replayed`, not the event again.
    """
    if not isinstance(args, MutatingToolInput):
        # A wiring bug, so INTERNAL, never the caller's mistake.
        raise TypeError(f"mutating tool {spec.name} must take a MutatingToolInput")

    async def effect() -> Mapping[str, Any]:
        output: BaseModel = await spec.handler(ctx, args)
        dumped: dict[str, Any] = output.model_dump(mode="json")
        return dumped

    guarded = await run_once(
        ctx.session,
        ctx.identity.tenant_id,
        args.idempotency_key,
        spec.name,
        args.model_dump(mode="json"),
        effect,
    )
    output = spec.output_model.model_validate(guarded.response)
    result = _success_result(output)
    if guarded.response.get(NOTHING_DONE_FIELD) is True and not guarded.replayed:
        # The tool found the thing already done and changed nothing, so its
        # own event would be a false record.
        return result
    event_type = REPLAYED_EVENT_TYPE if guarded.replayed else spec.event_type
    await _append(ctx.session, ctx.identity, spec, event_type, scope, None, None, ctx.now)
    return result


async def _refuse(
    settings: Settings,
    identity: Identity,
    spec: ToolSpec[Any, Any],
    stage: _Stage,
    code: ErrorCode,
    scope: str | None,
    exc: BaseException,
    now: datetime,
) -> types.CallToolResult:
    """Record the failure, then answer with the code alone."""
    await _record_failure(
        settings, identity, spec, _event_type_for(stage, code), code, scope, exc, now
    )
    return error_result(code)


def _event_type_for(stage: _Stage, code: ErrorCode) -> str:
    """Which of the four event types this call earned.

    `FORBIDDEN` is `tool.denied` even from inside a tool body, so a
    permission decision is never filed as an outage.
    """
    if stage is _Stage.KILL_SWITCH and code is ErrorCode.TOOL_DISABLED:
        return DISABLED_EVENT_TYPE
    if code is ErrorCode.FORBIDDEN and stage in {_Stage.RBAC, _Stage.BODY}:
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
    now: datetime,
) -> None:
    """Write the failure event from a fresh session, since the tool's own
    transaction has rolled back. A failed write is logged, not raised."""
    try:
        factory = _session_factory(settings)
        async with tenant_session(factory, identity.tenant_id) as session:
            await _append(session, identity, spec, event_type, scope, code, exc, now)
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
    now: datetime,
) -> None:
    """One audit event for one call. `detail` keeps the exception's words,
    since the response carries only the code."""
    payload: dict[str, Any] = {"tool": spec.name, "scope": scope}
    if code is not None:
        payload["code"] = code.value
    if exc is not None:
        payload["detail"] = f"{type(exc).__name__}: {exc}"
    await append_event(
        session,
        identity.tenant_id,
        AuditEventInput(
            occurred_at=now,
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
    """The session factory for this process and event loop. A named function
    so a test can prove a refused call opens no session."""
    return create_session_factory(shared_engine(settings.database_url))


async def close_shared_connections() -> None:
    """Close the running loop's pooled connections. For tests that run each
    call in its own event loop."""
    await close_loop_clients()
    await dispose_shared_engines()


__all__ = ["close_shared_connections", "dispatch", "error_result"]
