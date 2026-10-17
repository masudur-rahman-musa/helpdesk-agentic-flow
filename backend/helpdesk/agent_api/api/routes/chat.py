"""POST /chat, POST /chat/stream and GET /chat/messages: the agent's front door.

`/chat` and `/chat/stream` are two views of one turn: the same checks, graph
and stored rows, sent once as JSON and once as SSE. Tenant, user and session
all come from the verified token; the body carries only the message.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

from helpdesk.agent_api.core.deps import request_bearer
from helpdesk.agent_api.schemas.chat import ChatHistoryResponse, ChatRequest, ChatResponse
from helpdesk.agent_api.services import chat as chat_service
from helpdesk.agent_api.services import tool_proxy
from helpdesk.shared.core.config import load_settings

router = APIRouter()

build_tool_caller = tool_proxy.build_tool_caller
"""Kept as a module name so tests can swap in a stub that opens no socket."""


@router.post("/chat", response_model=ChatResponse)
async def chat(request: Request, body: ChatRequest) -> ChatResponse:
    """One message in, one stored turn out."""
    token = request_bearer(request)
    now = datetime.now(UTC)
    tools = build_tool_caller(load_settings(), token)
    return await chat_service.chat_turn(request, body.message, now, tools)


@router.post("/chat/stream")
async def chat_stream(request: Request, body: ChatRequest) -> Response:
    """The same turn as `/chat`, streamed as server-sent events.

    Checks run before the stream opens, so a refusal is a real HTTP status.
    POST, not GET, because a browser `EventSource` cannot send the token header
    and a query-string token would land in access logs.
    """
    token = request_bearer(request)
    now = datetime.now(UTC)
    tools = build_tool_caller(load_settings(), token)
    frames = await chat_service.chat_stream_response(request, body.message, now, tools)
    return StreamingResponse(frames, media_type="text/event-stream")


@router.get("/chat/messages", response_model=ChatHistoryResponse)
async def chat_messages(request: Request) -> ChatHistoryResponse:
    """This session's messages, oldest first.

    Not rate limited, so a client that used up its window can still read its
    own transcript.
    """
    return await chat_service.chat_history(request)
