"""Access domain: the directory port, the snapshot builder, and execution.

`directory.py` is the design's `DirectoryProvider` seam (read half from plan
11a, `apply_grant` from 11b). `snapshot.py` turns repository state into plan
08's `RequestSnapshot`. `requests.py` holds the access-request reads and
writes the tools consume. `execute.py` is the one place a grant is written,
with the design's re-check in front of it, and `revoke.py` the one
place access is taken away, over the whole live set for a (user, role).
`approvals.py` is the
approval slot state machine — who may fill which seat, when a chain is
finished — and the scoped queue of what is waiting for one approver.
"""

from helpdesk.access.approvals import (
    GROUP_SLOTS,
    NotAnApproverError,
    RequestNotPendingError,
    SlotDecision,
    SlotState,
    chain_is_complete,
    decide_slot,
    dump_slots,
    first_fillable_index,
    is_rejected,
    list_pending_for_approver,
    lock_request,
    may_fill,
    parse_slots,
    request_in_scope,
)
from helpdesk.access.directory import DirectoryProvider, SimulatedDirectory
from helpdesk.access.execute import (
    GRANT_EXECUTED_EVENT_TYPE,
    PolicyBlockedAtExecutionError,
    SodConflictAtExecutionError,
    execute_grant,
)
from helpdesk.access.requests import (
    approval_slots,
    create_access_request,
    list_requests_for_requester,
    resolved_approver_ids,
    set_request_status,
)
from helpdesk.access.revoke import (
    GRANT_REVOKED_EVENT_TYPE,
    REVOKED_GRANT_STATUS,
    GrantNotActiveError,
    revoke_role_access,
)
from helpdesk.access.snapshot import build_request_snapshot

__all__ = [
    "GRANT_EXECUTED_EVENT_TYPE",
    "GRANT_REVOKED_EVENT_TYPE",
    "GROUP_SLOTS",
    "REVOKED_GRANT_STATUS",
    "DirectoryProvider",
    "GrantNotActiveError",
    "NotAnApproverError",
    "PolicyBlockedAtExecutionError",
    "RequestNotPendingError",
    "SimulatedDirectory",
    "SlotDecision",
    "SlotState",
    "SodConflictAtExecutionError",
    "approval_slots",
    "build_request_snapshot",
    "chain_is_complete",
    "create_access_request",
    "decide_slot",
    "dump_slots",
    "execute_grant",
    "first_fillable_index",
    "is_rejected",
    "list_pending_for_approver",
    "list_requests_for_requester",
    "lock_request",
    "may_fill",
    "parse_slots",
    "request_in_scope",
    "resolved_approver_ids",
    "revoke_role_access",
    "set_request_status",
]
