"""Kill switches: an admin can turn any tool off, per tenant or platform-wide.

`check` is the one call every tool makes before it runs (the design);
`trip`, `clear` and `status` are what the admin surface drives. `switch.py`
is I/O only; `change.py` is the layer above it that decides — the protected
tool guard and the `killswitch.changed` record, which no caller can skip on
the way to a write.
"""

from helpdesk.killswitch.change import (
    KILLSWITCH_CHANGED_EVENT_TYPE,
    PROTECTED_TOOLS,
    ProtectedToolError,
    change_switch,
)
from helpdesk.killswitch.switch import (
    ACTOR_MAX_LENGTH,
    GLOBAL,
    GLOBAL_SEGMENT,
    KEY_PREFIX,
    REASON_MAX_LENGTH,
    TOOL_NAME_MAX_LENGTH,
    TOOL_NAME_PATTERN,
    InvalidSwitchTargetError,
    KillSwitch,
    Scope,
    SwitchState,
    SwitchUnavailableError,
    ToolSwitchStates,
    key_for,
)

__all__ = [
    "ACTOR_MAX_LENGTH",
    "GLOBAL",
    "GLOBAL_SEGMENT",
    "KEY_PREFIX",
    "KILLSWITCH_CHANGED_EVENT_TYPE",
    "PROTECTED_TOOLS",
    "REASON_MAX_LENGTH",
    "TOOL_NAME_MAX_LENGTH",
    "TOOL_NAME_PATTERN",
    "InvalidSwitchTargetError",
    "KillSwitch",
    "ProtectedToolError",
    "Scope",
    "SwitchState",
    "SwitchUnavailableError",
    "ToolSwitchStates",
    "change_switch",
    "key_for",
]
