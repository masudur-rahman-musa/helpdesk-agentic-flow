"""Health, readiness and metrics routes, shared by all three apps.

Each ping reuses the app's own client and makes a live call, so a dependency
that went down after startup still reads as not ready.
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
"""Appended to the gateway's origin; the configured base URL carries a `/v1` path."""


class HealthResponse(BaseModel):
    status: str


class ReadinessResponse(BaseModel):
    status: str
    checks: dict[str, bool]


async def ping_postgres(engine: Any) -> bool:
    """Can the database answer SELECT 1? Never raises, so /readyz says 503, not 500."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def ping_redis(client: Any) -> bool:
    """Can Redis answer PING? Never raises."""
    try:
        await client.ping()
        return True
    except Exception:
        return False


async def ping_qdrant(client: Any) -> bool:
    """Can Qdrant list collections? A real call, since an open port with broken storage is down."""
    try:
        await client.get_collections()
        return True
    except Exception:
        return False


async def ping_gateway(client: Any, base_url: str) -> bool:
    """Does the gateway's health route answer 2xx? Never raises."""
    try:
        url = _gateway_health_url(base_url)
        response = await client.get(url)
        return bool(200 <= response.status_code < 300)
    except Exception:
        return False


def _gateway_health_url(base_url: str) -> str:
    parts = urlsplit(base_url)
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"base_url is not an absolute URL: {base_url!r}")
    return f"{parts.scheme}://{parts.netloc}{GATEWAY_HEALTH_PATH}"


_CheckFn = Callable[[], Awaitable[bool]]
_BuildChecks = Callable[[Any], dict[str, _CheckFn]]
_LivenessFn = Callable[[], bool]


def register_health(
    app: FastAPI,
    build_checks: _BuildChecks,
    *,
    liveness: _LivenessFn | None = None,
) -> None:
    """Add /healthz, /readyz and /metrics to `app`.

    `build_checks(state)` runs per request; the checks run at once. A false
    `liveness` makes /healthz answer 503, so a stuck loop gets restarted.
    """

    @app.get("/healthz", response_model=None)
    async def healthz() -> JSONResponse:
        if liveness is not None and not liveness():
            return JSONResponse(status_code=503, content={"status": "dead"})
        return JSONResponse(status_code=200, content={"status": "ok"})

    @app.get("/readyz", response_model=ReadinessResponse)
    async def readyz(request: Request) -> JSONResponse:
        check_fns = build_checks(request.app.state)
        names = list(check_fns.keys())
        results: list[bool] = list(await asyncio.gather(*(fn() for fn in check_fns.values())))
        checks = dict(zip(names, results, strict=False))
        ready = all(checks.values())
        body = ReadinessResponse(status="ready" if ready else "not_ready", checks=checks)
        return JSONResponse(status_code=200 if ready else 503, content=body.model_dump())

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> PlainTextResponse:
        """The Prometheus scrape, unauthenticated."""
        metric_body, content_type = render_metrics()
        return PlainTextResponse(content=metric_body, media_type=content_type)
