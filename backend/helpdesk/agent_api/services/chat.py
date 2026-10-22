"""The chat turn logic the routes call: identity, rate limit, run, render, stream.

Services raise typed errors; one app-level handler turns them into responses.
"""

import dataclasses
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import datetime

from fastapi import Request

from helpdesk.agent_api.agent.events import ReplyEvent
from helpdesk.agent_api.agent.graph import LoopGraph
from helpdesk.agent_api.agent.loop import run_loop_turn, stream_loop_turn
from helpdesk.agent_api.agent.sources import shown_citations
from helpdesk.agent_api.agent.state import TurnOutcome, TurnState
from helpdesk.agent_api.agent.turn_deps import TurnDeps
from helpdesk.agent_api.api.sse import DONE, ERROR, encode_event, encode_turn_event
from helpdesk.agent_api.core.deps import request_claims
from helpdesk.agent_api.schemas.chat import (
    ChatHistoryResponse,
    ChatListItem,
    ChatListResponse,
    ChatMessage,
    ChatResponse,
    _Identity,
)
from helpdesk.agent_api.services.decision_card import decision_card
from helpdesk.agent_api.services.mcp_client import ToolCaller
from helpdesk.agent_api.services.ratelimit import check_rate_limit
from helpdesk.shared.auth.jwt import TokenClaims, issue_token
from helpdesk.shared.core.config import load_settings
from helpdesk.shared.core.db import tenant_session
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.crud.messages import list_recent_messages
from helpdesk.shared.crud.sessions import get_owned_chat, list_user_chats
from helpdesk.shared.crud.tenants import find_tenant_by_id
from helpdesk.shared.models.conversation import check_message_content


def _loop_graph(request: Request) -> LoopGraph | None:
    """The graph app start compiled, with its saver when steps are saved. An
    app that never ran its start has none, and the turn saves nothing."""
    graph: LoopGraph | None = getattr(request.app.state, "loop_graph", None)
    return graph


def _typed_error(code: ErrorCode) -> ToolError:
    """A ToolError carrying `code`; the constructor takes a message, not a code."""
    exc = ToolError()
    exc.code = code
    return exc


ToolsFactory = Callable[[str], ToolCaller]
"""Builds the tool caller from the bearer token a turn hands to the tool server."""


def _chat_uuid(value: str) -> uuid.UUID:
    """A chat id as sent, or `VALIDATION`. Only the plain hyphenated form passes:
    `uuid.UUID` alone would also take braces, `urn:` and a missing hyphen."""
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise _typed_error(ErrorCode.VALIDATION) from None
    if str(parsed) != value.lower():
        raise _typed_error(ErrorCode.VALIDATION)
    return parsed


def _identity(claims: TokenClaims, chat_id: uuid.UUID) -> _Identity:
    """The claims as ids, with the chat the turn belongs to. A bad claim is
    `VALIDATION`, not 401: the token was valid."""
    return _Identity(
        tenant_id=uuid.UUID(claims.tenant_id),
        user_id=uuid.UUID(claims.sub),
        session_id=chat_id,
    )


def _tool_token(claims: TokenClaims, chat_id: uuid.UUID, now: datetime) -> str:
    """The token the tool server sees: the caller's own claims, `sid` set to the chat.

    The tool server stamps `sid` on audit rows and filed requests, so it must
    be the chat and not the login.
    """
    return issue_token(
        {
            "sub": claims.sub,
            "tenant_id": claims.tenant_id,
            "department_id": claims.department_id,
            "principals": claims.principals,
            "sid": str(chat_id),
        },
        load_settings().jwt_secret,
        now,
    )


async def _chat_for_turn(request: Request, claims: TokenClaims, chat_id: str | None) -> uuid.UUID:
    """The chat this message goes to: a new id, or an existing chat of this user.

    Anything not this user's in this tenant is `NOT_FOUND`, so a probe learns nothing.
    """
    if chat_id is None:
        return uuid.uuid4()
    wanted = _chat_uuid(chat_id)
    try:
        tenant_id = uuid.UUID(claims.tenant_id)
        user_id = uuid.UUID(claims.sub)
    except ValueError:
        raise _typed_error(ErrorCode.VALIDATION) from None
    async with tenant_session(request.app.state.session_factory, tenant_id) as db:
        if await get_owned_chat(db, chat_id=wanted, user_id=user_id) is None:
            raise _typed_error(ErrorCode.NOT_FOUND)
    return wanted


def _rendered(state: TurnState) -> ChatResponse:
    """The turn as a reply (a pause is a 200), or a raised ToolError if it failed."""
    if state.outcome is TurnOutcome.FAILED:
        raise _typed_error(state.error_code or ErrorCode.INTERNAL)
    card = decision_card(state)
    return ChatResponse(
        chat_id=str(state.session_id),
        reply=state.reply,
        outcome=state.outcome.value,
        citations=[citation.model_dump(mode="json") for citation in shown_citations(state)],
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
    """The response body: one turn's frames, with failures as `error` events.

    The session opens here because a streaming body outlives its handler.
    """
    try:
        async with tenant_session(request.app.state.session_factory, identity.tenant_id) as db:
            tenant = await find_tenant_by_id(db, identity.tenant_id)
            if tenant is None:
                # A valid token for a tenant that no longer exists.
                yield encode_event(ERROR, {"code": ErrorCode.INTERNAL.value})
                yield encode_event(DONE, {})
                return
            async for event in stream_loop_turn(
                deps=TurnDeps(
                    db=db,
                    model=request.app.state.model,
                    tools=tools,
                    now=lambda: now,
                    graph=_loop_graph(request),
                ),
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                session_id=identity.session_id,
                tenant_slug=tenant.slug,
                message=message,
            ):
                if isinstance(event, ReplyEvent):
                    event = dataclasses.replace(
                        event,
                        reply_data={**event.reply_data, "chat_id": str(identity.session_id)},
                    )
                for frame in encode_turn_event(event):
                    yield frame
    except ToolError as exc:
        yield encode_event(ERROR, {"code": exc.code.value})
        yield encode_event(DONE, {})


async def chat_turn(
    request: Request,
    message: str,
    now: datetime,
    build_tools: ToolsFactory,
    chat_id: str | None = None,
) -> ChatResponse:
    """One message in, one reply out, or a typed error."""
    claims = request_claims(request)
    chat = await _chat_for_turn(request, claims, chat_id)
    try:
        identity = _identity(claims, chat)
    except ValueError:
        raise _typed_error(ErrorCode.VALIDATION) from None

    await check_rate_limit(
        request.app.state.redis,
        tenant_id=identity.tenant_id,
        session_id=claims.sid,
        now=now,
    )

    tools = build_tools(_tool_token(claims, chat, now))
    async with tenant_session(request.app.state.session_factory, identity.tenant_id) as db:
        tenant = await find_tenant_by_id(db, identity.tenant_id)
        if tenant is None:
            # A verified token for a tenant that no longer exists.
            raise ToolError(ErrorCode.INTERNAL)
        state = await run_loop_turn(
            deps=TurnDeps(
                db=db,
                model=request.app.state.model,
                tools=tools,
                now=lambda: now,
                graph=_loop_graph(request),
            ),
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            session_id=identity.session_id,
            tenant_slug=tenant.slug,
            message=message,
        )
    return _rendered(state)  # raises ToolError if FAILED, else returns ChatResponse


async def chat_stream_response(
    request: Request,
    message: str,
    now: datetime,
    build_tools: ToolsFactory,
    chat_id: str | None = None,
) -> AsyncIterator[str]:
    """Run every check first, so a refusal is a real status, then return the frames."""
    claims = request_claims(request)
    chat = await _chat_for_turn(request, claims, chat_id)
    try:
        identity = _identity(claims, chat)
    except ValueError:
        raise _typed_error(ErrorCode.VALIDATION) from None

    check_message_content(message)

    await check_rate_limit(
        request.app.state.redis,
        tenant_id=identity.tenant_id,
        session_id=claims.sid,
        now=now,
    )

    tools = build_tools(_tool_token(claims, chat, now))
    return _stream_frames(request, identity=identity, tools=tools, message=message, now=now)


async def chat_history(request: Request, chat_id: str | None) -> ChatHistoryResponse:
    """One chat's messages, oldest first. The chat must be this user's."""
    claims = request_claims(request)
    if chat_id is None:
        raise _typed_error(ErrorCode.VALIDATION)
    wanted = _chat_uuid(chat_id)
    try:
        identity = _identity(claims, wanted)
    except ValueError:
        raise _typed_error(ErrorCode.VALIDATION) from None

    async with tenant_session(request.app.state.session_factory, identity.tenant_id) as db:
        if await get_owned_chat(db, chat_id=wanted, user_id=identity.user_id) is None:
            raise _typed_error(ErrorCode.NOT_FOUND)
        rows = await list_recent_messages(db, wanted)
    return ChatHistoryResponse(
        messages=[ChatMessage(role=row.role, content=row.content) for row in rows]
    )


async def chat_list(request: Request) -> ChatListResponse:
    """This user's chats, newest first. Same list under every login."""
    claims = request_claims(request)
    try:
        identity = _identity(claims, uuid.uuid4())
    except ValueError:
        raise _typed_error(ErrorCode.VALIDATION) from None

    async with tenant_session(request.app.state.session_factory, identity.tenant_id) as db:
        chats = await list_user_chats(db, user_id=identity.user_id)
    return ChatListResponse(
        chats=[
            ChatListItem(id=str(chat.id), title=chat.title, last_seen_at=chat.last_seen_at)
            for chat in chats
        ]
    )
