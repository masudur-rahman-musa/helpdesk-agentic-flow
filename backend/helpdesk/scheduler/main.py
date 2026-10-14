"""The scheduler's HTTP surface, and the loop that is the actual service.

Decision H: a FastAPI app on 8082 rather than a bare worker with no port.
REQUIREMENTS #17 and the design say every service answers `/healthz`, `/readyz`
and `/metrics`, and a counter nothing can scrape is not a metric — the expiry
sweep's `helpdesk_expiries_total` is moved in this process and can only be
read here.

The loop runs as a lifespan TASK, started when the app starts and cancelled
when it stops. The failure this arrangement is written against is the silent
one: an app that serves all three endpoints while nothing ticks passes every
health check and expires nothing for ever. `runner.run_loop` is reached
through the module attribute so a test can replace it, the same seam
`health`'s two checks use.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from helpdesk.scheduler import runner
from helpdesk.scheduler.api.routes import health
from helpdesk.shared.obs.logging import configure_logging
from helpdesk.shared.obs.metrics import render as render_metrics

logger = logging.getLogger(__name__)

PORT = 8082
"""The scheduler's port.

Agent-api is on 8000 and mcp-server on 8081; the process that loses a bind is
the one that never runs a job, and nothing else in the system reports its
absence. Pinned by
`test_scheduler_service.py::test_the_service_port_is_exactly_the_planned_one`."""


class HealthResponse(BaseModel):
    status: str


class ReadinessResponse(BaseModel):
    status: str
    checks: dict[str, bool]


def create_app() -> FastAPI:
    """The app: health, readiness, the scrape, and the loop as a lifespan task."""

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        configure_logging()
        # Reached through the module attribute so a test can replace the loop.
        task = asyncio.create_task(runner.run_loop())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("the scheduler loop ended with an error")

    app = FastAPI(title="scheduler", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get("/readyz", response_model=ReadinessResponse)
    async def readyz() -> JSONResponse:
        # Called through the module attribute so tests can fake one check.
        checks = {
            "postgres": await health.check_postgres(),
            "redis": await health.check_redis(),
        }
        ready = all(checks.values())
        body = ReadinessResponse(status="ready" if ready else "not_ready", checks=checks)
        return JSONResponse(status_code=200 if ready else 503, content=body.model_dump())

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> PlainTextResponse:
        """The Prometheus scrape, unauthenticated."""
        body, content_type = render_metrics()
        return PlainTextResponse(content=body, media_type=content_type)

    return app


app = create_app()
