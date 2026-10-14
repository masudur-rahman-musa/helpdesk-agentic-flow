"""The kill-switch tools: the design's Admin group, the stop and its board.

`set_kill_switch` is the admin half of the design. Everything it DECIDES —
the protected-tool guard and the `killswitch.changed` record that cannot be
skipped on the way to the write — lives in `helpdesk.killswitch.change`, so
row 24's web admin reaches the same guards. What lives here is the wire
contract, the registry lookup that turns a client string into a tool this
platform actually has, the scope check against the token, and one clock read.

It has no scope helper: the design gives the row ONE cell, `platform_admin` at
`Scope.ANY`. The pipeline's matrix stage is the whole permission check, so
deleting it changes the answer for the six principals with no cell — pinned
by `test_mcp_set_kill_switch.py::test_the_six_denied_principals_change_no_switch`.

It reaches Redis through `mcp_server.redis_client`, the same per-loop pooled
client with the same pinned timeouts the pipeline reads the switch with.

`list_kill_switches` is the read half of the row above, and the whole of what
the design's `/admin/killswitch` page is drawn from: every registered tool, both
layers of its switch, in one `MGET`. It takes no arguments at all, so there is
nothing for a caller to choose and nothing to trust, and it
writes nothing — the same single `platform_admin` cell as the write, because a
board whose audience is wider than the write's would show six principals
switches none of them can change.

No function here opens a session or touches the transport: the pipeline binds
the session to the token's tenant before the handler is called.
"""

import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.mcp_server.core.identity import Identity
from helpdesk.mcp_server.redis_client import _redis
from helpdesk.shared.auth import rbac
from helpdesk.shared.core.settings import load_settings
from helpdesk.shared.killswitch import (
    GLOBAL,
    GLOBAL_SEGMENT,
    PROTECTED_TOOLS,
    KillSwitch,
    Scope,
    SwitchState,
    change_switch,
)
from helpdesk.shared.killswitch import (
    REASON_MAX_LENGTH as SWITCH_REASON_MAX_LENGTH,
)
from helpdesk.shared.killswitch import (
    TOOL_NAME_MAX_LENGTH as TOOL_NAME_MAX_LENGTH,  # re-exported for the tests
)
from helpdesk.shared.killswitch.switch import InvalidSwitchTargetError


class SetKillSwitchInput(BaseModel):
    """the design's signature, with `tenant_id?` read as a REQUIRED `scope`.

    Taking the optional field literally would make a forgotten argument trip
    the platform-wide stop, which is the most dangerous default in the system
    (decision A). So the scope is required and spelled out: the literal
    `"global"` for the platform, or a tenant id — which must be the token's,
    or the call is refused `FORBIDDEN`.

    `enabled` describes the KILL SWITCH, not the tool: `True` stops the tool
    (decision B). `SetKillSwitchOutput.stopped` echoes the outcome and the
    registry description says it in words, so neither an agent nor a person
    can get the polarity backwards from one surface alone.

    Not a `MutatingToolInput`: one SET or DELETE of one key is idempotent by
    nature, and the design omits the key for this tool alone (decision C).
    """

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(min_length=1, max_length=TOOL_NAME_MAX_LENGTH)
    """The wire name of the tool to stop or release, bounded against the kill
    switch's OWN constant, imported rather than redeclared:
    a name this model accepted and `key_for` refused would be a `VALIDATION`
    raised after the audit event rather than before it. Checked against the
    registry too, so a name no tool answers to never becomes a Redis key
    (`test_mcp_set_kill_switch.py::test_an_unregistered_tool_name_is_refused_before_any_key_is_written`).
    The 64/65 boundary is pinned by
    `::test_the_tool_name_length_boundary_is_inclusive_at_64`."""

    enabled: bool
    scope: Literal["global"] | uuid.UUID
    reason: str = Field(min_length=1, max_length=SWITCH_REASON_MAX_LENGTH)
    """Required in BOTH directions (decision H). Tripping stores it in the
    `SwitchState` every refused caller sees; clearing has nowhere to store it,
    so the `killswitch.changed` payload is the only record that turning a tool
    back on was a decision somebody made and explained. Bounded by
    `helpdesk.killswitch.REASON_MAX_LENGTH`, pinned inclusively at 1 and 500
    by `::test_the_reason_boundary_is_inclusive_at_1_and_500`."""


class SetKillSwitchOutput(BaseModel):
    """What the switch now says, in the words the caller can act on."""

    tool_name: str
    stopped: bool
    """True when the tool is now OFF. The echo decision B asks for: the input
    names the switch and this names the tool, so the two together cannot be
    read backwards."""

    scope: str
    """`global`, or the tenant id the switch was written for."""

    changed_at: datetime


async def set_kill_switch(
    session: AsyncSession, identity: Identity, args: SetKillSwitchInput
) -> SetKillSwitchOutput:
    """Stop a tool, or let it run again, for one tenant or the whole platform.

    Three checks run here and nowhere else, all before any effect:

    * the tool name is looked up in the REGISTRY, so a switch can only be set
      on a tool this platform actually serves. `VALIDATION`, not `NOT_FOUND`:
      the argument is malformed for this tool, and a name outside the registry
      is not a row a caller could have reached.
    * a `scope` naming a tenant other than the token's is refused `FORBIDDEN`
      and recorded `tool.denied` — `Scope.ANY` in the design means every subject
      in the caller's own tenant, never every tenant.
    * `"global"` is turned into the `GLOBAL` sentinel here, so the string from
      the request body never reaches `key_for` as a scope.

    Everything else — the protected-tool guard, the event, the ordering that
    makes a Redis failure leave no record behind — is
    `helpdesk.killswitch.change_switch`, beside the write it guards.

    The registry import is deferred to the call: `registry` imports this
    module, so a module-level import would be a cycle (decision F).

    `now` is read once and used for the event and the answer alike.
    """
    from helpdesk.mcp_server.tools.registry import REGISTRY

    if args.tool_name not in REGISTRY:
        raise InvalidSwitchTargetError(f"no registered tool named {args.tool_name!r}")

    scope: Scope
    if args.scope == GLOBAL_SEGMENT:
        scope = GLOBAL
    elif args.scope == identity.tenant_id:
        scope = identity.tenant_id
    else:
        raise rbac.RbacDenied("a kill switch may only be scoped globally or to your own tenant")

    now = datetime.now(UTC)
    await change_switch(
        session,
        KillSwitch(_redis(load_settings())),
        scope=scope,
        tool=args.tool_name,
        stopped=args.enabled,
        reason=args.reason,
        actor_ref=str(identity.user_id),
        session_id=identity.session_id,
        now=now,
    )
    return SetKillSwitchOutput(
        tool_name=args.tool_name,
        stopped=args.enabled,
        scope=GLOBAL_SEGMENT if scope is GLOBAL else str(scope),
        changed_at=now,
    )


class ListKillSwitchesInput(BaseModel):
    """No fields at all, and nothing for a caller to choose.

    The tenant is the token's and the tool list is the server's own registry, so there is no argument to trust and none to validate.
    `extra="forbid"` rather than `ignore`: a caller who sends a `tenant_id`
    believing they are reading another tenant's board must be refused, not
    quietly answered with their own. Pinned as an EXACTLY empty field set by
    `test_mcp_list_kill_switches.py::test_the_tool_takes_no_arguments_at_all`,
    which no later field can ride along into.
    """

    model_config = ConfigDict(extra="forbid")


class KillSwitchRow(BaseModel):
    """One tool's line on the admin board.

    Both layers, never one effective state (decision B): an admin clearing
    the tenant switch has to be told the global one still holds the tool
    down. `stopped` is carried beside them so row 24d renders a line without
    re-implementing the precedence, and `protected` so its toggle is disabled
    from the row rather than from a second copy of a security-relevant list
    (decision G). The exact six keys are pinned by
    `test_mcp_list_kill_switches.py::test_the_board_answers_the_envelope_and_an_exact_six_key_row`.
    """

    tool: str
    protected: bool
    """True when no switch may stop this tool at all: read off
    `helpdesk.killswitch.PROTECTED_TOOLS`, the frozenset that actually
    refuses the write, never retyped here. Pinned by
    `::test_protected_is_true_for_the_one_tool_no_switch_may_stop`."""

    stopped: bool
    """True when EITHER layer holds the tool down, or when its stored value
    could not be read. Never derived from the tenant layer alone: the board
    and `KillSwitch.check` must agree about what is running, pinned together
    in `::test_a_stopped_tool_reads_stopped_on_the_layer_it_was_stopped_on`,
    which calls the stopped tool and asserts the refusal."""

    global_state: SwitchState | None
    tenant_state: SwitchState | None
    unreadable: bool
    """The stored value is present but will not parse. Reported rather than
    raised (decision D), because this board is the only surface from which a
    bad key can be seen and cleared, and always alongside `stopped` -- a row
    that read "running" while `check` denied every call would be the one
    answer an operator cannot act on
    (`::test_an_unreadable_value_marks_its_row_and_the_board_still_answers`)."""


class ListKillSwitchesOutput(BaseModel):
    """The whole board: one row per registered tool, in the registry's order."""

    switches: list[KillSwitchRow]


async def list_kill_switches(
    session: AsyncSession, identity: Identity, args: ListKillSwitchesInput
) -> ListKillSwitchesOutput:
    """Every registered tool and the state of its two kill switches.

    The read half of `set_kill_switch`, and what the design's
    `/admin/killswitch` page is drawn from: `KillSwitch.status` answers for
    one tool at a time and `check` answers by raising, so before this there
    was no way to ask which tools are currently stopped.

    The tool names come from the REGISTRY, so every registered tool has a
    line and a tool added by a later plan cannot be invisible on the page
    (`test_mcp_list_kill_switches.py::test_the_listed_names_are_exactly_the_registry_in_its_own_order`).
    Not from a Redis `SCAN`, which shows only the keys that exist and could
    never say "this tool is running", and not from the `rbac.Tool` members,
    which are a different vocabulary from the one `check` is called with
    (decision A). The import is deferred to the call for the same reason
    `set_kill_switch`'s is: `registry` imports this module.

    The whole board is ONE `MGET` inside `list_states` (decision C), against
    the token's tenant and the global scope alone — another tenant's switch
    appears nowhere here (a project rule,
    `::test_another_tenants_switch_is_invisible_to_this_caller`).

    No scope helper and no second permission check: the design gives the row one
    cell, so the pipeline's matrix stage IS the whole check and deleting it
    hands the board to the six principals with no cell, pinned by
    `::test_the_six_denied_principals_read_no_board` (a project rule: there
    is no second copy of that guard to hide behind).

    Nothing is written, so the pipeline's generic `tool.succeeded` is the
    whole record a read of the board leaves
    (`::test_one_call_writes_exactly_one_event_naming_the_tool_and_the_caller`).
    The session is unused: this tool reads Redis and nothing else.
    """
    from helpdesk.mcp_server.tools.registry import REGISTRY

    tools = list(REGISTRY)
    states = await KillSwitch(_redis(load_settings())).list_states(identity.tenant_id, tools)
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
    "TOOL_NAME_MAX_LENGTH",
    "KillSwitchRow",
    "ListKillSwitchesInput",
    "ListKillSwitchesOutput",
    "SetKillSwitchInput",
    "SetKillSwitchOutput",
    "list_kill_switches",
    "set_kill_switch",
]
