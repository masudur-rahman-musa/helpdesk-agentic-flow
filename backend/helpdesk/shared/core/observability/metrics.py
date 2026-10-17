"""The in-process Prometheus metrics of the design, and the scrape body.

Eight metrics live here, in TWO factories, and which factory a metric belongs
to is itself a decision.

`build_metrics` registers eight on every service. The first six are the ones a
request or a tool call can move on its own; `helpdesk_expiries_total` arrived
with the scheduler's expiry sweep — an EVENT counter,
incremented by the job that expires a grant, and so a sibling of the five.
The final two, `helpdesk_job_runs_total` and `helpdesk_job_failures_total`,
count how many times each scheduler job ran and failed, labelled by job name.

All eight counters are DECLARED on every service's registry and MOVED only by
the scheduler, which is what makes them visible on that service's scrape from
the first tick (a family with no samples still renders its `# TYPE` line). A
scrape of agent-api or mcp-server shows them at zero for ever, which is the
honest reading: those processes run no jobs.

`install_sweep_gauges` registers the design's remaining two,
`helpdesk_grants_active` and `helpdesk_audit_chain_ok`, and ONLY the process
that moves them calls it. The asymmetry is the point: a
counter at zero reads as "nothing happened here", but a GAUGE carries a sample
from the moment it is registered, so an unlabelled `helpdesk_audit_chain_ok`
declared everywhere would report `0` — a tampered audit log — on two services
that never verify anything.

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
"""Drop the `_created` gauge prometheus_client adds beside every counter.

A library default is a decision, so it is made here rather than inherited: the gauge carries the unix time a counter was first
observed, which nothing in the design asks for and which doubles the series a
scrape carries. Without this the exposition names more than the eight metrics
`build_metrics` declares, which
`test_obs_metrics.py::test_build_metrics_registers_exactly_the_eight_metrics`
reads as a ninth.
"""

TURNS_TOTAL = "helpdesk_turns_total"
TOOL_CALLS_TOTAL = "helpdesk_tool_calls_total"
TOOL_LATENCY_SECONDS = "helpdesk_tool_latency_seconds"
REFUSALS_TOTAL = "helpdesk_refusals_total"
SOD_BLOCKS_TOTAL = "helpdesk_sod_blocks_total"
EXPIRIES_TOTAL = "helpdesk_expiries_total"
JOB_RUNS_TOTAL = "helpdesk_job_runs_total"
"""How many times each scheduler job ran, labelled by job name.

Declared on every service so a fresh process names the series before the first
tick; moved only by the scheduler. A scrape of agent-api or mcp-server shows it
at zero for ever, which is the honest reading: those processes run no jobs.
"""
JOB_FAILURES_TOTAL = "helpdesk_job_failures_total"
"""How many times each scheduler job failed, labelled by job name.

Same declaration rule as `JOB_RUNS_TOTAL`: declared everywhere, moved only by
the scheduler. Pinned by
`test_obs_metrics.py::test_each_new_job_counter_declares_a_job_label`.
"""

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
    """The eight collectors of one registry, held together so a call site
    cannot pick up seven of them from one registry and the eighth from another.
    """

    turns_total: Counter
    tool_calls_total: Counter
    tool_latency_seconds: Histogram
    refusals_total: Counter
    sod_blocks_total: Counter
    expiries_total: Counter
    job_runs_total: Counter
    job_failures_total: Counter


def build_metrics(registry: CollectorRegistry) -> Metrics:
    """Register exactly the eight metrics on `registry` and return them.

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
        expiries_total=Counter(
            EXPIRIES_TOTAL,
            "Grants the scheduler's sweep marked expired.",
            registry=registry,
        ),
        job_runs_total=Counter(
            JOB_RUNS_TOTAL,
            "Scheduler job runs completed, by job name (plan 72, ADR 0029).",
            labelnames=("job",),
            registry=registry,
        ),
        job_failures_total=Counter(
            JOB_FAILURES_TOTAL,
            "Scheduler job runs that raised an exception, by job name (plan 72, ADR 0029).",
            labelnames=("job",),
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
"""The registry all three services expose. A test installs its own over it."""

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


def record_expiries(count: int) -> None:
    """The grants ONE sweep expired, counted once per sweep.

    Takes the number rather than being called per row, because the sweep
    knows its set: a per-row call inside the loop would count rows the
    transaction then rolled back as expiries that happened. Called after the
    writes, for the same reason `record_sod_block` is counted where the
    refusal is answered. A sweep that expired nothing moves nothing, so the
    counter's rate is the rate access actually lapses. Pinned as an absolute
    value by
    `test_access_expiry.py::test_the_expiry_counter_rises_by_exactly_the_number_of_grants_expired`,
    which plants three lapsed grants beside two the filters exclude.
    """
    METRICS.expiries_total.inc(count)


def record_sod_block() -> None:
    """A refusal whose code was `SOD_CONFLICT` (the design's `sod_blocks_total`).

    Counted where the refusal is ANSWERED, not where it is classified: the
    policy package stays pure, and a counter moved inside the transaction that
    raised would count a block whose transaction then rolled back.
    """
    METRICS.sod_blocks_total.inc()


def record_job_run(*, job: str) -> None:
    """One scheduler job run completed for one tenant.

    Called after the job's `run` returns without raising, once per tenant per
    tick. The label is the job's registry name — a closed set, so no cardinality
    risk — making the per-job rate visible on a dashboard at a glance. Pinned by
    `test_scheduler_runner.py::test_the_job_run_counter_rises_once_per_tenant`.
    """
    METRICS.job_runs_total.labels(job=job).inc()


def record_job_failure(*, job: str) -> None:
    """One scheduler job run that raised for one tenant.

    Called when `job.run` raises a non-CancelledError exception, after the
    per-tenant lock key is deleted. `job` is the same label as `record_job_run`,
    so the failure rate is directly comparable to the run rate on the same
    dashboard row. Pinned by
    `test_scheduler_runner.py::test_the_job_failure_counter_rises_once_per_failed_tenant`.
    """
    METRICS.job_failures_total.labels(job=job).inc()


GRANTS_ACTIVE = "helpdesk_grants_active"
AUDIT_CHAIN_OK = "helpdesk_audit_chain_ok"


@dataclass(frozen=True)
class SweepGauges:
    """the design's two gauges, held together for the reason `Metrics` is.

    A pair whose two fields aliased one gauge would satisfy the exposition and
    then let a tenant's grant count overwrite the chain verdict, so
    `helpdesk_audit_chain_ok` would read 3 whenever three grants were live.
    Pinned by
    `test_obs_metrics.py::test_install_sweep_gauges_registers_exactly_the_two_spec_gauges`.
    """

    grants_active: Gauge
    audit_chain_ok: Gauge


_INSTALLED_SWEEP_GAUGES: MutableMapping[CollectorRegistry, SweepGauges] = WeakKeyDictionary()
"""What `install_sweep_gauges` has already put on each registry.

Weak keys, so a test's throwaway registry is collected with its gauges rather
than kept alive by this module for the life of the process."""


def install_sweep_gauges(registry: CollectorRegistry) -> SweepGauges:
    """Register the design's two remaining gauges on `registry`, once.

    Separate from `build_metrics` by an earlier change decision F. A prometheus gauge
    carries a sample from the moment it is registered, so an unlabelled
    `helpdesk_audit_chain_ok` declared on every service would render `0` for
    ever on agent-api's and mcp-server's exposition — a healthy deployment
    reporting a tampered audit log. These two are therefore declared by the
    process that MOVES them and by no other: `scheduler.jobs` calls this at
    import, which is the module that holds the sweep.

    Idempotent per registry, returning the SAME pair: the alternative is
    prometheus's bare duplicate-registration `ValueError` at the second import
    of the calling module. Pinned by
    `test_obs_metrics.py::test_installing_the_sweep_gauges_twice_on_one_registry_returns_the_same_pair`,
    and the label sets by `::test_each_sweep_gauge_declares_exactly_its_spec_labels`.
    """
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
    )
    _INSTALLED_SWEEP_GAUGES[registry] = gauges
    return gauges


SWEEP_GAUGES: SweepGauges | None = None
"""The pair the two recorders move, or None in a process that never sweeps.

None rather than an eagerly built instance: a process that has not installed
these has nothing to move, and a recorder called there is a bug worth a loud
error rather than a number nobody scrapes. `scheduler.jobs` binds it at
import; a test installs its own over it, which is what lets every gauge
assertion in `test_access_gauges.py` and `test_audit_health.py` be an
ABSOLUTE value.
"""


def _sweep_gauges() -> SweepGauges:
    """The installed pair, or a loud failure.

    Reads the module global at CALL time — never captured at import — so
    replacing `SWEEP_GAUGES` in a test reaches every recorder.
    """
    if SWEEP_GAUGES is None:
        raise RuntimeError(
            "the sweep gauges are not installed in this process; "
            "call install_sweep_gauges(REGISTRY) where the sweep runs"
        )
    return SWEEP_GAUGES


def record_grants_active(*, tenant: str, count: int) -> None:
    """One tenant's live-grant count, SET rather than incremented.

    A gauge answers "how many right now", so the sweep's number replaces the
    last one: `.inc(count)` — the shape every counter recorder above uses, and
    so the natural mistake — would double the reported access every cadence
    with nothing downstream to flag it. Pinned by
    `test_access_gauges.py::test_two_sweeps_over_an_unchanged_tenant_leave_the_same_value`.

    `tenant` is the bound tenant's uuid rendered by the caller, never client
    text: the label's cardinality is the tenant count.
    """
    _sweep_gauges().grants_active.labels(tenant=tenant).set(count)


def record_audit_chain_ok(ok: bool) -> None:
    """The audit chain verdict for this whole process (the design, an earlier change).

    Unlabelled by the design and by decision D, which is why the AND across
    tenants is worked out in `helpdesk.audit.health` before it reaches here:
    this function records a verdict, it does not combine them.
    """
    _sweep_gauges().audit_chain_ok.set(1 if ok else 0)


__all__ = [
    "AUDIT_CHAIN_OK",
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
    "UNKNOWN_INTENT",
    "UNKNOWN_TENANT",
    "Metrics",
    "SweepGauges",
    "build_metrics",
    "install_sweep_gauges",
    "record_audit_chain_ok",
    "record_expiries",
    "record_grants_active",
    "record_job_failure",
    "record_job_run",
    "record_refusal",
    "record_sod_block",
    "record_tool_call",
    "record_turn",
    "render",
]
