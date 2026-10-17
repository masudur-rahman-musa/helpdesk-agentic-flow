"""One plain sentence for each error code: what a tool failure sounds like.

The reply is built from these words, never the exception text, which can
carry a host, a path or a tool name into the chat. Every code has its own
row, so a new code fails a test instead of reaching a user as a vague
"something went wrong".
"""

from collections.abc import Mapping
from types import MappingProxyType

from helpdesk.shared.core.errors import ErrorCode

PLAIN_ERRORS: Mapping[ErrorCode, str] = MappingProxyType(
    {
        ErrorCode.FORBIDDEN: (
            "You do not have permission to do that, so I did not go ahead. "
            "Your manager or an administrator can do it for you."
        ),
        ErrorCode.NOT_FOUND: (
            "I could not find what that refers to in your organisation. "
            "It may have been renamed or removed, so it is worth checking the name."
        ),
        ErrorCode.SOD_CONFLICT: (
            "That combination of access is not allowed to be held by one person, "
            "so it was blocked. Asking for a narrower role usually resolves it."
        ),
        ErrorCode.POLICY_BLOCKED: (
            "A policy rule blocked this, so nothing was changed. "
            "A person on the helpdesk team can review it with you if you think it is wrong."
        ),
        ErrorCode.IDEMPOTENCY_CONFLICT: (
            "This looks like a repeat of something already in progress, "
            "so I stopped rather than doing it twice. Check the original before asking again."
        ),
        ErrorCode.TOOL_DISABLED: (
            "That action is switched off at the moment, so nothing was changed. "
            "It is usually turned back on shortly."
        ),
        ErrorCode.VALIDATION: (
            "Some of the details were not in a form I could use, so I stopped. "
            "Saying it again with the system and the access you need should fix it."
        ),
        ErrorCode.INTERNAL: (
            "Something on our side went wrong and I could not finish that. "
            "Nothing was changed as far as I can tell, and trying again shortly may work."
        ),
    }
)
