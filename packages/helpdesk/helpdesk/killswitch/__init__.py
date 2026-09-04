"""Kill switches: an admin can turn any tool off, per tenant or platform-wide.

`check` is the one call every tool makes before it runs (the design);
`trip`, `clear` and `status` are what the admin surface drives.
"""

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
    key_for,
)

__all__ = [
    "ACTOR_MAX_LENGTH",
    "GLOBAL",
    "GLOBAL_SEGMENT",
    "KEY_PREFIX",
    "REASON_MAX_LENGTH",
    "TOOL_NAME_MAX_LENGTH",
    "TOOL_NAME_PATTERN",
    "InvalidSwitchTargetError",
    "KillSwitch",
    "Scope",
    "SwitchState",
    "SwitchUnavailableError",
    "key_for",
]
