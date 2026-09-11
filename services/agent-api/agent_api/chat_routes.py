"""POST /chat, POST /chat/stream and GET /chat/messages: the agent's front door.

The door and the counter, and nothing else (the design). Every decision a turn
makes was made by an earlier change; this module verifies who is knocking,
counts the knock against the design's limit, hands the turn a tenant-bound
session, and renders what comes back.

`/chat` and `/chat/stream` are two VIEWS of one turn, never two turns
(19c decision G1): the same verification, the same limit, the same graph and
the same stored rows, rendered once as a JSON body and once as SSE frames.
Pinned by `test_chat_stream.py::test_both_routes_store_the_same_rows_for_one_message`.

**Identity comes from the token and from nowhere else**. The
tenant, the user AND the session are the verified claims' `tenant_id`, `sub`
and `sid`; the body carries a message and nothing that names a person or a
conversation (decision A1). A client-chosen session id would be worse than a
client-chosen user id, because the session id IS the checkpoint thread (ADR
0012), so it would address another turn's checkpoints. Unknown body keys are
therefore IGNORED rather than refused: a body naming another user must be
ACCEPTED and change nothing, which is what
`test_chat_route.py::test_identity_comes_from_the_token_and_not_from_the_body`
asserts by reading the rows back.

**Every refusal is a typed body from `helpdesk.errors`** — the same eight codes
the tools answer with, so a chat client and a tool client do not have to learn
two vocabularies. That includes the failures a turn reports rather than raises:
a turn that ended `FAILED` is answered as its own error code and carries NO
reply, because a templated failure sentence rendered as an ordinary 200 is
prose about a turn nothing decided
(`::test_an_unreachable_gateway_answers_a_typed_code_and_no_partial_reply`).

The process-lifetime resources are read off `app.state` per request (decision
B), never captured at import: a test drives its own gateway and its own Redis
by replacing them after startup.
"""

import uuid
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from helpdesk.auth.jwt import TokenClaims
from helpdesk.core.db import tenant_session
from helpdesk.core.repository import find_tenant_by_id, list_recent_messages
from helpdesk.core.settings import load_settings
from helpdesk.errors import ErrorCode, ToolError
from helpdesk.models.conversation import check_message_content
from pydantic import BaseModel

from agent_api import deps
from agent_api.decision_card import decision_card
from agent_api.graph.build import run_turn, stream_turn
from agent_api.graph.deps import TurnDeps
from agent_api.graph.state import TurnOutcome, TurnState
from agent_api.mcp_client import ToolCaller
from agent_api.ratelimit import RateLimitExceededError, check_rate_limit
from agent_api.sse import DONE, ERROR, encode_event

router = APIRouter()

_TOO_MANY_REQUESTS = 429

STATUS_FOR_CODE: Mapping[ErrorCode, int] = {
    ErrorCode.VALIDATION: 400,
    ErrorCode.FORBIDDEN: 401,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.POLICY_BLOCKED: _TOO_MANY_REQUESTS,
}
"""The HTTP status each typed code this route can answer with is dressed in.

Only the codes this surface actually produces. Everything else — an
`INTERNAL` from an unreachable gateway, MCP server or Redis, and any code a
future turn learns to fail with — falls through to 500, which is the right
answer for a failure the caller cannot act on. `POLICY_BLOCKED` is 429 here
because the only thing on this route that raises it is the rate limit
(`ratelimit.RateLimitExceededError`); a tool-level `POLICY_BLOCKED` never
escapes `run_turn`, it becomes a composed reply.
"""


class ChatRequest(BaseModel):
    """One message. Deliberately NOT `extra="forbid"`.

    A body naming a user, a tenant or a session must be accepted and must
    change nothing: refusing it would answer 422 and prove nothing about where
    identity came from. Pinned by
    `test_chat_route.py::test_identity_comes_from_the_token_and_not_from_the_body`,
    which reads the stored rows rather than the status.

    The message is not length-checked here either: `run_turn` runs
    `check_message_content` before any row, any model call and any MCP
    handshake, and a second cap on this model would be a second number to
    drift from `MAX_MESSAGE_CHARS` (a project rule's "one gate, one
    constant").
    """

    message: str


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatHistoryResponse(BaseModel):
    messages: list[ChatMessage]


class ChatResponse(BaseModel):
    """What one turn answers with, paused or finished.

    `request_id` is the access request a PAUSED turn is waiting on, and None on
    every other outcome: it is what lets a client show the user what they are
    waiting for without reading a checkpoint (decision E1).

    `decision` is this turn's decision card, or null where the turn decided
    nothing about access. It rides as the card's own
    JSON rather than as the model, because the body below is dumped in python
    mode and a uuid or a datetime is not something `JSONResponse` can write.
    """

    reply: str | None
    outcome: str
    citations: list[dict[str, Any]]
    request_id: str | None
    decision: dict[str, Any] | None


build_tool_caller = deps.build_tool_caller
"""The turn's MCP caller, `agent_api.deps`' since an earlier change (decision C).

Kept as a MODULE GLOBAL here, and called through the module attribute below,
for two reasons: it is the seam `agent_api.readiness` already uses, and the
route's tests replace it with the scripted stub of 17b so a turn opens no
socket. Moving the definition without leaving this name behind would rebind
none of those `monkeypatch.setattr(chat_routes, "build_tool_caller", …)`
sites and every one of them would quietly start dialling a real MCP server.
"""

_bearer = deps.bearer
_claims = deps.claims


def _typed(code: ErrorCode) -> JSONResponse:
    """One typed body, dressed in this code's status. Never free text."""
    return JSONResponse(status_code=STATUS_FOR_CODE.get(code, 500), content={"code": code.value})


class _Identity(BaseModel):
    """The three ids one request acts under, all read off the verified token."""

    tenant_id: uuid.UUID
    user_id: uuid.UUID
    session_id: uuid.UUID


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
        return _typed(state.error_code or ErrorCode.INTERNAL)
    card = decision_card(state)
    body = ChatResponse(
        reply=state.reply,
        outcome=state.outcome.value,
        citations=[citation.model_dump(mode="json") for citation in state.citations],
        request_id=None if state.awaiting_request_id is None else str(state.awaiting_request_id),
        decision=None if card is None else card.model_dump(mode="json"),
    )
    return JSONResponse(content=body.model_dump())


@router.post("/chat")
async def chat(
    request: Request, body: ChatRequest, authorization: str | None = Header(default=None)
) -> JSONResponse:
    """One message in, one stored turn out.

    The ORDER is the whole of this function's content, and every step of it is
    asserted: the token is verified before anything else, the rate limit is
    counted before the turn is started, and the turn is what writes rows.
    A limiter checked after the turn would bill a model call and store two
    rows for a message it then refuses, which is what
    `::test_an_unreachable_redis_refuses_the_turn_and_starts_none` catches by
    staging a gateway with nothing and reading the rows back.

    `now` is stamped once and passed as the turn's clock as well as the
    window's, so the message counted and the message stored are judged against
    one reading.
    """
    settings = load_settings()
    token = _bearer(authorization)
    claims = _claims(token, settings.jwt_secret)
    if claims is None or token is None:
        return _typed(ErrorCode.FORBIDDEN)

    now = datetime.now(UTC)
    try:
        identity = _identity(claims)
    except ValueError:
        return _typed(ErrorCode.VALIDATION)

    try:
        await check_rate_limit(
            request.app.state.redis,
            tenant_id=identity.tenant_id,
            session_id=claims.sid,
            now=now,
        )
    except RateLimitExceededError:
        return _typed(ErrorCode.POLICY_BLOCKED)
    except ToolError as exc:
        return _typed(exc.code)

    tools = build_tool_caller(settings, token)
    try:
        async with tenant_session(request.app.state.session_factory, identity.tenant_id) as db:
            tenant = await find_tenant_by_id(db, identity.tenant_id)
            if tenant is None:
                # A verified token for a tenant that no longer exists: nothing
                # the caller can act on, and not a 404 either — the row they
                # asked about is their own tenant.
                return _typed(ErrorCode.INTERNAL)
            state = await run_turn(
                deps=TurnDeps(db=db, model=request.app.state.model, tools=tools, now=lambda: now),
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                session_id=identity.session_id,
                tenant_slug=tenant.slug,
                message=body.message,
                checkpointer=request.app.state.checkpointer,
            )
    except ToolError as exc:
        # The pre-checks and the MCP handshake raise OUTSIDE every node, so
        # nothing on the turn path converts them into state: without this an
        # oversized message and an unreachable MCP server both reach the client
        # as FastAPI's untyped 500 with a stack trace.
        return _typed(exc.code)
    return _rendered(state)


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


@router.post("/chat/stream")
async def chat_stream(
    request: Request, body: ChatRequest, authorization: str | None = Header(default=None)
) -> Response:
    """The same message as `/chat`, watched: the design step 9 over SSE.

    A second VIEW of one turn, never a second turn (decision G1): `/chat` stays
    beside this route for the clients that should not have to parse frames — a
    script, row 26's golden runner — and both drive the same graph, the same
    pre-checks and the same stored rows.

    `POST` and not `GET` (decision D1). A browser `EventSource` cannot send an
    `Authorization` header, so a `GET` stream would carry the bearer in a query
    string and therefore into every access log, every proxy trace and every
    referrer. The web client of row 22 reads a `fetch` body as a stream, which
    costs it nothing.

    **Everything that can refuse, refuses BEFORE the first byte.** The token,
    the message cap and the rate limit are all decided here, as the same typed
    bodies and statuses `/chat` answers with — 401 `FORBIDDEN`, 400
    `VALIDATION`, 429 `POLICY_BLOCKED` — because a refusal after a 200 is a
    refusal the client has to parse out of a stream. The cap is checked here
    as well as inside `stream_turn` for that reason alone: it is the same
    function against the same constant, and the second call cannot disagree
    with the first.

    Each refusal opens no stream at all, asserted on the content type as well
    as on the body by `test_chat_stream.py`'s three refusal cases.
    """
    settings = load_settings()
    token = _bearer(authorization)
    claims = _claims(token, settings.jwt_secret)
    if claims is None or token is None:
        return _typed(ErrorCode.FORBIDDEN)

    now = datetime.now(UTC)
    try:
        identity = _identity(claims)
    except ValueError:
        return _typed(ErrorCode.VALIDATION)

    try:
        check_message_content(body.message)
    except ToolError as exc:
        return _typed(exc.code)

    try:
        await check_rate_limit(
            request.app.state.redis,
            tenant_id=identity.tenant_id,
            session_id=claims.sid,
            now=now,
        )
    except RateLimitExceededError:
        return _typed(ErrorCode.POLICY_BLOCKED)
    except ToolError as exc:
        return _typed(exc.code)

    frames = _stream_frames(
        request,
        identity=identity,
        tools=build_tool_caller(settings, token),
        message=body.message,
        now=now,
    )
    return StreamingResponse(frames, media_type="text/event-stream")


@router.get("/chat/messages")
async def chat_messages(
    request: Request, authorization: str | None = Header(default=None)
) -> JSONResponse:
    """This session's messages, oldest first, and this tenant's only.

    The session read is the token's `sid` and the read is bound to the token's
    tenant, so a token whose `sid` names another tenant's conversation reads an
    empty window rather than somebody else's chat
    (`::test_the_history_route_cannot_read_another_tenants_session`). The order
    is `list_recent_messages`'s own — by `ordinal`, never by `created_at`,
    which is transaction time and identical across one turn's rows.

    No rate limit: the design bounds messages, and refusing a client its own
    transcript during a window it has spent would hide the very turns it is
    asking about.
    """
    settings = load_settings()
    claims = _claims(_bearer(authorization), settings.jwt_secret)
    if claims is None:
        return _typed(ErrorCode.FORBIDDEN)
    try:
        identity = _identity(claims)
    except ValueError:
        return _typed(ErrorCode.VALIDATION)

    try:
        async with tenant_session(request.app.state.session_factory, identity.tenant_id) as db:
            rows = await list_recent_messages(db, identity.session_id)
            body = ChatHistoryResponse(
                messages=[ChatMessage(role=row.role, content=row.content) for row in rows]
            )
    except ToolError as exc:
        return _typed(exc.code)
    return JSONResponse(content=body.model_dump())
