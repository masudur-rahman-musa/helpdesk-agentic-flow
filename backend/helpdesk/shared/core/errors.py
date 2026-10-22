"""The typed error codes a tool may return. Free text never crosses to the user.

The code is a class attribute, so a caller cannot raise an error under the wrong code.
"""

from enum import StrEnum


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


class ToolError(Exception):
    """Base for every typed tool error. The message is for logs and audit, never the user."""

    code: ErrorCode = ErrorCode.INTERNAL


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
