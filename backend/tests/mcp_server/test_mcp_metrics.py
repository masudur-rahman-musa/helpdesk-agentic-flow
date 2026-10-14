"""An earlier change section 6: `/metrics` on mcp-server, and the `dispatch` call site.

Written before the implementation exists. Surface under test:

``mcp_server.main.create_app`` — an unauthenticated ``GET /metrics``
    answering ``helpdesk.obs.metrics.render()``, registered BEFORE
    ``app.mount("/", mcp_app)`` so the streamable HTTP transport cannot
    swallow it (the shape ``/healthz`` and ``/readyz`` already have).
``mcp_server.tools.dispatch.dispatch`` — one ``finally`` covering the success path
    and every refusal path, incrementing
    ``helpdesk_tool_calls_total{tool,status}`` once and observing
    ``helpdesk_tool_latency_seconds{tool}`` once, with ``status`` either
    ``"ok"`` or the typed ``ErrorCode`` value; a ``SOD_CONFLICT`` refusal also
    increments ``helpdesk_sod_blocks_total``.

Cost and isolation. The compose Postgres and Redis through this suite's
existing session-scoped fixtures — one throwaway seeded database and one
dedicated Redis index for the whole run. Nothing new is
built per test: each case's isolation is its own ``CollectorRegistry``,
installed over ``helpdesk.obs.metrics``'s module-level pair, so every number
below is an absolute value and never a delta.

The synthetic ``ToolSpec`` helpers are ``test_mcp_pipeline.py``'s, imported
rather than copied: they are how this suite already reaches a chosen refusal
code without a real tool having to be able to produce it.
"""

import uuid
from collections.abc import Iterator, Mapping

import pytest
from prometheus_client import CollectorRegistry
from test_mcp_pipeline import (
    NO_REAL_PRINCIPALS,
    SPEC_TOOL,
    _dispatch,
    _raiser,
    _spec,
    _typed_error,
)

from helpdesk.shared.errors import ErrorCode
from helpdesk.shared.obs import metrics
from tests.support.mcp import (  # noqa: F401 - fixtures are resolved by name
    SeededMcp,
    bearer,
    call_tool,
    error_code,
    http_get,
    mcp_env,
    mcp_pg,
    mcp_redis_base_url,
    mcp_redis_url,
    seeded_mcp_db,
    token_for,
)

TOOL_CALLS = "helpdesk_tool_calls_total"
TOOL_LATENCY_COUNT = "helpdesk_tool_latency_seconds_count"
SOD_BLOCKS = "helpdesk_sod_blocks_total"

CONTENT_TYPE_LITERAL = "text/plain; version=1.0.0; charset=utf-8"

SPEC_METRIC_NAMES: tuple[str, str, str, str, str] = (
    "helpdesk_turns_total",
    "helpdesk_tool_calls_total",
    "helpdesk_tool_latency_seconds",
    "helpdesk_refusals_total",
    "helpdesk_sod_blocks_total",
)

# A name no registry entry has and no other test writes, so its absence from
# the exposition is a statement about THIS call and not about the fixture.
UNREGISTERED_NAME = f"not_a_registered_tool_{uuid.uuid4().hex[:8]}"

LEAK_MARKER = "leaky-detail-that-must-never-cross-the-boundary"


@pytest.fixture
def fresh_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[CollectorRegistry]:
    """A registry of this test's own, installed over the module-level pair.

    Decision C: the call sites read `helpdesk.obs.metrics.METRICS` at call
    time, so replacing both names here is what makes an absolute assertion
    possible without resetting anything process-wide (a project rule's shape:
    a namespace, never a rebuild).
    """
    registry = CollectorRegistry()
    monkeypatch.setattr(metrics, "REGISTRY", registry)
    monkeypatch.setattr(metrics, "METRICS", metrics.build_metrics(registry))
    yield registry


def _sample(registry: CollectorRegistry, name: str, labels: Mapping[str, str]) -> float | None:
    return registry.get_sample_value(name, dict(labels))


def _sum_of(registry: CollectorRegistry, name: str) -> float:
    """Every sample of `name` on this registry, added up across label sets.

    What "exactly one" is asserted with: a cell of 1 says the right series was
    incremented, and this total says no OTHER series was incremented too.
    """
    return sum(
        sample.value
        for family in registry.collect()
        for sample in family.samples
        if sample.name == name
    )


def test_the_metrics_route_answers_before_the_mcp_mount(
    mcp_env: SeededMcp,  # noqa: F811
    fresh_registry: CollectorRegistry,
) -> None:
    """Catches `/metrics` registered AFTER `app.mount("/", mcp_app)`.

    The mount matches every path, so a route added after it is unreachable and
    the scrape reaches the streamable HTTP transport instead — which answers a
    plain GET with its own refusal, never a 200 carrying an exposition. The
    content type is asserted as the exact string because a scraper dispatches
    on it, and no `Authorization` header is sent because decision D makes this
    endpoint unauthenticated (`/healthz`'s shape).
    """
    response = http_get("/metrics")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == CONTENT_TYPE_LITERAL
    for name in SPEC_METRIC_NAMES:
        assert f"# TYPE {name} " in response.text, name


def test_a_successful_tool_call_counts_once_and_observes_once(
    mcp_env: SeededMcp,  # noqa: F811
    fresh_registry: CollectorRegistry,
) -> None:
    """Catches a counter incremented on the return statement only, which counts
    a success and skips the latency observation, and catches a call counted in
    both a `try` and an `except` so one call reports two.

    The count and the observation are asserted TOGETHER on one call: they are
    what a rate and a quantile are computed from, and a `_count` that disagrees
    with the counter makes every latency panel wrong by a factor nobody can
    see. The totals pin that no OTHER label set moved, so a status label
    derived from the wrong place fails here rather than passing on the cell.
    """
    result = call_tool(bearer(token_for(mcp_env.tenant_a)), SPEC_TOOL.value, {})

    assert result.is_error is False, result.content
    assert _sample(fresh_registry, TOOL_CALLS, {"tool": SPEC_TOOL.value, "status": "ok"}) == 1.0
    assert _sum_of(fresh_registry, TOOL_CALLS) == 1.0
    assert _sample(fresh_registry, TOOL_LATENCY_COUNT, {"tool": SPEC_TOOL.value}) == 1.0
    assert _sum_of(fresh_registry, TOOL_LATENCY_COUNT) == 1.0
    assert _sample(fresh_registry, SOD_BLOCKS, {}) == 0.0


def test_a_refused_tool_call_counts_under_its_error_code(
    mcp_env: SeededMcp,  # noqa: F811
    fresh_registry: CollectorRegistry,
) -> None:
    """Catches instrumentation on the success path alone — the shape a
    decorator around the handler takes — which makes the refusal rate this
    metric exists to report permanently zero, and catches a `status` label that
    reports every refusal as one bucket instead of the typed code.

    The refusal is an RBAC denial, which is refused BEFORE the handler runs, so
    it also pins the observation being made for a call that never reached a
    tool body: a `finally` wrapped around the handler alone observes nothing
    here, and the latency series would silently describe successes only.
    """
    token = token_for(mcp_env.tenant_a, principals=NO_REAL_PRINCIPALS)

    result = call_tool(bearer(token), SPEC_TOOL.value, {})

    assert error_code(result) == ErrorCode.FORBIDDEN.value
    forbidden = {"tool": SPEC_TOOL.value, "status": ErrorCode.FORBIDDEN.value}
    assert _sample(fresh_registry, TOOL_CALLS, forbidden) == 1.0
    assert _sample(fresh_registry, TOOL_CALLS, {"tool": SPEC_TOOL.value, "status": "ok"}) is None
    assert _sum_of(fresh_registry, TOOL_CALLS) == 1.0
    assert _sample(fresh_registry, TOOL_LATENCY_COUNT, {"tool": SPEC_TOOL.value}) == 1.0
    assert _sample(fresh_registry, SOD_BLOCKS, {}) == 0.0


def test_a_sod_conflict_increments_the_sod_counter(
    mcp_env: SeededMcp,  # noqa: F811
    fresh_registry: CollectorRegistry,
) -> None:
    """Catches the SoD counter left out, or wired to every refusal.

    `helpdesk_sod_blocks_total` is the only metric here whose ordinary value is
    zero, so an implementation that incremented it from the same place as
    `helpdesk_tool_calls_total` reads identically on a green suite and turns
    the one series a reviewer reads as "a separation-of-duties rule stopped
    something" into a count of every tool refusal there has ever been. The
    FORBIDDEN case above holds that counter at zero for exactly this reason,
    and this case is the only one that may move it.

    The refusal is raised from a synthetic spec's handler rather than staged
    through `approve_request`: every SoD block a real tool produces is caught
    inside that tool and ANSWERED as a successful result carrying
    `failure_code`, so no registered tool makes `dispatch` refuse
    `SOD_CONFLICT` today. Deleting the counter is still the only change that
    flips this case, which is what it exists to pin.
    """
    spec = _spec(_raiser(_typed_error(ErrorCode.SOD_CONFLICT)(LEAK_MARKER)))

    result = _dispatch(spec, bearer(token_for(mcp_env.tenant_a)))

    assert error_code(result) == ErrorCode.SOD_CONFLICT.value
    assert _sample(fresh_registry, SOD_BLOCKS, {}) == 1.0
    conflict = {"tool": SPEC_TOOL.value, "status": ErrorCode.SOD_CONFLICT.value}
    assert _sample(fresh_registry, TOOL_CALLS, conflict) == 1.0
    assert _sum_of(fresh_registry, TOOL_CALLS) == 1.0
    assert _sample(fresh_registry, TOOL_LATENCY_COUNT, {"tool": SPEC_TOOL.value}) == 1.0


def test_an_unregistered_tool_name_creates_no_series(
    mcp_env: SeededMcp,  # noqa: F811
    fresh_registry: CollectorRegistry,
) -> None:
    """Catches instrumentation moved up into `main._call_tool`, where the label
    value would be the name the CLIENT submitted.

    That is the cardinality leak of an earlier change's second risk in its exact form: a
    label fed from client text is one series per string anybody sends, held in
    process memory forever, and the process dies of it long before anybody
    notices the dashboard. `registry.lookup` refuses the name before `dispatch`
    is entered, so the honest number of series after this call is zero — and
    the submitted name must appear nowhere in the exposition a scraper reads,
    which is where a leaked series would be visible.
    """
    result = call_tool(bearer(token_for(mcp_env.tenant_a)), UNREGISTERED_NAME, {})

    assert error_code(result) == ErrorCode.INTERNAL.value
    assert _sum_of(fresh_registry, TOOL_CALLS) == 0.0
    assert _sum_of(fresh_registry, TOOL_LATENCY_COUNT) == 0.0

    body, _content_type = metrics.render()
    assert UNREGISTERED_NAME not in body


def test_the_exposition_carries_no_argument_text_from_a_refused_call(
    mcp_env: SeededMcp,  # noqa: F811
    fresh_registry: CollectorRegistry,
) -> None:
    """Catches a `status` (or any other) label built from the exception's text
    rather than from the typed code.

    §1's input-validation criterion says no label value is client text; the
    detail carried by every refusal in this suite is `LEAK_MARKER`, so a label
    fed from `str(exc)` puts it in the exposition and nothing else does. The
    same assertion covers the tool ARGUMENTS, which is the other string a call
    site could reach for.
    """
    spec = _spec(_raiser(_typed_error(ErrorCode.INTERNAL)(LEAK_MARKER)))

    result = _dispatch(spec, bearer(token_for(mcp_env.tenant_a)), {"note": LEAK_MARKER})

    assert error_code(result) == ErrorCode.INTERNAL.value
    body, _content_type = metrics.render()
    assert LEAK_MARKER not in body
    internal = {"tool": SPEC_TOOL.value, "status": ErrorCode.INTERNAL.value}
    assert _sample(fresh_registry, TOOL_CALLS, internal) == 1.0
