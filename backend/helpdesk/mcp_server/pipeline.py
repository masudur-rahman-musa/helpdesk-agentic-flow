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

Exactly one audit event per call FROM THE PIPELINE (the design). A tool body
may append its own domain events inside the same transaction — an earlier change's
`create_access_request` appends `request.auto_executed`, `grant.executed`
and `request.queued` beside the pipeline's `request.created`, because SPEC
§13.1 names all four as distinct recorded facts. They
commit or roll back with the tool's own writes; what this module guarantees
is that IT writes exactly one, pinned by the three exact event-sequence
assertions in `test_mcp_create_access_request.py`.

The success event is written
inside the tool's own transaction, so a tool that fails after writing takes
its event with it; the failure event is then written from a FRESH session,
because an event that rolls back with the failure it records is not a record. A call whose token never verified writes nothing at
all: an unverifiable token names no tenant, and the audit log is partitioned
by tenant, so there is no chain to append to (decision E). The cost, stated
so it is a decision and not an oversight, is that a flood of invalid tokens
leaves no trace in the audit log.
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

from helpdesk.mcp_server.identity import Identity, identity_from_header
from helpdesk.mcp_server.redis_client import (
    REDIS_TIMEOUT_SECONDS as REDIS_TIMEOUT_SECONDS,  # re-exported: the old call site
)
from helpdesk.mcp_server.redis_client import (
    _redis as _redis,  # re-exported: `test_mcp_pipeline.py` reads both from here
)
from helpdesk.mcp_server.redis_client import (
    close_loop_clients,
)
from helpdesk.mcp_server.registry import (
    DENIED_EVENT_TYPE,
    DISABLED_EVENT_TYPE,
    FAILED_EVENT_TYPE,
    REPLAYED_EVENT_TYPE,
    ToolSpec,
)
from helpdesk.mcp_server.tools.base import MutatingToolInput
from helpdesk.shared.audit import AuditEventInput, append_event
from helpdesk.shared.auth import rbac
from helpdesk.shared.core.db import (
    create_session_factory,
    dispose_shared_engines,
    shared_engine,
    tenant_session,
)
from helpdesk.shared.core.settings import Settings, load_settings
from helpdesk.shared.errors import ErrorCode, ToolError
from helpdesk.shared.idempotency.guard import run_once
from helpdesk.shared.killswitch import KillSwitch
from helpdesk.shared.obs import metrics
from helpdesk.shared.obs.context import bind_context

logger = logging.getLogger(__name__)

_OK_STATUS = "ok"
"""The `status` label of a call that returned an output.

Not an `ErrorCode` value, and deliberately not one: the label's domain is the
eight typed codes plus this word, so `status="ok"` is the only value that is
not a refusal and a dashboard can sum the rest as the refusal rate. Pinned by
`test_mcp_metrics.py::test_a_successful_tool_call_counts_once_and_observes_once`.
"""

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
    # Every line from here on names the tenant and session of THIS call, taken
    # from the identity the header already produced and from nothing else
    #. It wraps the refusal paths too: a denied or failed call is
    # the one an operator most needs to attribute. Only the design's
    # telemetry-safe fields are bound — the user id stays out of the context
    # even though the identity carries it, because the context is copied onto
    # every line.
    with bind_context(
        tenant_id=str(identity.tenant_id),
        session_id=str(identity.session_id),
        principal_kind=_principal_kind(identity),
    ):
        # the design's tool metrics, recorded in the `finally` below so the count
        # and the latency observation are made once on the success path and
        # once on every refusal path alike -- the stages have four exits and a
        # site per exit would drift. `status` stays None until an outcome is
        # known, which is what keeps a CANCELLED call (a `BaseException` in
        # 3.12, so it passes through untouched) out of the numbers: it neither
        # succeeded nor failed, exactly as it writes no audit event.
        status: str | None = None
        started = time.perf_counter()
        try:
            await _check_switch(settings, identity, spec)

            stage = _Stage.RBAC
            scope = rbac.authorize(identity.claims, spec.tool).scope.value

            stage = _Stage.VALIDATION
            args = spec.input_model.model_validate(dict(arguments))

            stage = _Stage.BODY
            result = await _call_tool(spec, settings, identity, args, scope)
            status = _OK_STATUS
        except ToolError as exc:
            status = exc.code.value
            return await _refuse(settings, identity, spec, stage, exc.code, scope, exc)
        except ValidationError as exc:
            # Only the argument parse earns VALIDATION. A tool body that parses
            # something of its own (a provider reply, a stored payload) and finds
            # it malformed has an upstream problem, not a caller with bad
            # arguments; calling that VALIDATION would tell the user their input
            # was wrong and record the same lie in the audit event.
            code = ErrorCode.VALIDATION if stage is _Stage.VALIDATION else ErrorCode.INTERNAL
            status = code.value
            return await _refuse(settings, identity, spec, stage, code, scope, exc)
        except Exception as exc:
            # asyncio.CancelledError is a BaseException in 3.12, so it passes
            # through here untouched; that is the behaviour, not an oversight.
            status = ErrorCode.INTERNAL.value
            return await _refuse(settings, identity, spec, stage, ErrorCode.INTERNAL, scope, exc)
        finally:
            if status is not None:
                metrics.record_tool_call(
                    tool=spec.name,
                    status=status,
                    duration_seconds=time.perf_counter() - started,
                )
                if status == ErrorCode.SOD_CONFLICT.value:
                    # Counted from the answered code, not from where the
                    # conflict was classified: the policy package is pure, and
                    # the transaction that raised may still roll back
                    #.
                    metrics.record_sod_block()
        return result


def _principal_kind(identity: Identity) -> str | None:
    """The caller's principals as one telemetry-safe field.

    All of them, sorted and comma-joined: a caller can hold several, so a line
    attributed to one of them would misdescribe the call, and sorting makes
    the same caller read the same on every line. A principal names a role,
    never a person.
    """
    if not identity.claims.principals:
        return None
    return ",".join(sorted(identity.claims.principals))


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
        if spec.mutating:
            return await _run_mutating_tool(spec, session, identity, args, scope)
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


async def _run_mutating_tool(
    spec: ToolSpec[Any, Any],
    session: AsyncSession,
    identity: Identity,
    args: BaseModel,
    scope: str | None,
) -> types.CallToolResult:
    """The tool body inside `run_once`, so a retry can never repeat the effect.

    Reached only after the kill switch and RBAC by construction (this runs
    inside `_call_tool`, after both stages), so a stopped or refused call
    burns no idempotency key — pinned by
    `test_mcp_helpdesk_tools.py::test_a_disabled_mutating_call_burns_no_idempotency_key`.

    Both the fresh and the replayed path build the response from the stored
    mapping through `output_model`, so a replay is byte-identical to the
    first answer by construction, not by comparison. A replay writes
    `tool.replayed`, never the declared event again.
    """
    if not isinstance(args, MutatingToolInput):
        # A spec marked mutating whose input model carries no key is a wiring
        # bug; caught here as INTERNAL, never as the caller's mistake.
        raise TypeError(f"mutating tool {spec.name} must take a MutatingToolInput")

    async def effect() -> Mapping[str, Any]:
        output: BaseModel = await spec.handler(session, identity, args)
        dumped: dict[str, Any] = output.model_dump(mode="json")
        return dumped

    guarded = await run_once(
        session,
        identity.tenant_id,
        args.idempotency_key,
        spec.name,
        args.model_dump(mode="json"),
        effect,
    )
    output = spec.output_model.model_validate(guarded.response)
    result = _success_result(output)
    event_type = REPLAYED_EVENT_TYPE if guarded.replayed else spec.event_type
    await _append(session, identity, spec, event_type, scope, None, None)
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

    Only a stop at the kill switch is `tool.disabled`; everything else that
    is not a permission decision, including a Redis outage while reading the
    switch, is `tool.failed`.

    A `FORBIDDEN` is `tool.denied` wherever it was raised, at the RBAC stage
    or inside the tool body. An earlier change's on-behalf check is the second kind:
    `create_access_request` re-authorizes against
    `CREATE_ACCESS_REQUEST_ON_BEHALF` once it can see whose access is being
    asked for, which is after validation by construction (RBAC runs before
    validation so a caller who may not call a tool learns nothing about its
    argument shape, decision B). Recording that as `tool.failed` would file a
    permission decision as an outage. Only `FORBIDDEN` gets this treatment:
    every other code raised in the body stays `tool.failed`. Pinned by
    `test_mcp_create_access_request.py::test_a_manager_naming_a_target_in_another_department_is_denied`.
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


async def close_shared_connections() -> None:
    """Close the running loop's pooled connections, Postgres and Redis alike.

    A server calls this never (its loop outlives the process's work); a test
    driving each call through its own `asyncio.run` calls it at the end of
    every scenario, because a pool belonging to a dead loop is a connection
    nothing will ever close.
    """
    await close_loop_clients()
    await dispose_shared_engines()


__all__ = ["close_shared_connections", "dispatch", "error_result"]
