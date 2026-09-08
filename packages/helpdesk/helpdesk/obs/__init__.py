"""Observability: the telemetry context and the JSON log formatter.

the design in one line: `tenant_id`, `session_id`, `trace_id` and
`principal_kind` may travel in telemetry; a user id, an email or a name may
not, and the `sessions` table is the only mapping between the two. A log line
is telemetry, so the redaction that makes that true is a property of the
formatter every line goes through, never advice to callers.

Metrics and the Langfuse trace with its per-node spans
join this package; nothing here reaches a network or a store.
"""

from helpdesk.obs.context import TelemetryContext, bind_context, current_context
from helpdesk.obs.logging import JsonFormatter, configure_logging, redact

__all__ = [
    "JsonFormatter",
    "TelemetryContext",
    "bind_context",
    "configure_logging",
    "current_context",
    "redact",
]
