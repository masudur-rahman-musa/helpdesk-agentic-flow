"""The in-process metrics and the exposition they render.

`build_metrics` registers a SIXTH metric,
`helpdesk_expiries_total`, the counter the scheduler's expiry sweep moves.
This file is the schema-literal for the exposition in the
same sense `test_models.py` is for the table list, so a change that adds a
metric extends the literal here — the expectation below is a snapshot of a
deferred set. Nothing is weakened: the assertion is still an equality over the whole
exposition, and one more name has to be spelled exactly right for it to hold.

Written before the implementation exists. Surface under test:

``helpdesk.obs.metrics``
    ``class Metrics`` — a frozen dataclass carrying ``turns_total``,
        ``tool_calls_total``, ``tool_latency_seconds``, ``refusals_total``,
        ``sod_blocks_total`` and ``expiries_total``.
    ``def build_metrics(registry: CollectorRegistry) -> Metrics`` — registers
        exactly the six event metrics on the given registry, with
        exactly the expected names and label sets.
    ``REGISTRY: CollectorRegistry`` and ``METRICS: Metrics`` — the module-level
        default pair every call site reads.
    ``def render() -> tuple[str, str]`` — ``(body, content_type)`` off the
        module-level ``REGISTRY``, read at CALL time.
    ``class SweepGauges`` — a frozen dataclass carrying ``grants_active`` and
        ``audit_chain_ok``.
    ``def install_sweep_gauges(registry: CollectorRegistry) -> SweepGauges`` —
        registers the two remaining gauges, ``helpdesk_grants_active``
        (label ``tenant``) and ``helpdesk_audit_chain_ok`` (no labels), and is
        IDEMPOTENT per registry: a second call returns the same pair rather
        than raising prometheus's duplicate-registration ``ValueError``.
``helpdesk.obs`` re-exports ``Metrics``, ``build_metrics``, ``render`` and
``METRICS``.

No backing service: a registry is an in-process object, and nothing in ``helpdesk/obs/`` reaches a
network or a store. Per-test isolation
is a fresh ``CollectorRegistry`` monkeypatched over the module-level pair,
never a reset of a shared one, so every assertion below is an
absolute value rather than a delta.

Names are asserted as the strings a scraper reads, which for a counter is the
family name with ``_total`` restored: ``prometheus_client`` strips that suffix
when it builds the family and puts it back in the exposition, so the exposition
is where the literal spelling can be pinned.
"""

from collections.abc import Iterator, Mapping
from typing import Any

import pytest
from prometheus_client import CollectorRegistry, generate_latest

from helpdesk.shared.core.observability import metrics
from helpdesk.shared.core.observability.metrics import build_metrics, install_sweep_gauges, render

# The six in-process metrics, as the exposition spells them, with the
# type each one must be. A histogram is what makes a latency observable at all,
# so the type is part of the name assertion and not decoration.
SPEC_METRICS: Mapping[str, str] = {
    "helpdesk_turns_total": "counter",
    "helpdesk_tool_calls_total": "counter",
    "helpdesk_tool_latency_seconds": "histogram",
    "helpdesk_refusals_total": "counter",
    "helpdesk_sod_blocks_total": "counter",
    # An event counter, moved by the scheduler's sweep.
    "helpdesk_expiries_total": "counter",
    # Per-job run and failure counters.
    "helpdesk_job_runs_total": "counter",
    "helpdesk_job_failures_total": "counter",
    # How each turn ended, and how many turns it took to file a request.
    "helpdesk_turn_endings_total": "counter",
    "helpdesk_turns_to_file": "histogram",
    # Policy search cache hits and misses, per tenant.
    "helpdesk_policy_cache_total": "counter",
}

# The label sets, as a literal. The field name is how the test reaches
# the metric off `Metrics`; the exposed name is what it is called on the wire.
LABEL_CASES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("turns_total", "helpdesk_turns_total", ("tenant", "intent", "decision")),
    ("tool_calls_total", "helpdesk_tool_calls_total", ("tool", "status")),
    ("tool_latency_seconds", "helpdesk_tool_latency_seconds", ("tool",)),
    ("refusals_total", "helpdesk_refusals_total", ("tenant", "reason")),
    ("sod_blocks_total", "helpdesk_sod_blocks_total", ()),
    ("expiries_total", "helpdesk_expiries_total", ()),
    # Per-job run and failure counters.
    ("job_runs_total", "helpdesk_job_runs_total", ("job",)),
    ("job_failures_total", "helpdesk_job_failures_total", ("job",)),
    ("turn_endings_total", "helpdesk_turn_endings_total", ("tenant", "outcome")),
    ("turns_to_file", "helpdesk_turns_to_file", ("tenant",)),
    ("policy_cache_total", "helpdesk_policy_cache_total", ("tenant", "result")),
)

# The two remaining gauges, installed by a SEPARATE factory
# and NOT by `build_metrics`. Same shape as `SPEC_METRICS` above:
# the exposition's spelling, with the type each one must be.
SWEEP_METRICS: Mapping[str, str] = {
    "helpdesk_grants_active": "gauge",
    "helpdesk_audit_chain_ok": "gauge",
    "helpdesk_chats_waiting": "gauge",
}

CONTENT_TYPE_LITERAL = "text/plain; version=1.0.0; charset=utf-8"
"""Prometheus's text exposition content type, verbatim.

Asserted as the string rather than as `prometheus_client.CONTENT_TYPE_LATEST`
so an upgrade that moves the default to OpenMetrics -- or to any other format
a scraper would stop parsing -- is caught here and not in production.

The version is 1.0.0, the text format prometheus_client has answered with
since 0.23 and the one the installed 0.26 renders. An older 0.0.4 default was
once expected, and the current one was taken instead; the two formats are wire-compatible for a
scraper, and pinning the client backwards to keep an older string was the
alternative rejected.
"""


def _type_lines(body: str) -> dict[str, str]:
    """Every `# TYPE <name> <type>` line of an exposition, as a mapping.

    The TYPE line is emitted for a family with no samples at all, which is what
    makes "a metric still at zero is named" assertable.
    """
    types: dict[str, str] = {}
    for line in body.splitlines():
        if line.startswith("# TYPE "):
            _hash, _type, name, kind = line.split(" ", 3)
            types[name] = kind
    return types


@pytest.fixture
def fresh_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[CollectorRegistry]:
    """A registry of this test's own, installed over the module-level pair.

    Nothing expensive is rebuilt, and
    the isolation is a namespace — this registry — rather than a reset of a
    process-wide one, which would race any other test that had incremented it.
    """
    registry = CollectorRegistry()
    monkeypatch.setattr(metrics, "REGISTRY", registry)
    monkeypatch.setattr(metrics, "METRICS", build_metrics(registry))
    yield registry


def test_build_metrics_registers_exactly_the_eleven_metrics() -> None:
    """Catches a `build_metrics` that registers fewer or more than eight
    metrics on the caller's registry, or that misspells one of the expected
    names (including the two per-job counters),
    either of which makes the dashboard query for that name return nothing.

    The two remaining gauges are deliberately NOT here. They are installed through
    `install_sweep_gauges`, which only the
    process that MOVES them calls: an unlabelled gauge has a sample from the
    moment it is registered, so `helpdesk_audit_chain_ok` declared by
    `build_metrics` would render `0` for ever on agent-api's and mcp-server's
    exposition — indistinguishable from a tampered chain. So this case is now
    also the one that fails if that factory's two gauges are folded back in
    here; `test_install_sweep_gauges_registers_exactly_the_three_spec_gauges`
    is where they are pinned instead.

    Asserted as an equality over the whole exposition, not as membership
    checks: a set that also contains something else must fail here, which is
    what "exactly" means.
    """
    registry = CollectorRegistry()

    built = build_metrics(registry)

    assert _type_lines(generate_latest(registry).decode()) == dict(SPEC_METRICS)

    # Eleven distinct objects: a `Metrics` whose fields alias one counter would
    # satisfy the exposition above and then count every refusal as a turn.
    fields = [getattr(built, field) for field, _name, _labels in LABEL_CASES]
    assert len({id(metric) for metric in fields}) == len(LABEL_CASES)


@pytest.mark.parametrize(
    ("field", "exposed_name", "labels"),
    LABEL_CASES,
    ids=[case[1] for case in LABEL_CASES],
)
def test_each_metric_declares_exactly_its_spec_labels(
    field: str, exposed_name: str, labels: tuple[str, ...]
) -> None:
    """Catches a label set that is not the expected one: a missing label loses the
    dimension every query slices on, and an EXTRA label is the cardinality leak
    that is a risk here, because the only values left to fill it with
    are the ones nobody declared a closed set for.

    Driven through `.labels()` rather than read off a private attribute, so it
    fails on the behaviour a call site actually gets: the correct set must be
    accepted, and both a superset and a proper subset must be refused. The
    unlabelled metric is the boundary row — it must refuse `.labels()` outright
    and increment directly.
    """
    registry = CollectorRegistry()
    built = build_metrics(registry)
    metric: Any = getattr(built, field)

    if not labels:
        with pytest.raises(ValueError):
            metric.labels(tenant="fake-tenant")
        metric.inc()
        assert registry.get_sample_value(exposed_name) == 1.0
        return

    exact = dict.fromkeys(labels, "fake-value")
    child: Any = metric.labels(**exact)
    if exposed_name.endswith(("_seconds", "_to_file")):
        child.observe(0.5)
        assert registry.get_sample_value(f"{exposed_name}_count", exact) == 1.0
    else:
        child.inc()
        assert registry.get_sample_value(exposed_name, exact) == 1.0

    with pytest.raises(ValueError):
        metric.labels(**exact, unexpected="fake-value")
    with pytest.raises(ValueError):
        metric.labels(**dict(list(exact.items())[:-1]))


def test_render_answers_the_prometheus_text_content_type(fresh_registry: CollectorRegistry) -> None:
    """Catches a route handed a body with no content type, or with
    `application/json`: prometheus refuses to parse a scrape whose type it does
    not recognise, so a wrong string here is a target that silently reports no
    metrics at all.

    The registry is monkeypatched to this test's own, so it also catches a
    `render` that captured `REGISTRY` at import time — the body it returns must
    be the one this registry holds, and a captured default would render the
    process-wide collectors instead.
    """
    body, content_type = render()

    assert content_type == CONTENT_TYPE_LITERAL
    assert _type_lines(body) == dict(SPEC_METRICS)


def test_a_metric_with_no_observation_still_appears_in_the_exposition(
    fresh_registry: CollectorRegistry,
) -> None:
    """Catches metrics created lazily at the first observation — the shape a
    hand-rolled counter dict takes — so a freshly started process exposes
    nothing and every alert on `rate(helpdesk_refusals_total[5m])` reads as
    "no data" rather than "none yet", which is the difference between a healthy
    service and one nobody is watching.

    Every one of the five is asserted with NOTHING recorded on this registry,
    including `helpdesk_sod_blocks_total`, whose zero is the whole claim: a SoD
    block is rare, so the series that proves none happened is the one a
    reviewer reads.
    """
    body, _content_type = render()

    for name in SPEC_METRICS:
        assert f"# TYPE {name} {SPEC_METRICS[name]}" in body, name

    assert render()[0] == body, "an exposition with no observations is stable between scrapes"


# --- the two sweep gauges and their separate factory ----------------


def test_install_sweep_gauges_registers_exactly_the_three_spec_gauges() -> None:
    """Catches a factory that registers something other than the two expected
    remaining names — a third metric smuggled in beside them, a counter where
    the spec says gauge (a counter cannot go DOWN, so a grant count that fell
    when access was revoked would be read by `rate()` as a counter reset), or
    a misspelling, which makes the dashboard query return nothing at all.

    Also catches a `SweepGauges` whose two fields alias ONE gauge: that would
    satisfy the exposition above and then let a tenant's grant count overwrite
    the chain verdict, so `helpdesk_audit_chain_ok` would read 3 whenever three
    grants were live.

    Asserted on a registry of this case's own, so "exactly" means exactly: the
    process-wide registry already carries `build_metrics`'s six.
    """
    registry = CollectorRegistry()

    gauges = install_sweep_gauges(registry)

    assert _type_lines(generate_latest(registry).decode()) == dict(SWEEP_METRICS)
    assert gauges.grants_active is not gauges.audit_chain_ok


def test_each_sweep_gauge_declares_exactly_its_spec_labels() -> None:
    """Catches a label set that is not the expected one, in either direction.

    `helpdesk_grants_active` without its `tenant` label collapses two tenants'
    counts into one series, so the last sweep of the tick decides what the
    whole deployment shows. With an EXTRA label it is the cardinality leak
    that is a risk here — the only values left to fill it with are
    ones nobody declared a closed set for.

    `helpdesk_audit_chain_ok` is the boundary row, and its unlabelled shape is
    deliberate, not an oversight: a `tenant` label would make the
    process-wide AND unexpressible, so the gauge must refuse `.labels()`
    outright and move directly.

    Driven through `.labels()` rather than read off a private attribute, so it
    fails on the behaviour a call site actually gets.
    """
    registry = CollectorRegistry()
    gauges = install_sweep_gauges(registry)
    grants_active: Any = gauges.grants_active
    audit_chain_ok: Any = gauges.audit_chain_ok

    grants_active.labels(tenant="fake-tenant").set(3)
    assert registry.get_sample_value("helpdesk_grants_active", {"tenant": "fake-tenant"}) == 3.0
    with pytest.raises(ValueError):
        grants_active.labels(tenant="fake-tenant", unexpected="fake-value")
    with pytest.raises(ValueError):
        grants_active.labels()

    with pytest.raises(ValueError):
        audit_chain_ok.labels(tenant="fake-tenant")
    audit_chain_ok.set(1)
    assert registry.get_sample_value("helpdesk_audit_chain_ok") == 1.0


def test_installing_the_sweep_gauges_twice_on_one_registry_returns_the_same_pair() -> None:
    """Catches a factory that just constructs two `Gauge`s every call.

    prometheus refuses a duplicate registration with a bare `ValueError`, so
    the second call would raise — and the scheduler installs at IMPORT of
    `scheduler.jobs`, which means the process would die at start-up the first
    time anything imported that module twice under different names, or the
    first time a test installed over the same registry. Returning a NEW pair
    each time would be just as wrong in the other direction: the recorders
    would then move gauges nobody is scraping.

    The exposition is re-asserted after the second call, so a factory that
    "succeeds" by registering under a suffixed name fails here too.
    """
    registry = CollectorRegistry()

    first = install_sweep_gauges(registry)
    second = install_sweep_gauges(registry)

    assert second is first
    assert _type_lines(generate_latest(registry).decode()) == dict(SWEEP_METRICS)


# --- per-job run and failure counters --------------------------------


def test_each_new_job_counter_declares_a_job_label() -> None:
    """Catches a counter registered without its `job` label — a counter with
    no labels would collapse all jobs into one series, so `rate(
    helpdesk_job_runs_total[5m])` could not be split by job name and a single
    failing job would look like total scheduler failure.

    Driven through `.labels(job=...)` rather than read off a private
    attribute: a counter that accepted the call but used the wrong label name
    would fail here while a private-attribute check would pass.
    """
    registry = CollectorRegistry()
    built = build_metrics(registry)

    # Both counters must accept .labels(job=...) and reject anything else.
    for field_name in ("job_runs_total", "job_failures_total"):
        counter: Any = getattr(built, field_name)
        counter.labels(job="expire_grants").inc()
        exposed = f"helpdesk_{field_name}"
        assert registry.get_sample_value(exposed, {"job": "expire_grants"}) == 1.0, (
            f"{exposed} must be reachable via .labels(job=...)"
        )
        with pytest.raises(ValueError):
            counter.labels(job="expire_grants", unexpected="x")
        with pytest.raises(ValueError):
            counter.labels(tenant="expire_grants")
