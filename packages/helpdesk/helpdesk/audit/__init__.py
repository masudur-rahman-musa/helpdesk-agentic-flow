"""Append-only hash-chained audit log (the design, an earlier change).

`chain` is the pure core; `writer`, `verify` and `search` are the thin I/O
halves. Reading the log back under a filter lives here rather than in a
service, so every statement against `audit_events` is in one package.
"""

from helpdesk.audit.chain import (
    GENESIS_HASH,
    VerifyResult,
    canonical_json,
    event_hash,
    verify_chain,
)
from helpdesk.audit.search import (
    SCAN_CAP,
    AuditSearchPage,
    InvalidTimeRangeError,
    search_audit_events,
)
from helpdesk.audit.verify import verify_tenant_chain
from helpdesk.audit.writer import AuditEventInput, append_event

__all__ = [
    "GENESIS_HASH",
    "SCAN_CAP",
    "AuditEventInput",
    "AuditSearchPage",
    "InvalidTimeRangeError",
    "VerifyResult",
    "append_event",
    "canonical_json",
    "event_hash",
    "search_audit_events",
    "verify_chain",
    "verify_tenant_chain",
]
