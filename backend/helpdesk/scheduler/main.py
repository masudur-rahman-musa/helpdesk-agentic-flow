"""The scheduler's HTTP app, and the loop that is the real service.

An app rather than a bare worker, so the scheduler has `/healthz`, `/readyz`
and `/metrics` like every service; counters such as `helpdesk_expiries_total`
move in this process and can only be scraped here. The loop runs as a
lifespan task, and `/healthz` fails once it stops, so a scheduler that
answers health checks but runs nothing does not go unnoticed.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from helpdesk.scheduler import runner
from helpdesk.shared.core import health as _health
from helpdesk.shared.core.config import load_settings
from helpdesk.shared.core.db import shared_engine
from helpdesk.shared.core.observability.logging import configure_logging
from helpdesk.shared.core.redis import get_redis

logger = logging.getLogger(__name__)

PORT = 8082


def create_app() -> FastAPI:
    """The app: health, readiness, the scrape, and the loop as a lifespan task.

    The task is kept in a list local to this call, so two apps built in one
    test run never share it.
    """
    _task_holder: list[asyncio.Task[None]] = []

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        configure_logging()
        # Reached through the module attribute so a test can replace the loop.
        task: asyncio.Task[None] = asyncio.create_task(runner.run_loop())
        _task_holder.append(task)
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

    def _liveness() -> bool:
        """True while starting up or while the loop runs; False once it has stopped."""
        if not _task_holder:
            return True
        return not _task_holder[0].done()

    app = FastAPI(title="scheduler", lifespan=lifespan)
    _health.register_health(app, _build_scheduler_checks, liveness=_liveness)
    return app


def _build_scheduler_checks(state: Any) -> dict[str, _health._CheckFn]:
    """Readiness checks: Postgres holds the work and Redis holds the cadence lock.

    `state` is unused; the shared client getters are what tests replace.
    """
    settings = load_settings()
    return {
        "postgres": lambda: _health.ping_postgres(shared_engine(settings.database_url)),
        "redis": lambda: _health.ping_redis(get_redis(settings)),
    }


app = create_app()
