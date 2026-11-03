"""The typed error codes a tool may return. Free text never crosses to the user.

The code is a class attribute, so a caller cannot raise an error under the wrong code.

Each class also carries one fixed `reason` sentence, written with the class, so
the model can say why a call was turned down. It is never built from the
error's message, which may hold row data.
"""

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType


class ErrorCode(StrEnum):
    """The closed set of codes; the value is the wire string."""

    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    SOD_CONFLICT = "SOD_CONFLICT"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    REQUEST_NOT_PENDING = "REQUEST_NOT_PENDING"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    TOOL_DISABLED = "TOOL_DISABLED"
    VALIDATION = "VALIDATION"
    INTERNAL = "INTERNAL"


CODE_REASONS: Mapping[ErrorCode, str] = MappingProxyType(
    {
        ErrorCode.FORBIDDEN: "This person's role may not do this.",
        ErrorCode.NOT_FOUND: "Nothing with that id was found in this company.",
        ErrorCode.SOD_CONFLICT: (
            "Segregation of duties forbids it: this would join two roles one person "
            "may not hold together."
        ),
        ErrorCode.POLICY_BLOCKED: "A company policy rule blocks this.",
        ErrorCode.REQUEST_NOT_PENDING: "The request is already decided, so it is no longer open.",
        ErrorCode.IDEMPOTENCY_CONFLICT: "The same change key was used for a different change.",
        ErrorCode.TOOL_DISABLED: "An admin has turned this tool off for now.",
        ErrorCode.VALIDATION: "The tool did not accept these details.",
        ErrorCode.INTERNAL: "Something went wrong on the help desk's side.",
    }
)
"""The sentence a class without its own reason carries, one per code."""


class ToolError(Exception):
    """Base for every typed tool error. The message is for logs and audit, never the user.

    `reason` is the class's fixed sentence; a class that sets none gets its
    code's sentence from `CODE_REASONS`."""

    code: ErrorCode = ErrorCode.INTERNAL
    reason: str = CODE_REASONS[ErrorCode.INTERNAL]

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if "reason" not in cls.__dict__:
            cls.reason = CODE_REASONS[cls.code]


class NotFoundError(ToolError):
    """Not in the caller's tenant.

    Same answer whether the row is another tenant's or missing, so it leaks nothing.
    """

    code = ErrorCode.NOT_FOUND


class IdempotencyConflictError(ToolError):
    """The same key was reused for a different request."""

    code = ErrorCode.IDEMPOTENCY_CONFLICT


class ToolDisabledError(ToolError):
    """An admin turned this tool off.

    Not `FORBIDDEN`, so the audit trail can tell a shutdown from a denial.
    """

    code = ErrorCode.TOOL_DISABLED
