"""An earlier change section 6: the five in-process metrics and the exposition they render.

Written before the implementation exists. Surface under test:

``helpdesk.obs.metrics``
    ``class Metrics`` — a frozen dataclass carrying ``turns_total``,
        ``tool_calls_total``, ``tool_latency_seconds``, ``refusals_total`` and
        ``sod_blocks_total``.
    ``def build_metrics(registry: CollectorRegistry) -> Metrics`` — registers
        exactly the design's five in-process metrics on the given registry, with
        exactly the design's names and label sets.
    ``REGISTRY: CollectorRegistry`` and ``METRICS: Metrics`` — the module-level
        default pair every call site reads.
    ``def render() -> tuple[str, str]`` — ``(body, content_type)`` off the
        module-level ``REGISTRY``, read at CALL time.
``helpdesk.obs`` re-exports ``Metrics``, ``build_metrics``, ``render`` and
``METRICS``.

No backing service: a registry is an in-process object, and an earlier change's §2 says
nothing in ``helpdesk/obs/`` reaches a network or a store. Per-test isolation
is a fresh ``CollectorRegistry`` monkeypatched over the module-level pair
(decision C), never a reset of a shared one, so every assertion below is an
absolute value rather than a delta.

Names are asserted as the strings a scraper reads, which for a counter is the
family name with ``_total`` restored: ``prometheus_client`` strips that suffix
when it builds the family and puts it back in the exposition, so the exposition
is where the design's literal spelling can be pinned.
"""

from collections.abc import Iterator, Mapping
from typing import Any

import pytest
from helpdesk.obs import metrics
from helpdesk.obs.metrics import build_metrics, render
from prometheus_client import CollectorRegistry, generate_latest

# the design's five in-process metrics, as the exposition spells them, with the
# type each one must be. A histogram is what makes a latency observable at all,
# so the type is part of the name assertion and not decoration.
SPEC_METRICS: Mapping[str, str] = {
    "helpdesk_turns_total": "counter",
    "helpdesk_tool_calls_total": "counter",
    "helpdesk_tool_latency_seconds": "histogram",
    "helpdesk_refusals_total": "counter",
    "helpdesk_sod_blocks_total": "counter",
}

# the design's label sets, as a literal. The field name is how the test reaches
# the metric off `Metrics`; the exposed name is what it is called on the wire.
LABEL_CASES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("turns_total", "helpdesk_turns_total", ("tenant", "intent", "decision")),
    ("tool_calls_total", "helpdesk_tool_calls_total", ("tool", "status")),
    ("tool_latency_seconds", "helpdesk_tool_latency_seconds", ("tool",)),
    ("refusals_total", "helpdesk_refusals_total", ("tenant", "reason")),
    ("sod_blocks_total", "helpdesk_sod_blocks_total", ()),
)

CONTENT_TYPE_LITERAL = "text/plain; version=1.0.0; charset=utf-8"
"""Prometheus's text exposition content type, verbatim.

Asserted as the string rather than as `prometheus_client.CONTENT_TYPE_LATEST`
so an upgrade that moves the default to OpenMetrics -- or to any other format
a scraper would stop parsing -- is caught here and not in production.

The version is 1.0.0, the text format prometheus_client has answered with
since 0.23 and the one the installed 0.26 renders. An earlier change's §1 was written
against the older 0.0.4 default and the owner decided to take the current one
(the project notes, a project rule); the two formats are wire-compatible for a
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

    a project rule's shape one level down: nothing expensive is rebuilt, and
    the isolation is a namespace — this registry — rather than a reset of a
    process-wide one, which would race any other test that had incremented it.
    """
    registry = CollectorRegistry()
    monkeypatch.setattr(metrics, "REGISTRY", registry)
    monkeypatch.setattr(metrics, "METRICS", build_metrics(registry))
    yield registry


def test_build_metrics_registers_exactly_the_five_metrics() -> None:
    """Catches a `build_metrics` that registers a sixth metric on the caller's
    registry — the three sweep metrics decision B defers to row 21 among them —
    or that misspells one of the design's names, either of which makes the
    dashboard query for that name return nothing at all.

    Asserted as an equality over the whole exposition, not as five membership
    checks: a set that also contains something else must fail here, which is
    what "exactly" means.
    """
    registry = CollectorRegistry()

    built = build_metrics(registry)

    assert _type_lines(generate_latest(registry).decode()) == dict(SPEC_METRICS)

    # Five distinct objects: a `Metrics` whose fields alias one counter would
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
    """Catches a label set that is not the design's: a missing label loses the
    dimension every query slices on, and an EXTRA label is the cardinality leak
    an earlier change's second risk names, because the only values left to fill it with
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
    if exposed_name.endswith("_seconds"):
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
