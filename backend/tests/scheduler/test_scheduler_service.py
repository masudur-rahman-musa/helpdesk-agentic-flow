"""An earlier change §6: the scheduler's HTTP surface and its lifespan task (decision H).

Written before the implementation exists. Expected surface (the implementer
must provide exactly this):

``scheduler.main``
    ``PORT = 8082`` — the port §1 fixes for this service.
    ``def create_app() -> FastAPI`` — ``GET /healthz`` (200,
        ``{"status": "ok"}``), ``GET /readyz`` (postgres and redis, 200 when
        both are reachable and 503 when either is not, body
        ``{"status": ..., "checks": {"postgres": bool, "redis": bool}}``) and
        an unauthenticated ``GET /metrics`` answering
        ``helpdesk.obs.metrics.render()``. The checks are called through the
        ``readiness`` MODULE attribute so a test can fake one, the same shape
        `mcp_server.main` uses.
    ``app = create_app()`` — the module-level app uvicorn serves.
    The lifespan starts ``runner.run_loop()`` as a task — reached through the
    ``runner`` module attribute, again so a test can replace it — and cancels
    it on shutdown.

An earlier change moved the readiness checks into `shared.core.health` as `ping_*`
functions. The faking seam is now those functions: tests patch
`health.ping_postgres` and `health.ping_redis` instead of the old
`readiness.check_*` names. The `_build_scheduler_checks` function in
`scheduler/main.py` calls them through the `_health` module attribute.

No backing service is needed here: both readiness checks are faked at the
module seam and `/metrics` renders an in-process registry.

a project rule: this file carries the service in its name; `test_health.py`
and `test_readiness.py` already exist under agent-api, which is exactly why
neither of those basenames is reused here.

No pytest-asyncio: each case is a sync function driving one `asyncio.run`.
"""

import asyncio
from typing import Any

import httpx
import pytest

from helpdesk.scheduler import main, runner
from helpdesk.shared.core import health


def _get(app: Any, path: str) -> httpx.Response:
    async def scenario() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)

    return asyncio.run(scenario())


def test_the_service_port_is_exactly_the_planned_one() -> None:
    """a project rule. Catches the scheduler landing on 8000 or 8081, where
    it would collide with agent-api or mcp-server: the process that loses the
    bind is the one that never runs a job, and nothing else in the system
    reports its absence."""
    assert main.PORT == 8082


def test_healthz_answers_ok() -> None:
    """Catches a service whose only endpoint is the loop: without `/healthz`
    an orchestrator has nothing to restart on, so a scheduler wedged in a
    dead loop keeps its container up for ever (REQUIREMENTS #17)."""
    response = _get(main.app, "/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_reports_both_dependencies_and_answers_200_when_both_are_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a `/readyz` that reports on postgres alone. Every job in this
    service needs BOTH — Redis holds the cadence gate, so a scheduler with no
    Redis runs nothing at all — and a readiness answer that names only one
    dependency reports ready through exactly that outage."""

    async def up(*args: Any) -> bool:
        return True

    monkeypatch.setattr(health, "ping_postgres", up)
    monkeypatch.setattr(health, "ping_redis", up)

    response = _get(main.create_app(), "/readyz")

    assert response.status_code == 200
    assert response.json()["checks"] == {"postgres": True, "redis": True}


@pytest.mark.parametrize("down", ["postgres", "redis"])
def test_readyz_answers_503_when_either_dependency_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, down: str
) -> None:
    """Both rows, because a route that ANDs the wrong pair of checks — or
    that returns 200 with a false check in the body — is the failure this
    endpoint exists to prevent: the container reports ready throughout the
    outage, and the operator's dashboard shows a healthy scheduler expiring
    nothing. Parametrised so a route wired to one check only fails on the
    other's row."""

    async def up(*args: Any) -> bool:
        return True

    async def falls_over(*args: Any) -> bool:
        return False

    monkeypatch.setattr(health, "ping_postgres", falls_over if down == "postgres" else up)
    monkeypatch.setattr(health, "ping_redis", falls_over if down == "redis" else up)

    response = _get(main.create_app(), "/readyz")

    assert response.status_code == 503
    assert response.json()["checks"][down] is False


def test_metrics_renders_and_carries_the_expiry_counter() -> None:
    """Decision G: `helpdesk_expiries_total` is exposed on THIS service's
    scrape and nowhere else, so a counter the sweep increments with no route
    to read it is a metric no dashboard can ever show. Asserted on the `#
    TYPE` line, which is emitted for a family with no samples at all — the
    counter is named even before the first grant expires, which is the whole
    point of registering it at import."""
    response = _get(main.app, "/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "# TYPE helpdesk_expiries_total counter" in response.text


def test_healthz_fails_after_the_loop_task_dies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a `/healthz` that always returns 200 regardless of whether the
    job loop task is still running — an orchestrator relying on that endpoint
    to decide whether to restart the container would never restart a scheduler
    whose loop had quietly stopped, so no job of any tenant would ever run
    again and nothing would report it.

    The fake loop returns immediately instead of looping; after a brief wait
    for the task to finish, `/healthz` must answer 503.
    """

    async def dying_loop() -> None:
        return  # exits at once; simulates a loop that stopped

    monkeypatch.setattr(runner, "run_loop", dying_loop)

    async def scenario() -> int:
        app = main.create_app()
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.05)  # let the task finish
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/healthz")
                return response.status_code

    status = asyncio.run(scenario())
    assert status == 503


def test_healthz_stays_ok_while_the_loop_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a `/healthz` that returns 503 even when the loop task is alive,
    which would cause the orchestrator to restart a healthy container on every
    check, killing any in-flight tick mid-run.

    The fake loop blocks on an Event that is never set; inside the lifespan
    context the task is still running, so `/healthz` must answer 200.
    """

    async def blocking_loop() -> None:
        await asyncio.Event().wait()  # blocks until cancelled

    monkeypatch.setattr(runner, "run_loop", blocking_loop)

    async def scenario() -> tuple[int, Any]:
        app = main.create_app()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/healthz")
                return response.status_code, response.json()

    status, body = asyncio.run(scenario())
    assert status == 200
    assert body == {"status": "ok"}


def test_the_lifespan_starts_the_loop_and_cancels_it_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision H. Catches an app that serves the three endpoints while no
    loop runs at all — the service would pass every health check and expire
    nothing, which is the worst of the failure modes here because it is
    silent — and one that starts the loop without cancelling it, leaving a
    task running against a closed engine through shutdown and a container
    that never exits.

    The loop is replaced at the `runner` module attribute, so the assertion
    is about the lifespan wiring rather than about what a real tick does.
    """
    started = asyncio.Event()
    cancelled: list[bool] = []

    async def fake_loop() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    monkeypatch.setattr(runner, "run_loop", fake_loop)

    async def scenario() -> None:
        app = main.create_app()
        async with app.router.lifespan_context(app):
            await asyncio.wait_for(started.wait(), timeout=5)
        assert cancelled == [True]

    asyncio.run(scenario())
