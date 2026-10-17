"""The kill-switch tools: stop a tool, and the board that shows them all.

The rules for a change live in the shared kill-switch service, so the
web admin gets the same checks.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from helpdesk.mcp_server.tools.base import MutatingToolInput
from helpdesk.mcp_server.tools.context import ToolContext
from helpdesk.shared.auth import rbac
from helpdesk.shared.services.killswitch import (
    GLOBAL,
    GLOBAL_SEGMENT,
    PROTECTED_TOOLS,
    KillSwitch,
    Scope,
    SwitchState,
    change_switch,
)
from helpdesk.shared.services.killswitch import (
    REASON_MAX_LENGTH as SWITCH_REASON_MAX_LENGTH,
)
from helpdesk.shared.services.killswitch import (
    TOOL_NAME_MAX_LENGTH as SWITCH_TOOL_NAME_MAX_LENGTH,
)
from helpdesk.shared.services.killswitch.switch import InvalidSwitchTargetError


class SetKillSwitchInput(MutatingToolInput):
    """Input for `set_kill_switch`.

    `scope` is required, so a forgotten argument cannot stop a tool for the
    whole platform. `enabled=True` stops the tool.
    """

    tool_name: str = Field(min_length=1, max_length=SWITCH_TOOL_NAME_MAX_LENGTH)
    """The tool to stop or release; must be a registered tool."""

    enabled: bool
    scope: Literal["global"] | uuid.UUID
    reason: str = Field(min_length=1, max_length=SWITCH_REASON_MAX_LENGTH)
    """Required both ways; on release, the audit event is its only record."""


class SetKillSwitchOutput(BaseModel):
    """What the switch now says, in the words the caller can act on."""

    tool_name: str
    stopped: bool
    """True when the tool is now off. Echoed so nobody reads `enabled`
backwards."""

    scope: str
    """`global`, or the tenant id the switch was written for."""

    changed_at: datetime


async def set_kill_switch(ctx: ToolContext, args: SetKillSwitchInput) -> SetKillSwitchOutput:
    """Stop a tool, or let it run again, for one tenant or the whole platform.

    A tenant scope must be the caller's own. The registry is imported inside
    the call because it imports this module.
    """
    from helpdesk.mcp_server.tools.registry import REGISTRY

    if args.tool_name not in REGISTRY:
        raise InvalidSwitchTargetError(f"no registered tool named {args.tool_name!r}")

    scope: Scope
    if args.scope == GLOBAL_SEGMENT:
        scope = GLOBAL
    elif args.scope == ctx.identity.tenant_id:
        scope = ctx.identity.tenant_id
    else:
        raise rbac.RbacDenied("a kill switch may only be scoped globally or to your own tenant")

    await change_switch(
        ctx.session,
        KillSwitch(ctx.clients.redis()),
        scope=scope,
        tool=args.tool_name,
        stopped=args.enabled,
        reason=args.reason,
        actor_ref=str(ctx.identity.user_id),
        session_id=ctx.identity.session_id,
        now=ctx.now,
    )
    return SetKillSwitchOutput(
        tool_name=args.tool_name,
        stopped=args.enabled,
        scope=GLOBAL_SEGMENT if scope is GLOBAL else str(scope),
        changed_at=ctx.now,
    )


class ListKillSwitchesInput(BaseModel):
    """No fields. Extras are refused, so a stray `tenant_id` is an error, not
    a silent read of the caller's own board."""

    model_config = ConfigDict(extra="forbid")


class KillSwitchRow(BaseModel):
    """One tool's line on the admin board. Both layers show, so clearing the
    tenant switch does not hide a global stop."""

    tool: str
    protected: bool
    """True when no switch may stop this tool. Read from the same set that
refuses the write."""

    stopped: bool
    """True when either layer holds the tool down, or its value is unreadable."""

    global_state: SwitchState | None
    tenant_state: SwitchState | None
    unreadable: bool
    """The stored value will not parse. Shown, since this board is where it
gets cleared."""


class ListKillSwitchesOutput(BaseModel):
    """The whole board: one row per registered tool, in the registry's order."""

    switches: list[KillSwitchRow]


async def list_kill_switches(
    ctx: ToolContext, args: ListKillSwitchesInput
) -> ListKillSwitchesOutput:
    """Every registered tool and the state of its two kill switches. Names
    come from the registry, so no tool is missing."""
    from helpdesk.mcp_server.tools.registry import REGISTRY

    tools = list(REGISTRY)
    states = await KillSwitch(ctx.clients.redis()).list_states(ctx.identity.tenant_id, tools)
    return ListKillSwitchesOutput(
        switches=[
            KillSwitchRow(
                tool=row.tool,
                protected=row.tool in PROTECTED_TOOLS,
                stopped=row.unreadable
                or row.global_state is not None
                or row.tenant_state is not None,
                global_state=row.global_state,
                tenant_state=row.tenant_state,
                unreadable=row.unreadable,
            )
            for row in states
        ]
    )


__all__ = [
    "KillSwitchRow",
    "ListKillSwitchesInput",
    "ListKillSwitchesOutput",
    "SetKillSwitchInput",
    "SetKillSwitchOutput",
    "list_kill_switches",
    "set_kill_switch",
]
