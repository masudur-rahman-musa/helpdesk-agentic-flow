"""The chat turn logic the routes call: identity, render, stream.

Pulled out of `chat_routes` by an earlier change so `api/routes/chat.py` holds only the
HTTP wiring and the `build_tool_caller` seam its tests patch.

An earlier change moves all DB and rate-limit logic here so the route holds nothing but
wiring. `_rendered` now raises ToolError for a FAILED turn instead of returning
a JSONResponse — the app-level handler in `router.py` is the one edge that
renders it. `chat_turn`, `chat_stream_response` and `chat_history` are the new
service functions the route delegates to.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from fastapi import Request

from helpdesk.agent_api.agent.graph import run_turn, stream_turn
from helpdesk.agent_api.agent.state import TurnOutcome, TurnState
from helpdesk.agent_api.agent.turn_deps import TurnDeps
from helpdesk.agent_api.api.sse import DONE, ERROR, encode_event
from helpdesk.agent_api.core.deps import request_bearer, request_claims
from helpdesk.agent_api.schemas.chat import ChatHistoryResponse, ChatMessage, ChatResponse, _Identity
from helpdesk.agent_api.services.decision_card import decision_card
from helpdesk.agent_api.services.mcp_client import ToolCaller
from helpdesk.agent_api.services.ratelimit import RateLimitExceededError, check_rate_limit
from helpdesk.shared.auth.jwt import TokenClaims
from helpdesk.shared.core.db import tenant_session
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.crud.messages import list_recent_messages
from helpdesk.shared.crud.tenants import find_tenant_by_id
from helpdesk.shared.models.conversation import check_message_content


def _typed_error(code: ErrorCode) -> ToolError:
    """Return a ToolError whose .code carries the given code.

    `ToolError.code` is a class attribute defaulting to INTERNAL. Passing a
    code to the constructor sets `args[0]` on the Exception, NOT `.code`, so
    `ToolError(ErrorCode.FORBIDDEN).code` is still INTERNAL. This helper
    overrides the instance attribute so the app-level handler in `router.py`
    reads the right code. Named to make the override intent visible.
    """
    exc = ToolError()
    exc.code = code
    return exc


def _identity(claims: TokenClaims) -> _Identity:
    """The claims as ids, or `ValueError` for a claim that is not a uuid.

    A separate step because the failure is a typed `VALIDATION` rather than a
    401: the token verified, so the caller proved who they are; what is
    malformed is a claim inside it.
    """
    return _Identity(
        tenant_id=uuid.UUID(claims.tenant_id),
        user_id=uuid.UUID(claims.sub),
        session_id=uuid.UUID(claims.sid),
    )


def _rendered(state: TurnState) -> ChatResponse:
    """The finished (or paused) turn as a result, or a ToolError for a failure.

    A paused turn is a 200 with a body, not a 202 (decision E1): the turn
    produced a reply and stored it, so the pause is state the client renders,
    not a deferral of the request that was just made.

    A FAILED turn raises ToolError carrying the turn's error code (an earlier change,
    a design note): services raise, the app-level handler in `router.py` renders.
    No decision card on a failed turn — pinned, with the card's two other
    shapes, by `test_chat_route.py::test_a_filed_request_answers_its_decision_card`
    and `::test_a_turn_with_no_access_tool_answers_a_null_decision`.
    Pinned as a raise (not a return) by
    `test_error_map.py::test_chat_is_no_longer_its_own_map`.
    """
    if state.outcome is TurnOutcome.FAILED:
        raise _typed_error(state.error_code or ErrorCode.INTERNAL)
    card = decision_card(state)
    return ChatResponse(
        reply=state.reply,
        outcome=state.outcome.value,
        citations=[citation.model_dump(mode="json") for citation in state.citations],
        request_id=None if state.awaiting_request_id is None else str(state.awaiting_request_id),
        decision=None if card is None else card.model_dump(mode="json"),
    )


async def _stream_frames(
    request: Request,
    *,
    identity: _Identity,
    tools: ToolCaller,
    message: str,
    now: datetime,
) -> AsyncIterator[str]:
    """The response BODY: one turn's frames, and a typed event for a failure.

    The tenant-bound session is opened here rather than in the handler because
    a `StreamingResponse`'s body outlives the handler that returned it: a
    session opened around the handler would be closed before the first node
    ran. The `async with` therefore wraps the whole generator, so the
    connection goes back to the pool when the last frame is written — on the
    failing path as well as the finishing one, which is what
    `test_chat_stream.py::test_an_unreachable_mcp_server_mid_stream_is_a_typed_error_event`
    reads `engine.pool.checkedout()` after the response to prove.

    Every failure here arrives as an `error` event and never as a status: the
    status line was already sent as 200 when the first byte left (decision E1).
    `stream_turn` types the failures it can see itself; this `except` is the
    backstop for the ones it cannot — anything raised before the generator is
    entered, including the tenant read below.
    """
    try:
        async with tenant_session(request.app.state.session_factory, identity.tenant_id) as db:
            tenant = await find_tenant_by_id(db, identity.tenant_id)
            if tenant is None:
                # A verified token for a tenant that no longer exists, exactly
                # as `/chat` answers it — but after the 200, so it is typed as
                # an event rather than as a status.
                yield encode_event(ERROR, {"code": ErrorCode.INTERNAL.value})
                yield encode_event(DONE, {})
                return
            async for frame in stream_turn(
                deps=TurnDeps(db=db, model=request.app.state.model, tools=tools, now=lambda: now),
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                session_id=identity.session_id,
                tenant_slug=tenant.slug,
                message=message,
                checkpointer=request.app.state.checkpointer,
            ):
                yield frame
    except ToolError as exc:
        yield encode_event(ERROR, {"code": exc.code.value})
        yield encode_event(DONE, {})


async def chat_turn(
    request: Request,
    message: str,
    now: datetime,
    tools: ToolCaller,
) -> ChatResponse:
    """One message in, one ChatResponse out; or a ToolError (or RateLimitExceededError).

    The route builds the caller and passes it in so the `build_tool_caller` seam
    stays on `api/routes/chat.py` and the route's tests can patch it there without
    opening a socket.

    The rate-limit error propagates as `RateLimitExceededError` (a ToolError
    subclass) to the app-level handler that renders the literal 429 (an earlier change,
    a design note). All other ToolErrors go through the STATUS_FOR_CODE handler.
    Pinned by `test_api_layering.py::test_the_rate_limit_is_rendered_by_the_one_edge`
    and `::test_a_failed_turn_answers_its_typed_code_and_no_reply`.
    """
    claims = request_claims(request)
    try:
        identity = _identity(claims)
    except ValueError:
        raise _typed_error(ErrorCode.VALIDATION)

    await check_rate_limit(
        request.app.state.redis,
        tenant_id=identity.tenant_id,
        session_id=claims.sid,
        now=now,
    )

    async with tenant_session(request.app.state.session_factory, identity.tenant_id) as db:
        tenant = await find_tenant_by_id(db, identity.tenant_id)
        if tenant is None:
            # A verified token for a tenant that no longer exists.
            raise ToolError(ErrorCode.INTERNAL)
        state = await run_turn(
            deps=TurnDeps(db=db, model=request.app.state.model, tools=tools, now=lambda: now),
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            session_id=identity.session_id,
            tenant_slug=tenant.slug,
            message=message,
            checkpointer=request.app.state.checkpointer,
        )
    return _rendered(state)  # raises ToolError if FAILED, else returns ChatResponse


async def chat_stream_response(
    request: Request,
    message: str,
    now: datetime,
    tools: ToolCaller,
) -> AsyncIterator[str]:
    """Pre-checks, then the SSE frame generator; raises before any bytes are sent.

    Everything that can refuse fires here, before the route wraps the result in
    a StreamingResponse. If a check raises, FastAPI catches it as an HTTP
    exception BEFORE any byte is sent, so the client gets a proper status rather
    than a stream that opens 200 then immediately errors.

    The actual streaming logic is in `_stream_frames`; this function is
    deliberately NOT a generator so the pre-checks run eagerly.
    """
    claims = request_claims(request)
    try:
        identity = _identity(claims)
    except ValueError:
        raise _typed_error(ErrorCode.VALIDATION)

    check_message_content(message)

    await check_rate_limit(
        request.app.state.redis,
        tenant_id=identity.tenant_id,
        session_id=claims.sid,
        now=now,
    )

    return _stream_frames(request, identity=identity, tools=tools, message=message, now=now)


async def chat_history(request: Request) -> ChatHistoryResponse:
    """This session's messages, oldest first; raises ToolError on any failure.

    Moved from `api/routes/chat.py` by an earlier change so the route holds no DB code.
    Pinned by `test_api_layering.py::test_the_chat_and_audit_routes_hold_no_db_or_login_logic`.
    """
    claims = request_claims(request)
    try:
        identity = _identity(claims)
    except ValueError:
        raise _typed_error(ErrorCode.VALIDATION)

    async with tenant_session(request.app.state.session_factory, identity.tenant_id) as db:
        rows = await list_recent_messages(db, identity.session_id)
    return ChatHistoryResponse(
        messages=[ChatMessage(role=row.role, content=row.content) for row in rows]
    )
