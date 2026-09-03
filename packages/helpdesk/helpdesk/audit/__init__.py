"""Append-only hash-chained audit log (the design, an earlier change).

`chain` is the pure core; `writer` and `verify` are the thin I/O halves.
"""

from helpdesk.audit.chain import (
    GENESIS_HASH,
    VerifyResult,
    canonical_json,
    event_hash,
    verify_chain,
)
from helpdesk.audit.verify import verify_tenant_chain
from helpdesk.audit.writer import AuditEventInput, append_event

__all__ = [
    "GENESIS_HASH",
    "AuditEventInput",
    "VerifyResult",
    "append_event",
    "canonical_json",
    "event_hash",
    "verify_chain",
    "verify_tenant_chain",
]
