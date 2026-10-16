"""The chat turn logic the routes call: identity, render, stream.

Pulled out of `chat_routes` by an earlier change so `api/routes/chat.py` holds only the
HTTP wiring and the `build_tool_caller` seam its tests patch. The endpoints
import `_identity`, `_rendered` and `_stream_frames` from here; a typed refusal
is `deps.typed_refusal`, the one shared map since an earlier change, so this
module no longer keeps a second status map of its own.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import datetime

from fastapi import Request
from fastapi.responses import JSONResponse

from helpdesk.agent_api.agent.graph import stream_turn
from helpdesk.agent_api.agent.state import TurnOutcome, TurnState
from helpdesk.agent_api.agent.turn_deps import TurnDeps
from helpdesk.agent_api.api.sse import DONE, ERROR, encode_event
from helpdesk.agent_api.core.deps import typed_refusal
from helpdesk.agent_api.schemas.chat import ChatResponse, _Identity
from helpdesk.agent_api.services.decision_card import decision_card
from helpdesk.agent_api.services.mcp_client import ToolCaller
from helpdesk.shared.auth.jwt import TokenClaims
from helpdesk.shared.core.db import tenant_session
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.crud.tenants import find_tenant_by_id


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


def _rendered(state: TurnState) -> JSONResponse:
    """The finished (or paused) turn as a response, or its failure as a code.

    A paused turn is a 200 with a body, not a 202 (decision E1): the turn
    produced a reply and stored it, so the pause is state the client renders,
    not a deferral of the request that was just made.

    A FAILED turn answers its typed code and no body at all, so it carries no
    decision card either — pinned, with the card's two other shapes, by
    `test_chat_route.py::test_a_filed_request_answers_its_decision_card` and
    `::test_a_turn_with_no_access_tool_answers_a_null_decision`.
    """
    if state.outcome is TurnOutcome.FAILED:
        return typed_refusal(state.error_code or ErrorCode.INTERNAL)
    card = decision_card(state)
    body = ChatResponse(
        reply=state.reply,
        outcome=state.outcome.value,
        citations=[citation.model_dump(mode="json") for citation in state.citations],
        request_id=None if state.awaiting_request_id is None else str(state.awaiting_request_id),
        decision=None if card is None else card.model_dump(mode="json"),
    )
    return JSONResponse(content=body.model_dump())


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
