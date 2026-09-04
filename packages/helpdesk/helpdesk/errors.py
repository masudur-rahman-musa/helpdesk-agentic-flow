"""The typed error codes that cross a tool boundary (the design).

Pure: no I/O, no models, no services. Every package that raises across the
MCP boundary raises a `ToolError` subclass, so the boundary maps a closed
set of codes to user-facing language and never forwards free text.

The code is a class attribute, not a constructor argument: an error type
and its code are one decision, made where the class is declared, and a
caller cannot construct the same error under a different code.
"""

from enum import StrEnum


class ErrorCode(StrEnum):
    """Exactly the eight the design codes; the value is the wire string."""

    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    SOD_CONFLICT = "SOD_CONFLICT"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    TOOL_DISABLED = "TOOL_DISABLED"
    VALIDATION = "VALIDATION"
    INTERNAL = "INTERNAL"


class ToolError(Exception):
    """Base for every typed error a tool may return.

    Construction stays single-argument, matching the errors that predate
    this module: the message is detail for logs and the audit trail, never
    the thing shown to a user.
    """

    code: ErrorCode = ErrorCode.INTERNAL


class IdempotencyConflictError(ToolError):
    """The same key was reused for a different request (the design)."""

    code = ErrorCode.IDEMPOTENCY_CONFLICT
