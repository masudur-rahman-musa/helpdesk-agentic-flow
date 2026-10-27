"""In-process Prometheus metrics and the scrape body.

`build_metrics` declares the counters on every service, so each scrape names
them from the start. The three gauges are installed only by the scheduler, which
moves them: a gauge reports 0 from birth, and an audit gauge at 0 elsewhere
would read as a tampered log. Label values are enums, tool names or tenant
ids, never client text and never a person, since the scrape is unauthenticated.
"""

from collections.abc import MutableMapping
from dataclasses import dataclass
from weakref import WeakKeyDictionary

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    disable_created_metrics,
    generate_latest,
)
from prometheus_client.gc_collector import GCCollector
from prometheus_client.platform_collector import PlatformCollector
from prometheus_client.process_collector import ProcessCollector

disable_created_metrics()  # type: ignore[no-untyped-call]
"""Drop the `_created` series beside every counter; nothing reads it and it doubles the scrape."""

TURNS_TOTAL = "helpdesk_turns_total"
TOOL_CALLS_TOTAL = "helpdesk_tool_calls_total"
TOOL_LATENCY_SECONDS = "helpdesk_tool_latency_seconds"
REFUSALS_TOTAL = "helpdesk_refusals_total"
SOD_BLOCKS_TOTAL = "helpdesk_sod_blocks_total"
EXPIRIES_TOTAL = "helpdesk_expiries_total"
JOB_RUNS_TOTAL = "helpdesk_job_runs_total"
JOB_FAILURES_TOTAL = "helpdesk_job_failures_total"
TURN_ENDINGS_TOTAL = "helpdesk_turn_endings_total"
TURNS_TO_FILE = "helpdesk_turns_to_file"
POLICY_CACHE_TOTAL = "helpdesk_policy_cache_total"

TURNS_TO_FILE_BUCKETS = (1, 2, 3, 4, 5, 6, 8, 10, 15)
"""A chat that needs more than fifteen messages to file lands in `+Inf`."""

UNKNOWN_TENANT = "unknown"
"""Spelled out, so a series with no tenant is not mistaken for a broken id."""

UNKNOWN_INTENT = "unknown"
"""The intent of a turn that failed before it was classified."""


@dataclass(frozen=True)
class Metrics:
    """All collectors of one registry, so a call site cannot mix registries."""

    turns_total: Counter
    tool_calls_total: Counter
    tool_latency_seconds: Histogram
    refusals_total: Counter
    sod_blocks_total: Counter
    expiries_total: Counter
    job_runs_total: Counter
    job_failures_total: Counter
    turn_endings_total: Counter
    turns_to_file: Histogram
    policy_cache_total: Counter


def build_metrics(registry: CollectorRegistry) -> Metrics:
    """Register the counters on `registry`. Dashboards query by these label sets."""
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
        expiries_total=Counter(
            EXPIRIES_TOTAL,
            "Grants the scheduler's sweep marked expired.",
            registry=registry,
        ),
        job_runs_total=Counter(
            JOB_RUNS_TOTAL,
            "Scheduler job runs completed, by job name.",
            labelnames=("job",),
            registry=registry,
        ),
        job_failures_total=Counter(
            JOB_FAILURES_TOTAL,
            "Scheduler job runs that raised an exception, by job name.",
            labelnames=("job",),
            registry=registry,
        ),
        turn_endings_total=Counter(
            TURN_ENDINGS_TOTAL,
            "How each chat turn ended, by tenant and outcome (filed, asked, stopped, "
            "refused, failed, unclear). A plain answer counts none.",
            labelnames=("tenant", "outcome"),
            registry=registry,
        ),
        turns_to_file=Histogram(
            TURNS_TO_FILE,
            "User messages in the chat when a request was filed, by tenant.",
            labelnames=("tenant",),
            buckets=TURNS_TO_FILE_BUCKETS,
            registry=registry,
        ),
        policy_cache_total=Counter(
            POLICY_CACHE_TOTAL,
            "Policy searches answered from the search cache or not, by tenant.",
            labelnames=("tenant", "result"),
            registry=registry,
        ),
    )


def _build_default_registry() -> CollectorRegistry:
    """Our own registry, with the process and Python collectors added on purpose."""
    registry = CollectorRegistry()
    ProcessCollector(registry=registry)
    PlatformCollector(registry=registry)
    GCCollector(registry=registry)
    return registry


REGISTRY = _build_default_registry()

METRICS = build_metrics(REGISTRY)
"""Read as `metrics.METRICS`, never imported, so a test can swap in a fresh registry."""


def render() -> tuple[str, str]:
    """The scrape body and its content type, read from the current registry."""
    return generate_latest(REGISTRY).decode("utf-8"), CONTENT_TYPE_LATEST


def record_turn(*, tenant: str | None, intent: str, decision: str) -> None:
    METRICS.turns_total.labels(
        tenant=tenant or UNKNOWN_TENANT, intent=intent, decision=decision
    ).inc()


def record_turn_ending(*, tenant: str, outcome: str) -> None:
    """`outcome` is one of the six closed names, never client text."""
    METRICS.turn_endings_total.labels(tenant=tenant, outcome=outcome).inc()


def record_turns_to_file(*, tenant: str, user_messages: int) -> None:
    METRICS.turns_to_file.labels(tenant=tenant).observe(user_messages)


def record_refusal(*, tenant: str | None, reason: str) -> None:
    """`reason` is an `ErrorCode` value, never the exception's own words."""
    METRICS.refusals_total.labels(tenant=tenant or UNKNOWN_TENANT, reason=reason).inc()


def record_tool_call(*, tool: str, status: str, duration_seconds: float) -> None:
    """Count and latency together, so the two always cover the same calls."""
    METRICS.tool_calls_total.labels(tool=tool, status=status).inc()
    METRICS.tool_latency_seconds.labels(tool=tool).observe(duration_seconds)


def record_expiries(count: int) -> None:
    """Called once after a sweep commits, so rolled-back rows are never counted."""
    METRICS.expiries_total.inc(count)


def record_sod_block() -> None:
    """Counted where the refusal is answered, so the policy code stays pure."""
    METRICS.sod_blocks_total.inc()


def record_policy_cache(*, tenant: str, result: str) -> None:
    """`result` is `hit` or `miss`, never client text."""
    METRICS.policy_cache_total.labels(tenant=tenant, result=result).inc()


def record_job_run(*, job: str) -> None:
    METRICS.job_runs_total.labels(job=job).inc()


def record_job_failure(*, job: str) -> None:
    METRICS.job_failures_total.labels(job=job).inc()


GRANTS_ACTIVE = "helpdesk_grants_active"
AUDIT_CHAIN_OK = "helpdesk_audit_chain_ok"
CHATS_WAITING = "helpdesk_chats_waiting"


@dataclass(frozen=True)
class SweepGauges:
    """The scheduler's three gauges, held together like `Metrics`."""

    grants_active: Gauge
    audit_chain_ok: Gauge
    chats_waiting: Gauge


_INSTALLED_SWEEP_GAUGES: MutableMapping[CollectorRegistry, SweepGauges] = WeakKeyDictionary()


def install_sweep_gauges(registry: CollectorRegistry) -> SweepGauges:
    """Register the three gauges on `registry` once; a second call returns the same pair."""
    installed = _INSTALLED_SWEEP_GAUGES.get(registry)
    if installed is not None:
        return installed
    gauges = SweepGauges(
        grants_active=Gauge(
            GRANTS_ACTIVE,
            "Grants that are live right now, by tenant.",
            labelnames=("tenant",),
            registry=registry,
        ),
        audit_chain_ok=Gauge(
            AUDIT_CHAIN_OK,
            "1 while every tenant this process verified has an intact audit chain.",
            registry=registry,
        ),
        chats_waiting=Gauge(
            CHATS_WAITING,
            "Chats left waiting on a question for 30 minutes or more, by tenant.",
            labelnames=("tenant",),
            registry=registry,
        ),
    )
    _INSTALLED_SWEEP_GAUGES[registry] = gauges
    return gauges


SWEEP_GAUGES: SweepGauges | None = None
"""None outside the scheduler, so a stray recorder call fails loudly."""


def _sweep_gauges() -> SweepGauges:
    if SWEEP_GAUGES is None:
        raise RuntimeError(
            "the sweep gauges are not installed in this process; "
            "call install_sweep_gauges(REGISTRY) where the sweep runs"
        )
    return SWEEP_GAUGES


def record_grants_active(*, tenant: str, count: int) -> None:
    """Set, not incremented: adding would double the count every sweep."""
    _sweep_gauges().grants_active.labels(tenant=tenant).set(count)


def record_chats_waiting(*, tenant: str, count: int) -> None:
    """Set, not incremented, like `record_grants_active`."""
    _sweep_gauges().chats_waiting.labels(tenant=tenant).set(count)


def record_audit_chain_ok(ok: bool) -> None:
    """One verdict for the whole process; the caller combines the tenants first."""
    _sweep_gauges().audit_chain_ok.set(1 if ok else 0)


__all__ = [
    "AUDIT_CHAIN_OK",
    "CHATS_WAITING",
    "EXPIRIES_TOTAL",
    "GRANTS_ACTIVE",
    "JOB_FAILURES_TOTAL",
    "JOB_RUNS_TOTAL",
    "METRICS",
    "REFUSALS_TOTAL",
    "REGISTRY",
    "SOD_BLOCKS_TOTAL",
    "SWEEP_GAUGES",
    "TOOL_CALLS_TOTAL",
    "TOOL_LATENCY_SECONDS",
    "TURNS_TOTAL",
    "TURNS_TO_FILE",
    "TURNS_TO_FILE_BUCKETS",
    "TURN_ENDINGS_TOTAL",
    "UNKNOWN_INTENT",
    "UNKNOWN_TENANT",
    "Metrics",
    "SweepGauges",
    "build_metrics",
    "install_sweep_gauges",
    "record_audit_chain_ok",
    "record_chats_waiting",
    "record_expiries",
    "record_grants_active",
    "record_job_failure",
    "record_job_run",
    "record_refusal",
    "record_sod_block",
    "record_tool_call",
    "record_turn",
    "record_turn_ending",
    "record_turns_to_file",
    "render",
]
