"""Which tenant and session a log line belongs to, per task.

A `ContextVar`, so two chat turns running at once never borrow each other's
tenant. No `user_id` field on purpose: every field here goes on every log line.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from pydantic import BaseModel, ConfigDict


class TelemetryContext(BaseModel):
    """Who this call is, minus the person. Fields are empty until a token is checked."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str | None = None
    session_id: str | None = None
    trace_id: str | None = None
    principal_kind: str | None = None


_CONTEXT: ContextVar[TelemetryContext] = ContextVar("helpdesk_telemetry_context")


def current_context() -> TelemetryContext:
    return _CONTEXT.get(TelemetryContext())


@contextmanager
def bind_context(
    *,
    tenant_id: str | None = None,
    session_id: str | None = None,
    trace_id: str | None = None,
    principal_kind: str | None = None,
) -> Iterator[TelemetryContext]:
    """Bind a fresh context for this block, then restore the old one.

    Replaces rather than merges, so an unset field reads as unknown, not the outer value.
    """
    context = TelemetryContext(
        tenant_id=tenant_id,
        session_id=session_id,
        trace_id=trace_id,
        principal_kind=principal_kind,
    )
    token = _CONTEXT.set(context)
    try:
        yield context
    finally:
        _CONTEXT.reset(token)
