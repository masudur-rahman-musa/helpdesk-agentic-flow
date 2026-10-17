"""Kill switches: an admin can turn any tool off, per tenant or for everyone.

Every tool calls `check` before it runs. `switch.py` only stores and reads;
`change.py` holds the guard and the audit record so no caller can skip them.
"""

from helpdesk.shared.services.killswitch.change import (
    KILLSWITCH_CHANGED_EVENT_TYPE,
    PROTECTED_TOOLS,
    ProtectedToolError,
    change_switch,
)
from helpdesk.shared.services.killswitch.switch import (
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
