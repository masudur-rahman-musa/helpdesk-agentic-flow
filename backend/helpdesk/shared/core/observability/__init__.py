"""Observability: the telemetry context, the JSON log formatter, the metrics.

the design in one line: `tenant_id`, `session_id`, `trace_id` and
`principal_kind` may travel in telemetry; a user id, an email or a name may
not, and the `sessions` table is the only mapping between the two. A log line
is telemetry, so the redaction that makes that true is a property of the
formatter every line goes through, never advice to callers.

`metrics` holds the same idea for numbers: the design's in-process counters,
whose label values are closed enums, tool names and tenant ids — never a
person and never client text.

`tracing` is the third member: one Langfuse trace per chat turn,
one span per graph node, and the same rule about what may travel. It is the
only module here that CAN reach a network, and it does so from a background
exporter the facade never awaits — a trace is a record of a turn, never a
condition of one, so an unconfigured or broken client is invisible to every
caller.
"""

from helpdesk.shared.core.observability.context import (
    TelemetryContext,
    bind_context,
    current_context,
)
from helpdesk.shared.core.observability.logging import JsonFormatter, configure_logging, redact
from helpdesk.shared.core.observability.metrics import METRICS, Metrics, build_metrics, render

__all__ = [
    "METRICS",
    "JsonFormatter",
    "Metrics",
    "TelemetryContext",
    "bind_context",
    "build_metrics",
    "configure_logging",
    "current_context",
    "redact",
    "render",
]
