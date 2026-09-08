"""Which tenant and session a log line belongs to, per task.

One `ContextVar`, bound at the two entry points that know who is calling: the
agent-api middleware and `pipeline.dispatch`. A
`ContextVar` rather than a module global because it is per-task by
construction, and two concurrent chat turns in one process must not borrow
each other's tenant — pinned by
`test_obs_context.py::test_two_concurrent_tasks_do_not_see_each_other_s_context`,
which fails for a module global and passes for nothing else.

Only the four fields the design permits in telemetry live here. There is
deliberately no `user_id` field: the context is what the formatter copies onto
every line, so a field added here would be published on every line, and the
`sessions` table is the only place a session id may be resolved to a person.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from pydantic import BaseModel, ConfigDict


class TelemetryContext(BaseModel):
    """The telemetry-safe part of "who is this call", every field optional.

    Frozen: a bound context is a fact about the call, and a later layer
    editing it in place would rewrite the attribution of lines already
    emitted. Rebinding is `bind_context`, which restores on exit.

    Every field is optional because the honest answer before a token is
    verified is "not known yet" — a startup line, or a request refused at the
    transport, carries no tenant, and inventing one would be worse than the
    gap.
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: str | None = None
    session_id: str | None = None
    trace_id: str | None = None
    principal_kind: str | None = None


_CONTEXT: ContextVar[TelemetryContext] = ContextVar("helpdesk_telemetry_context")


def current_context() -> TelemetryContext:
    """The context bound in this task, or an empty one outside any bind."""
    return _CONTEXT.get(TelemetryContext())


@contextmanager
def bind_context(
    *,
    tenant_id: str | None = None,
    session_id: str | None = None,
    trace_id: str | None = None,
    principal_kind: str | None = None,
) -> Iterator[TelemetryContext]:
    """Bind a fresh context for this block, and restore the previous one.

    The bind REPLACES rather than merges: a nested bind names the whole
    context of its block, so an unset field reads as unknown instead of
    inheriting the enclosing call's value and attributing a line to it.

    Restoration is by the `ContextVar` token, not by re-setting the old value:
    a token is what makes the restore exact when binds nest, pinned by
    `test_obs_context.py::test_the_context_is_restored_on_exit`.
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
