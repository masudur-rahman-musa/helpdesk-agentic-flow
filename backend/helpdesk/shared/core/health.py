"""Health, readiness and metrics routes — written once, used by all three apps.

Each ping function takes the client the app already holds. No new client is
ever built here: the app's lifespan opens one connection pool and one Redis
and Qdrant client per process, and the readiness check probes through those.
A down dependency still answers not-ready, because each probe is an active
call (SELECT 1, PING, get_collections, GET /healthz) made right now, not a
report on a pooled connection from before the outage.

`register_health(app, build_checks)` adds /healthz, /readyz and /metrics to
any FastAPI app. `build_checks` receives `request.app.state` at request time
and returns the dict of names to zero-arg async callables, so each check can
close over whatever clients the app keeps on its state.

/readyz runs all checks concurrently via `asyncio.gather`, so its wait is the
slowest single check rather than the sum of them.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import text
from starlette.requests import Request

from helpdesk.shared.core.observability.metrics import render as render_metrics

GATEWAY_HEALTH_PATH = "/healthz"
"""Path appended to the origin of `GATEWAY_URL` for the gateway probe.

The gateway base URL is versioned (`…/v1`); the health endpoint is at the
service level, so the path is stripped and this is appended. A deployment
whose console health path differs sets this. Cross-repo assumption noted in
an earlier change: if the console does not serve this, the agent reports not-ready
against a healthy gateway. Moved here from `agent_api/api/routes/health.py`.
"""


class HealthResponse(BaseModel):
    status: str


class ReadinessResponse(BaseModel):
    status: str
    checks: dict[str, bool]


# ---------------------------------------------------------------------------
# Ping functions — each takes a client, never builds one
# ---------------------------------------------------------------------------


async def ping_postgres(engine: Any) -> bool:
    """Can the database answer SELECT 1 right now?

    `engine` is the `AsyncEngine` the app built once at startup. The type is
    `Any` (not `AsyncEngine`) so callers that hold the engine via `getattr`
    do not need a cast; at runtime, any object that is not a proper engine
    raises inside the `try`, which is caught and returned as False.

    Never raises: any exception returns False so /readyz reports not-ready
    rather than 500.
    """
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def ping_redis(client: Any) -> bool:
    """Can the Redis server answer PING right now?

    `client` is the `redis.asyncio.Redis` instance the app built once at
    startup. Never raises: a connection error or a wrong client type becomes
    False.
    """
    try:
        await client.ping()
        return True
    except Exception:
        return False


async def ping_qdrant(client: Any) -> bool:
    """Can the Qdrant server list its collections right now?

    `get_collections` rather than a bare TCP probe: a Qdrant answering its
    port while its storage is unavailable is still an outage for
    `search_policy`. `client` is the `AsyncQdrantClient` the app built at
    startup. Never raises: any failure returns False.
    """
    try:
        await client.get_collections()
        return True
    except Exception:
        return False


async def ping_gateway(client: Any, base_url: str) -> bool:
    """Is the Ops Console gateway answering at its service-level health route?

    Probes `{scheme}://{netloc}/healthz` derived from `base_url` (which is
    the versioned base `…/v1`). Any 2xx counts as reachable. `client` is the
    `httpx.AsyncClient` the app built at startup. Never raises: an
    unreachable host, a non-2xx response, or an unparseable `base_url` all
    return False so /readyz reports not-ready rather than 500.
    """
    try:
        url = _gateway_health_url(base_url)
        response = await client.get(url)
        return bool(200 <= response.status_code < 300)
    except Exception:
        return False


def _gateway_health_url(base_url: str) -> str:
    """`{scheme}://{netloc}/healthz` for a versioned base URL.

    Raises `ValueError` when the configured value carries no scheme or no
    host: a probe assembled from half a configuration would report on
    nothing, and the caller converts the raise to False.
    """
    parts = urlsplit(base_url)
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"base_url is not an absolute URL: {base_url!r}")
    return f"{parts.scheme}://{parts.netloc}{GATEWAY_HEALTH_PATH}"


# ---------------------------------------------------------------------------
# Route registrar
# ---------------------------------------------------------------------------

_CheckFn = Callable[[], Awaitable[bool]]
_BuildChecks = Callable[[Any], dict[str, _CheckFn]]


def register_health(app: FastAPI, build_checks: _BuildChecks) -> None:
    """Add /healthz, /readyz and /metrics to `app`.

    `build_checks(state)` is called at request time and returns a name ->
    zero-arg async callable mapping. All checks run concurrently so /readyz
    waits for the slowest single check, not the sum of them.

    Routes are registered with `include_in_schema=False` for /metrics and
    in the default OpenAPI schema for /healthz and /readyz.
    """

    @app.get("/healthz")
    async def healthz() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get("/readyz", response_model=ReadinessResponse)
    async def readyz(request: Request) -> JSONResponse:
        check_fns = build_checks(request.app.state)
        names = list(check_fns.keys())
        results: list[bool] = list(
            await asyncio.gather(*(fn() for fn in check_fns.values()))
        )
        checks = dict(zip(names, results, strict=False))
        ready = all(checks.values())
        body = ReadinessResponse(
            status="ready" if ready else "not_ready", checks=checks
        )
        return JSONResponse(
            status_code=200 if ready else 503, content=body.model_dump()
        )

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> PlainTextResponse:
        """The Prometheus scrape, unauthenticated.

        Every /metrics under a service gets here through this one registrar
        rather than each app duplicating the same two lines.
        """
        metric_body, content_type = render_metrics()
        return PlainTextResponse(content=metric_body, media_type=content_type)
