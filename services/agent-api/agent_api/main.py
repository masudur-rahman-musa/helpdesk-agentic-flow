"""Agent API service. Health, login, audit verify, and the chat surface.

The process-lifetime resources live in a `lifespan` and
are reached off `app.state`, never off a module global: the checkpointer's
psycopg pool is the reason — one pool per request would be a connection storm
and a fresh handshake per message, and an earlier change decision D already fixed "one
process-wide pool, the caller owns it". The gateway client and the Redis
client ride along for the same reason and are closed with it.

`auth_routes` and `admin_routes` keep their per-request shape. They open no
pool, so a startup they never entered costs them nothing, and moving them onto
the app state would be a refactor this plan does not need.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from helpdesk.auth.jwt import TokenClaims, verify_token
from helpdesk.core.db import create_engine, create_session_factory
from helpdesk.core.settings import load_settings
from helpdesk.gateway.client import ModelClient
from helpdesk.obs.context import bind_context
from helpdesk.obs.logging import configure_logging
from pydantic import BaseModel
from redis.asyncio import Redis
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from agent_api import readiness
from agent_api.admin_routes import router as admin_router
from agent_api.auth_routes import router as auth_router
from agent_api.chat_routes import router as chat_router
from agent_api.checkpointer import build_checkpointer, build_pool, psycopg_dsn

REDIS_CONNECT_TIMEOUT_SECONDS = 5.0
"""How long the rate limiter waits to reach Redis at all.

The same connect half `agent_api.checkpointer` gives its pool: this client is
touched in front of every chat turn, and a Redis that accepts the TCP
connection and never answers would otherwise hang the turn for the operating
system's default.
"""

REDIS_COMMAND_TIMEOUT_SECONDS = 5.0
"""The read half of the pair: how long one `INCR` or `EXPIRE` may take.

Equal to the connect half rather than longer, unlike the gateway's and the MCP
client's: the two commands the limiter sends are single-key integer
operations, so a Redis taking seconds over one is already the outage the
fail-closed refusal exists for.
"""


class TelemetryContextMiddleware:
    """Bind the telemetry context of every HTTP request, from its own token.

    A plain ASGI middleware rather than `BaseHTTPMiddleware`: the bind is a
    `ContextVar`, and this shape runs the downstream app in the SAME task, so
    what is bound here is what the routes and every node under them read.

    The verification is BEST EFFORT and decides nothing. It never refuses and
    never authorizes — the routes still verify the same token for themselves,
    and they remain the only gate. What it buys is the answer to "which tenant
    and session does this line belong to" for lines emitted before, during and
    after a route's own checks. A token that does not verify (absent, expired,
    forged) binds an empty context, which is the honest answer: there is no
    verified tenant, so the line carries none.

    Identity is derived from the token and from nothing else,
    and only the four the design fields are bound; the formatter redacts the
    rest whatever a caller passes. Pinned by
    `test_logging_wiring.py::test_a_record_logged_during_a_request_carries_that_call_s_tenant_and_session`.
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
    """This request's claims if its bearer token verifies, else `None`.

    Every failure — no header, the wrong scheme, a bad signature, an expired
    token, a misconfigured process — is the same answer, because the only
    decision downstream of it is whether a log line names a tenant.
    """
    headers = Headers(scope=scope)
    scheme, _, token = headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    try:
        return verify_token(token.strip(), load_settings().jwt_secret, datetime.now(UTC))
    except Exception:
        return None


def _principal_kind(claims: TokenClaims) -> str | None:
    """The caller's principals as one telemetry-safe field.

    A user can hold several principals, so the field is all of them, sorted
    and comma-joined: sorted so the same caller reads the same on every line,
    and all of them because a line attributed to "employee" for a caller who
    is also an approver would misdescribe the call. A principal names a role,
    never a person, so this carries no identity.
    """
    if not claims.principals:
        return None
    return ",".join(sorted(claims.principals))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build the process-lifetime resources once, and close every one at exit.

    The close half is not housekeeping: a checkpointer pool left open leaks
    connections into Postgres for the life of the process (an earlier change's first
    risk), and a leaked httpx pool does the same to the gateway. Both are read
    back off the objects this function built by
    `test_chat_route.py::test_the_lifespan_closes_the_pool_and_the_clients`.

    Shutdown reads `app.state` rather than the local names, so a caller that
    swapped a client in — which the chat tests do, to stage a gateway — has its
    replacement closed rather than the original closed twice.
    """
    configure_logging()
    settings = load_settings()
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
    app.state.checkpointer_pool = pool
    app.state.checkpointer = build_checkpointer(pool)
    try:
        yield
    finally:
        await app.state.redis.aclose()
        await app.state.model.aclose()
        await app.state.checkpointer_pool.close()
        await app.state.engine.dispose()


app = FastAPI(title="agent-api", lifespan=lifespan)
app.add_middleware(TelemetryContextMiddleware)
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(chat_router)


class HealthResponse(BaseModel):
    status: str


class ReadinessResponse(BaseModel):
    status: str
    checks: dict[str, bool]


@app.get("/healthz")
async def healthz() -> HealthResponse:
    return HealthResponse(status="ok")


@app.get("/readyz", response_model=ReadinessResponse)
async def readyz() -> JSONResponse:
    # Called via the module attribute so tests can fake individual checks.
    checks = {
        "postgres": await readiness.check_postgres(),
        "redis": await readiness.check_redis(),
        "qdrant": await readiness.check_qdrant(),
        "gateway": await readiness.check_gateway(),
    }
    ready = all(checks.values())
    body = ReadinessResponse(status="ready" if ready else "not_ready", checks=checks)
    return JSONResponse(status_code=200 if ready else 503, content=body.model_dump())
