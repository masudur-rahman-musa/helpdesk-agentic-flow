"""Changing a kill switch: the guard, the record, and only then the write.

`switch.py` is I/O only — it stores and reads a switch and decides nothing.
This module is the layer above it that DECIDES, and it exists so the two
cannot be had separately. Row 24's web admin is a
second caller, and a guard that lived in the MCP handler would let that
caller reach `KillSwitch.trip` without it.

Three things happen here, in this order, and the order is the point
(decision E):

1. the protected-tool guard, so nothing at all happens for a call that is
   refused;
2. the `killswitch.changed` audit event, appended into the CALLER's
   transaction and not yet committed;
3. the Redis `SET` or `DELETE`.

Redis is not in that transaction, so one of the two records can land alone.
Appending first means a Redis failure raises and takes the event back with
the rollback, which errs toward "no event, no change" rather than "changed,
no record". The residual window is the commit itself: a commit that fails
after a successful Redis write leaves the change recorded only as
`tool.failed`. That is stated rather than fixed — the alternative is a
two-phase protocol over a store with no transactions.

The event's SUBJECT is the TARGET tool, never `set_kill_switch`: an auditor
asking "when was `revoke_access` stopped, and by whom" reads the subject, and
an event naming the admin tool answers that for nobody.
Pinned by
`test_mcp_set_kill_switch.py::test_the_event_names_the_target_tool_as_its_subject`.

Every value that shapes a key or a stored string is checked BEFORE the append: `key_for` validates the scope and the tool name, and the
reason is bounded against `REASON_MAX_LENGTH` here rather than only inside
`SwitchState`, because a clear stores no state and would otherwise take an
unbounded reason straight into the audit payload.
"""

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.services.audit import AuditEventInput, append_event
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
"""the design's event for an admin turning a tool off or back on."""

PROTECTED_TOOLS = frozenset({"set_kill_switch"})
"""Tools no kill switch may ever stop (decision G).

`set_kill_switch` is the only tool that can undo a switch, so stopping it
locks the platform out of its own recovery and leaves a manual `DEL` against
production Redis as the only way back. Pinned by
`test_mcp_set_kill_switch.py::test_setting_the_kill_switch_on_set_kill_switch_is_policy_blocked`."""

_ACTOR_KIND = "user"
_SUBJECT_TYPE = "tool"


class ProtectedToolError(ToolError):
    """The target is a tool that must stay reachable (decision G).

    `POLICY_BLOCKED`, never `FORBIDDEN`: the caller holds the permission and
    the platform refuses the action, which is a different fact and a
    different line in the audit log.
    """

    code = ErrorCode.POLICY_BLOCKED


def _scope_segment(scope: Scope) -> str:
    """How the scope is spelled in the audit payload: a uuid or `global`."""
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
    """Stop `tool` for `scope`, or let it run again; record either way.

    `stopped` follows decision B: True trips the switch and the tool is off,
    False clears it and the tool runs again. Clearing a switch that was never
    set is a no-op that still writes the event — an admin's action is
    recorded whether or not it changed anything, and "I turned it back on"
    is the sentence an auditor needs even when nothing was on.

    `actor_ref` is a user id, never an email or a display name: `check`
    copies `SwitchState.actor` into the `TOOL_DISABLED` message every caller
    of the stopped tool sees (decision I), and the same value goes on the
    event so the two records agree.

    Returns the stored `SwitchState` for a trip and `None` for a clear.

    Raises `ProtectedToolError` for a `PROTECTED_TOOLS` target,
    `InvalidSwitchTargetError` for a scope, tool name or reason this cannot
    store, and `SwitchUnavailableError` when Redis cannot be reached — the
    last of these after the event was appended, so the caller's rollback
    takes it back.
    """
    if tool in PROTECTED_TOOLS:
        raise ProtectedToolError(f"{tool} may not be stopped by a kill switch")
    # Validates the scope's type and the tool name's shape and length with no
    # I/O, so a malformed target writes neither an event nor a key.
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
    """the design's `killswitch.changed`, subject = the tool that was changed.

    `stopped` is on the payload as well as implied by the reason, so a reader
    of one event never has to know decision B's polarity to know whether the
    tool ended up on or off. Subject and the exact payload key set pinned by
    `test_mcp_set_kill_switch.py::test_the_event_names_the_target_tool_as_its_subject`.
    """
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
