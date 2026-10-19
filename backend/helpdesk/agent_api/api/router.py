"""The agent API app: health, login, audit, chat and the web app's routes.

Long-lived resources (the checkpointer pool, gateway, Redis and Qdrant
clients) are built once in `lifespan` and read off `app.state`, since a pool
per request would flood Postgres with connections.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis
from starlette.datastructures import Headers
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from helpdesk.agent_api.agent.checkpointer import build_checkpointer, build_pool, psycopg_dsn
from helpdesk.agent_api.api.middleware import RequireAuthMiddleware
from helpdesk.agent_api.api.routes.admin.audit import router as audit_router
from helpdesk.agent_api.api.routes.admin.killswitch import router as killswitch_router
from helpdesk.agent_api.api.routes.admin.policies import router as policies_router
from helpdesk.agent_api.api.routes.admin.rules import router as rules_router
from helpdesk.agent_api.api.routes.admin.settings import router as settings_router
from helpdesk.agent_api.api.routes.admin.tickets import router as tickets_router
from helpdesk.agent_api.api.routes.approvals import router as approval_router
from helpdesk.agent_api.api.routes.auth import router as auth_router
from helpdesk.agent_api.api.routes.chat import router as chat_router
from helpdesk.agent_api.api.routes.me import router as me_router
from helpdesk.agent_api.api.routes.notifications import router as notification_router
from helpdesk.agent_api.api.routes.reviews import router as review_router
from helpdesk.agent_api.core.deps import STATUS_FOR_CODE
from helpdesk.agent_api.services.auth import LoginRefusedError
from helpdesk.agent_api.services.model_gateway.client import ModelClient
from helpdesk.agent_api.services.ratelimit import RateLimitExceededError
from helpdesk.agent_api.services.tracing import configure_tracing
from helpdesk.agent_api.services.tracing import flush as flush_traces
from helpdesk.shared.auth.jwt import TokenClaims, verify_token
from helpdesk.shared.core import health as _health
from helpdesk.shared.core.config import load_settings, web_origin_from_env
from helpdesk.shared.core.db import create_engine, create_session_factory
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.core.observability.context import bind_context
from helpdesk.shared.core.observability.logging import configure_logging

REDIS_CONNECT_TIMEOUT_SECONDS = 5.0
"""How long the rate limiter waits to reach Redis. It runs before every
chat turn, so a silent Redis must not hang the turn."""

REDIS_COMMAND_TIMEOUT_SECONDS = 5.0
"""How long one command may take. Both commands are tiny, so a slow one
already means the outage the fail-closed refusal is for."""


class TelemetryContextMiddleware:
    """Bind each request's log context from its own token.

    Best effort, and it decides nothing: routes still check the token. A token
    that does not verify binds an empty context, since there is no verified
    tenant. Plain ASGI, so the bound `ContextVar` is what routes and nodes read.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        claims = _unverified_request_claims(scope)
        with bind_context(
            tenant_id=None if claims is None else claims.tenant_id,
            session_id=None if claims is None else claims.sid,
            principal_kind=None if claims is None else _principal_kind(claims),
        ):
            await self.app(scope, receive, send)


def _unverified_request_claims(scope: Scope) -> TokenClaims | None:
    """This request's claims if its token verifies, else `None`. Every failure
    gives the same answer: it only decides whether a log line names a tenant."""
    headers = Headers(scope=scope)
    scheme, _, token = headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    try:
        return verify_token(token.strip(), load_settings().jwt_secret, datetime.now(UTC))
    except Exception:
        return None


def _principal_kind(claims: TokenClaims) -> str | None:
    """All the caller's principals, sorted and comma-joined, so a line never
    names just one role. Roles, never people."""
    if not claims.principals:
        return None
    return ",".join(sorted(claims.principals))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build the long-lived resources once, and close each one at exit.

    An unclosed pool leaks Postgres connections. Shutdown closes what is on
    `app.state`, so a client a test swapped in is the one closed.
    """
    configure_logging()
    settings = load_settings()
    # With no Langfuse keys set, this builds nothing and spans are no-ops.
    configure_tracing(settings=settings)
    engine = create_engine(settings.database_url)
    pool = build_pool(psycopg_dsn(settings.database_url))
    await pool.open()

    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.model = ModelClient(base_url=settings.gateway_url, api_keys=settings.gateway_api_keys)
    app.state.redis = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
        socket_timeout=REDIS_COMMAND_TIMEOUT_SECONDS,
    )
    # Kept for the readiness check, so it does not build a client per call.
    app.state.qdrant = AsyncQdrantClient(url=settings.qdrant_url)
    app.state.checkpointer_pool = pool
    app.state.checkpointer = build_checkpointer(pool)
    try:
        yield
    finally:
        # Flush spans only at shutdown; doing it per turn would add a network
        # round trip before every reply.
        flush_traces()
        await app.state.qdrant.close()
        await app.state.redis.aclose()
        await app.state.model.aclose()
        await app.state.checkpointer_pool.close()
        await app.state.engine.dispose()


app = FastAPI(title="agent-api", lifespan=lifespan)
app.add_middleware(TelemetryContextMiddleware)
# Added after telemetry and before CORS, so it runs inside CORS (a 401 keeps
# its CORS headers) and outside the log bind.
app.add_middleware(RequireAuthMiddleware)
# Only the web app's origin, never a wildcard. Read from the environment
# because the middleware stack is fixed at import time, before `lifespan`.
# No credentials: the web app's server attaches the bearer, not the browser.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[web_origin_from_env()],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["authorization", "content-type", "accept"],
)
app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(me_router)
app.include_router(approval_router)
app.include_router(review_router)
app.include_router(notification_router)
app.include_router(audit_router)
app.include_router(killswitch_router)
app.include_router(settings_router)
app.include_router(rules_router)
app.include_router(policies_router)
app.include_router(tickets_router)

# The only place typed errors become HTTP responses. Starlette picks the most
# specific handler, so a rate limit gets 429, not the generic `ToolError` code.
# `LoginRefusedError` is not a `ToolError`, so it has its own handler.


@app.exception_handler(RateLimitExceededError)
async def handle_rate_limit(_request: Request, _exc: RateLimitExceededError) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"code": ErrorCode.POLICY_BLOCKED.value},
    )


@app.exception_handler(ToolError)
async def handle_tool_error(_request: Request, exc: ToolError) -> JSONResponse:
    return JSONResponse(
        status_code=STATUS_FOR_CODE[exc.code],
        content={"code": exc.code.value},
    )


@app.exception_handler(LoginRefusedError)
async def handle_login_refused(_request: Request, _exc: LoginRefusedError) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"code": ErrorCode.FORBIDDEN.value},
    )


def _build_agent_api_checks(state: object) -> dict[str, _health._CheckFn]:
    """The readiness checks, using the clients `lifespan` built.

    `getattr` so an app run without `lifespan` reports not ready instead of
    failing. The gateway check gets the base URL too, to find the health path.
    """
    engine = getattr(state, "engine", None)
    redis = getattr(state, "redis", None)
    qdrant = getattr(state, "qdrant", None)
    model = getattr(state, "model", None)
    return {
        "postgres": lambda: _health.ping_postgres(engine),
        "redis": lambda: _health.ping_redis(redis),
        "qdrant": lambda: _health.ping_qdrant(qdrant),
        "gateway": lambda: _health.ping_gateway(
            getattr(model, "client", None),
            getattr(model, "base_url", ""),
        ),
    }


_health.register_health(app, _build_agent_api_checks)
