"""The in-process Prometheus metrics of the design, and the scrape body.

Five metrics live here — the ones a request or a tool call can move on its
own. The other three the design names (`helpdesk_grants_active`,
`helpdesk_expiries_total`, `helpdesk_audit_chain_ok`) each need a periodic
sweep over the database rather than an event, so they belong to the scheduler
row and not to this module.

Nothing here reaches a network or a store, which is what keeps the promise
`helpdesk.obs`'s docstring makes for the whole package: recording a metric is
an increment in this process's memory, and the exposition is rendered when a
scrape asks for it.

Two rules hold the label sets safe. Every label value is a closed enum member
(`Intent`, `TurnOutcome`, `ErrorCode`), a registry tool name, or the tenant
uuid the JWT carried — never client text, because a label whose values are
client text is an unbounded leak of memory in a process nobody restarts.
And no label names a person: the exposition is unauthenticated by decision D,
so the design applies to it exactly as it applies to a log line. Pinned by
`test_agent_metrics.py::test_the_exposition_names_no_user_of_the_seeded_tenant`
and
`test_mcp_metrics.py::test_the_exposition_carries_no_argument_text_from_a_refused_call`.

The metrics are built by a FACTORY over a registry that is handed in, with one
module-level default instance for the call sites (decision C). A test builds
its own registry and asserts an absolute value; against prometheus's global
default it could only assert a delta, which passes just as happily when the
wrong site did the incrementing.
"""

from dataclasses import dataclass

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    disable_created_metrics,
    generate_latest,
)
from prometheus_client.gc_collector import GCCollector
from prometheus_client.platform_collector import PlatformCollector
from prometheus_client.process_collector import ProcessCollector

disable_created_metrics()
"""Drop the `_created` gauge prometheus_client adds beside every counter.

A library default is a decision, so it is made here rather than inherited: the gauge carries the unix time a counter was first
observed, which nothing in the design asks for and which doubles the series a
scrape carries. Without this the exposition names more than the five metrics
this module declares, which
`test_obs_metrics.py::test_build_metrics_registers_exactly_the_five_metrics`
reads as a sixth.
"""

TURNS_TOTAL = "helpdesk_turns_total"
TOOL_CALLS_TOTAL = "helpdesk_tool_calls_total"
TOOL_LATENCY_SECONDS = "helpdesk_tool_latency_seconds"
REFUSALS_TOTAL = "helpdesk_refusals_total"
SOD_BLOCKS_TOTAL = "helpdesk_sod_blocks_total"

UNKNOWN_TENANT = "unknown"
"""What a tenant label reads when the call is outside a bound context.

The sentinel is spelled rather than left empty so a series with no tenant is
visibly that, and not a tenant whose id failed to render. Every instrumented
site runs inside a `bind_context`, so this value means a caller nobody has
yet, the same shape as a log line emitted before the middleware binds.
"""

UNKNOWN_INTENT = "unknown"
"""The intent of a turn that failed before anything was classified.

Pinned as the literal by
`test_agent_metrics.py::test_a_stream_that_fails_before_any_state_counts_intent_unknown`.
"""


@dataclass(frozen=True)
class Metrics:
    """The five collectors of one registry, held together so a call site
    cannot pick up four of them from one registry and the fifth from another.
    """

    turns_total: Counter
    tool_calls_total: Counter
    tool_latency_seconds: Histogram
    refusals_total: Counter
    sod_blocks_total: Counter


def build_metrics(registry: CollectorRegistry) -> Metrics:
    """Register exactly the five metrics on `registry` and return them.

    The label sets are the design's, and they are the contract a dashboard
    queries by, so they are pinned member by member by
    `test_obs_metrics.py::test_each_metric_declares_exactly_its_spec_labels`.
    """
    return Metrics(
        turns_total=Counter(
            TURNS_TOTAL,
            "Chat turns, by tenant, classified intent and final outcome.",
            labelnames=("tenant", "intent", "decision"),
            registry=registry,
        ),
        tool_calls_total=Counter(
            TOOL_CALLS_TOTAL,
            "MCP tool calls through the pipeline, by tool and typed status.",
            labelnames=("tool", "status"),
            registry=registry,
        ),
        tool_latency_seconds=Histogram(
            TOOL_LATENCY_SECONDS,
            "How long one tool call spent in the pipeline, by tool.",
            labelnames=("tool",),
            registry=registry,
        ),
        refusals_total=Counter(
            REFUSALS_TOTAL,
            "Turns that ended refused or failed, by tenant and typed error code.",
            labelnames=("tenant", "reason"),
            registry=registry,
        ),
        sod_blocks_total=Counter(
            SOD_BLOCKS_TOTAL,
            "Tool calls refused because of a separation-of-duties conflict.",
            registry=registry,
        ),
    )


def _build_default_registry() -> CollectorRegistry:
    """A dedicated registry carrying the platform collectors by decision.

    On a registry of our own the `process_*`, `python_gc_*` and
    `python_info` collectors are absent unless asked for, so they are
    registered here explicitly: a deployment gets them because this line
    says so, not because importing prometheus_client had a side effect.
    """
    registry = CollectorRegistry()
    ProcessCollector(registry=registry)
    PlatformCollector(registry=registry)
    GCCollector(registry=registry)
    return registry


REGISTRY = _build_default_registry()
"""The registry both services expose. A test installs its own over it."""

METRICS = build_metrics(REGISTRY)
"""The instance every call site records against, read through this module.

Call sites reach it as `metrics.METRICS`, never by importing the object, so a
test can install a fresh registry and read absolute values off it.
"""


def render() -> tuple[str, str]:
    """The scrape body and its content type, off the current default registry.

    `REGISTRY` is read HERE rather than captured at import, which is what lets
    a test render the registry it installed. Pinned by
    `test_obs_metrics.py::test_render_answers_the_prometheus_text_content_type`.
    """
    return generate_latest(REGISTRY).decode("utf-8"), CONTENT_TYPE_LATEST


def record_turn(*, tenant: str | None, intent: str, decision: str) -> None:
    """One finished turn, whatever the outcome (the design's `turns_total`)."""
    METRICS.turns_total.labels(
        tenant=tenant or UNKNOWN_TENANT, intent=intent, decision=decision
    ).inc()


def record_refusal(*, tenant: str | None, reason: str) -> None:
    """A turn that ended refused or failed, under its typed code.

    `reason` is an `ErrorCode` value and nothing else — the code the client
    was answered with, never the exception's own words.
    """
    METRICS.refusals_total.labels(tenant=tenant or UNKNOWN_TENANT, reason=reason).inc()


def record_tool_call(*, tool: str, status: str, duration_seconds: float) -> None:
    """One tool call: the count and the latency, always together.

    Both in one function because the two are one observation of one call, and
    a site that incremented the counter without observing the histogram would
    report a rate whose latency has a different denominator.
    """
    METRICS.tool_calls_total.labels(tool=tool, status=status).inc()
    METRICS.tool_latency_seconds.labels(tool=tool).observe(duration_seconds)


def record_sod_block() -> None:
    """A refusal whose code was `SOD_CONFLICT` (the design's `sod_blocks_total`).

    Counted where the refusal is ANSWERED, not where it is classified: the
    policy package stays pure, and a counter moved inside the transaction that
    raised would count a block whose transaction then rolled back.
    """
    METRICS.sod_blocks_total.inc()


__all__ = [
    "METRICS",
    "REGISTRY",
    "REFUSALS_TOTAL",
    "SOD_BLOCKS_TOTAL",
    "TOOL_CALLS_TOTAL",
    "TOOL_LATENCY_SECONDS",
    "TURNS_TOTAL",
    "UNKNOWN_INTENT",
    "UNKNOWN_TENANT",
    "Metrics",
    "build_metrics",
    "record_refusal",
    "record_sod_block",
    "record_tool_call",
    "record_turn",
    "render",
]
