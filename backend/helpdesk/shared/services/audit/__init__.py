"""Append-only hash-chained audit log (the design, an earlier change).

`chain` is the pure core; `writer`, `verify`, `search` and `anchor` are the
thin I/O halves. Reading the log back under a filter lives here rather than in
a service, so every statement against `audit_events` and `audit_chain_anchors`
is in one package.

The anchor is what the chain cannot hold about itself: a head remembered
outside the walk, so a log shortened at the end or re-chained from genesis
stops verifying. `retention` is the other end of that record: an
anchor past the window goes, and the tenant's head never does.
"""

from helpdesk.shared.audit.anchor import (
    AnchorRegressionError,
    anchor_tenant_chain,
    latest_chain_anchor,
    write_chain_anchor,
)
from helpdesk.shared.audit.retention import (
    ANCHOR_RETENTION_DAYS,
    PURGE_LOG_MESSAGE,
    anchor_retention_cutoff,
    purge_chain_anchors,
)
from helpdesk.shared.services.audit.chain import (
    ANCHOR_HASH_PATTERN,
    GENESIS_HASH,
    MIN_ANCHOR_SEQ,
    ChainAnchor,
    InvalidChainAnchorError,
    VerifyResult,
    canonical_json,
    event_hash,
    verify_chain,
)
from helpdesk.shared.services.audit.search import (
    SCAN_CAP,
    AuditSearchPage,
    InvalidTimeRangeError,
    search_audit_events,
)
from helpdesk.shared.services.audit.verify import verify_tenant_chain
from helpdesk.shared.services.audit.writer import AuditEventInput, append_event

__all__ = [
    "ANCHOR_HASH_PATTERN",
    "ANCHOR_RETENTION_DAYS",
    "GENESIS_HASH",
    "MIN_ANCHOR_SEQ",
    "PURGE_LOG_MESSAGE",
    "SCAN_CAP",
    "AnchorRegressionError",
    "AuditEventInput",
    "AuditSearchPage",
    "ChainAnchor",
    "InvalidChainAnchorError",
    "InvalidTimeRangeError",
    "VerifyResult",
    "anchor_retention_cutoff",
    "anchor_tenant_chain",
    "append_event",
    "canonical_json",
    "event_hash",
    "latest_chain_anchor",
    "purge_chain_anchors",
    "search_audit_events",
    "verify_chain",
    "verify_tenant_chain",
    "write_chain_anchor",
]
