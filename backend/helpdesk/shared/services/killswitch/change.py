"""Changing a kill switch: the guard, the audit record, then the write.

This lives above the storage layer so every caller (the MCP tool and the web
admin) gets the guard and the record too. The order matters:

1. the protected-tool guard, so a refused call does nothing at all;
2. the `killswitch.changed` event, added to the caller's open transaction;
3. the Redis `SET` or `DELETE`.

Redis is not in the transaction. Writing the event first means a Redis error
rolls the event back, so we lean toward "no event, no change" over "changed,
no record". The one gap left is a failed commit after a good Redis write.

The event's subject is the target tool, not `set_kill_switch`, so an auditor
can ask when a given tool was stopped and by whom.
"""

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.audit import AuditEventInput, append_event
from helpdesk.shared.services.killswitch.switch import (
    GLOBAL_SEGMENT,
    REASON_MAX_LENGTH,
    InvalidSwitchTargetError,
    KillSwitch,
    Scope,
    SwitchState,
    _GlobalScope,
    key_for,
)

KILLSWITCH_CHANGED_EVENT_TYPE = "killswitch.changed"
"""Audit event for an admin turning a tool off or back on."""

PROTECTED_TOOLS = frozenset({"set_kill_switch"})
"""Tools no kill switch may stop. `set_kill_switch` is the only way to undo
a switch, so stopping it would lock admins out of their own fix."""

_ACTOR_KIND = "user"
_SUBJECT_TYPE = "tool"


class ProtectedToolError(ToolError):
    """The target must stay reachable. `POLICY_BLOCKED`, not `FORBIDDEN`:
    the caller has the permission; the platform refuses the action."""

    code = ErrorCode.POLICY_BLOCKED


def _scope_segment(scope: Scope) -> str:
    return GLOBAL_SEGMENT if isinstance(scope, _GlobalScope) else str(scope)


async def change_switch(
    session: AsyncSession,
    switch: KillSwitch,
    *,
    scope: Scope,
    tool: str,
    stopped: bool,
    reason: str,
    actor_ref: str,
    session_id: uuid.UUID | None,
    now: datetime,
) -> SwitchState | None:
    """Stop `tool` for `scope` (`stopped=True`) or let it run again; record either way.

    Clearing a switch that was never set still writes the event: the admin's
    action is recorded even if nothing changed. `actor_ref` is a user id,
    never an email, because it is shown to every caller of the stopped tool.
    """
    if tool in PROTECTED_TOOLS:
        raise ProtectedToolError(f"{tool} may not be stopped by a kill switch")
    # Checks scope and tool name with no I/O, so a bad target writes nothing.
    key_for(scope, tool)
    if len(reason) > REASON_MAX_LENGTH:
        raise InvalidSwitchTargetError(f"reason is longer than {REASON_MAX_LENGTH} characters")

    await _append_changed(
        session,
        scope=scope,
        tool=tool,
        stopped=stopped,
        reason=reason,
        actor_ref=actor_ref,
        session_id=session_id,
        now=now,
    )

    if stopped:
        return await switch.trip(scope, tool, reason, actor_ref)
    await switch.clear(scope, tool)
    return None


async def _append_changed(
    session: AsyncSession,
    *,
    scope: Scope,
    tool: str,
    stopped: bool,
    reason: str,
    actor_ref: str,
    session_id: uuid.UUID | None,
    now: datetime,
) -> None:
    """Append `killswitch.changed` with the changed tool as its subject."""
    await append_event(
        session,
        bound_tenant_id(session),
        AuditEventInput(
            occurred_at=now,
            actor_kind=_ACTOR_KIND,
            actor_ref=actor_ref,
            session_id=session_id,
            trace_id=None,
            event_type=KILLSWITCH_CHANGED_EVENT_TYPE,
            subject_type=_SUBJECT_TYPE,
            subject_id=tool,
            payload={
                "tool": tool,
                "scope": _scope_segment(scope),
                "stopped": stopped,
                "reason": reason,
                "actor": actor_ref,
            },
        ),
    )
