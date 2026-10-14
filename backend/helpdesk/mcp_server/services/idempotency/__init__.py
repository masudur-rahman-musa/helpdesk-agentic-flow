"""Idempotency: one key, one execution (the design).

`fingerprint` says whether two requests are the same request; `run_once`
wraps a tool's effect so a retry replays the stored response instead of
running it again.
"""

from helpdesk.mcp_server.services.idempotency.fingerprint import fingerprint
from helpdesk.mcp_server.services.idempotency.guard import (
    KEY_TTL,
    InvalidIdempotencyKeyError,
    TenantMismatchError,
    UnstorableResponseError,
    run_once,
)

__all__ = [
    "KEY_TTL",
    "InvalidIdempotencyKeyError",
    "TenantMismatchError",
    "UnstorableResponseError",
    "fingerprint",
    "run_once",
]
