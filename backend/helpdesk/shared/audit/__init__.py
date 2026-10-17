"""The audit log: append, search, anchor and check the chain, remove old anchors."""

from helpdesk.shared.audit.anchor import (
    ANCHOR_LOG_MESSAGE,
    AnchorRegressionError,
    anchor_tenant_chain,
    latest_chain_anchor,
    write_chain_anchor,
)
from helpdesk.shared.audit.chain import (
    ANCHOR_HASH_PATTERN,
    GENESIS_HASH,
    MIN_ANCHOR_SEQ,
    ChainAnchor,
    InvalidChainAnchorError,
    VerifyResult,
    canonical_json,
    event_hash,
    hashed_fields,
    verify_chain,
)
from helpdesk.shared.audit.reader import as_chain_event, verify_tenant_chain
from helpdesk.shared.audit.retention import purge_chain_anchors
from helpdesk.shared.audit.search import (
    SCAN_CAP,
    AuditSearchPage,
    InvalidTimeRangeError,
    search_audit_events,
)
from helpdesk.shared.audit.writer import AuditEventInput, TenantMismatchError, append_event

__all__ = [
    "ANCHOR_HASH_PATTERN",
    "ANCHOR_LOG_MESSAGE",
    "GENESIS_HASH",
    "MIN_ANCHOR_SEQ",
    "SCAN_CAP",
    "AnchorRegressionError",
    "AuditEventInput",
    "AuditSearchPage",
    "ChainAnchor",
    "InvalidChainAnchorError",
    "InvalidTimeRangeError",
    "TenantMismatchError",
    "VerifyResult",
    "anchor_tenant_chain",
    "append_event",
    "as_chain_event",
    "canonical_json",
    "event_hash",
    "hashed_fields",
    "latest_chain_anchor",
    "purge_chain_anchors",
    "search_audit_events",
    "verify_chain",
    "verify_tenant_chain",
    "write_chain_anchor",
]
