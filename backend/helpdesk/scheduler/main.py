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
"""The scheduler's port.

Agent-api is on 8000 and mcp-server on 8081; the process that loses a bind is
the one that never runs a job, and nothing else in the system reports its
absence. Pinned by
`test_scheduler_service.py::test_the_service_port_is_exactly_the_planned_one`."""


def create_app() -> FastAPI:
    """The app: health, readiness, the scrape, and the loop as a lifespan task.

    The loop task is stored in a one-element list so the `liveness` closure
    can reach it without a module-level mutable (which a test that calls
    `create_app()` twice would share). The liveness returns True while the
    task exists and has not finished; /healthz returns 503 once the task is
    done, so an orchestrator can restart a container whose loop quietly stopped.
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
        """True before the lifespan starts or while the task runs; False once it dies.

        The lifespan hasn't started if the holder is empty — the process is
        coming up, not a dead loop — so the check returns True. Once the task is
        stored and later finishes or errors, `done()` is True and liveness is
        False: that is the case the orchestrator needs to restart on.
        """
        if not _task_holder:
            return True
        return not _task_holder[0].done()

    app = FastAPI(title="scheduler", lifespan=lifespan)
    _health.register_health(app, _build_scheduler_checks, liveness=_liveness)
    return app


def _build_scheduler_checks(state: Any) -> dict[str, _health._CheckFn]:
    """Build the two readiness checks for the scheduler using its module-level clients.

    Both Postgres and Redis are on the path of every tick: Postgres is where
    the work lives, and Redis holds the cadence gate. The module-level lazy
    getters (`shared_engine`, `get_redis`) return the same already-opened
    clients the jobs use. `state` is not used; the module-level getters are
    the seam tests patch. Pinned by `tests/shared/core/test_health.py`.
    """
    settings = load_settings()
    return {
        "postgres": lambda: _health.ping_postgres(shared_engine(settings.database_url)),
        "redis": lambda: _health.ping_redis(get_redis(settings)),
    }


app = create_app()
