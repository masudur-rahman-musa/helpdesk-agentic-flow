"""POST /chat, POST /chat/stream, GET /chat/messages and GET /chat/chats: the agent's front door.

`/chat` and `/chat/stream` are two views of one turn: the same checks, graph
and stored rows, sent once as JSON and once as SSE. Tenant and user
come from the verified token; the body carries the message and, to continue a chat, its id.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

from helpdesk.agent_api.core.deps import request_bearer
from helpdesk.agent_api.schemas.chat import (
    ChatHistoryResponse,
    ChatListResponse,
    ChatRequest,
    ChatResponse,
)
from helpdesk.agent_api.services import chat as chat_service
from helpdesk.agent_api.services import tool_proxy
from helpdesk.agent_api.services.mcp_client import ToolCaller
from helpdesk.shared.core.config import load_settings

router = APIRouter()

build_tool_caller = tool_proxy.build_tool_caller
"""Kept as a module name so tests can swap in a stub that opens no socket."""


def _tools_for(token: str) -> ToolCaller:
    """Looked up on this module at call time, so a test's stub replaces it."""
    return build_tool_caller(load_settings(), token)


@router.post("/chat", response_model=ChatResponse)
async def chat(request: Request, body: ChatRequest) -> ChatResponse:
    """One message in, one stored turn out."""
    request_bearer(request)
    return await chat_service.chat_turn(
        request, body.message, datetime.now(UTC), _tools_for, body.chat_id
    )


@router.post("/chat/stream")
async def chat_stream(request: Request, body: ChatRequest) -> Response:
    """The same turn as `/chat`, streamed as server-sent events.

    Checks run before the stream opens, so a refusal is a real HTTP status.
    POST, not GET, because a browser `EventSource` cannot send the token header
    and a query-string token would land in access logs.
    """
    request_bearer(request)
    frames = await chat_service.chat_stream_response(
        request, body.message, datetime.now(UTC), _tools_for, body.chat_id
    )
    return StreamingResponse(frames, media_type="text/event-stream")


@router.get("/chat/messages", response_model=ChatHistoryResponse)
async def chat_messages(request: Request, chat_id: str | None = None) -> ChatHistoryResponse:
    """One chat's messages, oldest first. `chat_id` is required.

    Not rate limited, so a client that used up its window can still read its
    own transcript.
    """
    return await chat_service.chat_history(request, chat_id)


@router.get("/chat/chats", response_model=ChatListResponse)
async def chat_chats(request: Request) -> ChatListResponse:
    """This user's chats, newest first. Not rate limited, like the history."""
    return await chat_service.chat_list(request)
