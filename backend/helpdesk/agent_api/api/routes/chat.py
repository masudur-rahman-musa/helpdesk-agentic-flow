"""POST /chat, POST /chat/stream and GET /chat/messages: the agent's front door.

The door and nothing else. All logic — identity, rate
limit, DB access, turn execution — lives in `agent_api.services.chat`.  This
file builds the tool caller via the `build_tool_caller` seam (kept here so
route tests can patch it without opening a socket) and delegates everything
else.

`/chat` and `/chat/stream` are two VIEWS of one turn, never two turns
(19c decision G1): the same verification, the same limit, the same graph and
the same stored rows, rendered once as a JSON body and once as SSE frames.
Pinned by `test_chat_stream.py::test_both_routes_store_the_same_rows_for_one_message`.

**Identity comes from the token and from nowhere else**. The
tenant, the user AND the session are the verified claims' `tenant_id`, `sub`
and `sid`; the body carries a message and nothing that names a person or a
conversation (decision A1).
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
"""The turn's MCP caller, `agent_api.deps`' since an earlier change (decision C).

Kept as a MODULE GLOBAL here, and called through the module attribute below,
for two reasons: it is the seam `agent_api.readiness` already uses, and the
route's tests replace it with the scripted stub of 17b so a turn opens no
socket. Moving the definition without leaving this name behind would rebind
none of those `monkeypatch.setattr(chat_routes, "build_tool_caller", …)`
sites and every one of them would quietly start dialling a real MCP server.
"""


@router.post("/chat", response_model=ChatResponse)
async def chat(request: Request, body: ChatRequest) -> ChatResponse:
    """One message in, one stored turn out.

    Builds the tool caller (keeping the seam on this module) and delegates
    everything else to `chat_service.chat_turn`. Raises a ToolError (or
    RateLimitExceededError) which the app-level handler in `router.py` renders.
    """
    token = request_bearer(request)
    now = datetime.now(UTC)
    tools = build_tool_caller(load_settings(), token)
    return await chat_service.chat_turn(request, body.message, now, tools)


@router.post("/chat/stream")
async def chat_stream(request: Request, body: ChatRequest) -> Response:
    """The same message as `/chat`, watched: the design step 9 over SSE.

    A second VIEW of one turn, never a second turn (decision G1). Pre-checks
    run inside `chat_service.chat_stream_response` BEFORE this route wraps the
    result in a `StreamingResponse`, so any refusal (rate limit, message cap)
    becomes a proper HTTP status rather than a 200 that opens and immediately
    errors. Pinned by `test_chat_stream.py`'s refusal cases.

    `POST` and not `GET` (decision D1): a browser `EventSource` cannot send an
    `Authorization` header, so a `GET` stream would carry the bearer in a query
    string and therefore into access logs.
    """
    token = request_bearer(request)
    now = datetime.now(UTC)
    tools = build_tool_caller(load_settings(), token)
    frames = await chat_service.chat_stream_response(request, body.message, now, tools)
    return StreamingResponse(frames, media_type="text/event-stream")


@router.get("/chat/messages", response_model=ChatHistoryResponse)
async def chat_messages(request: Request) -> ChatHistoryResponse:
    """This session's messages, oldest first, and this tenant's only.

    Delegates DB access to `chat_service.chat_history`. No rate limit: SPEC
    §10.3 bounds messages, and refusing a client its own transcript during a
    window it has spent would hide the very turns it is asking about.
    """
    return await chat_service.chat_history(request)
