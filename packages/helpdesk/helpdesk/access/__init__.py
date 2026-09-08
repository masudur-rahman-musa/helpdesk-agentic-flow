"""Access domain: the directory port, the snapshot builder, and execution.

`directory.py` is the design's `DirectoryProvider` seam (read half from plan
11a, `apply_grant` from 11b). `snapshot.py` turns repository state into plan
08's `RequestSnapshot`. `requests.py` holds the access-request reads and
writes the tools consume. `execute.py` is the one place a grant is written,
with the design's re-check in front of it, and `revoke.py` the one
place access is taken away, over the whole live set for a (user, role).
`lifecycle.py` is the design's joiner and mover halves: `onboard_user` (plan
12a) creates the person and applies their department's template, and
`transfer_user` diffs the two templates and moves them, and
`offboard_user` marks a leaver, revokes everything they hold and
closes every request still open against them — all three through
`execute_grant` and `revoke_role_access`, never writing a grant themselves.
`rollback.py` is the design's undo at the REQUEST's unit: within
24 hours of the grant it ends every live row the request produced, through
that same one writer, and links each one back to the decision being taken
back.
`reviews.py` opens a periodic access review over a department or a
system and flags every live grant in it; it decides nothing about access, and
an earlier change's decide path is what turns a flag into a revocation.
`expiry.py` is the scheduler's sweep: the one writer of the
`expired` status, which is deliberately not the revocation path.
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
from helpdesk.access.directory import (
    DirectoryProvider,
    DirectoryReader,
    SimulatedDirectory,
)
from helpdesk.access.execute import (
    GRANT_EXECUTED_EVENT_TYPE,
    PolicyBlockedAtExecutionError,
    SodConflictAtExecutionError,
    execute_grant,
    reclassify,
)
from helpdesk.access.expiry import (
    EXPIRED_GRANT_STATUS,
    GRANT_EXPIRED_EVENT_TYPE,
    expire_grants,
)
from helpdesk.access.lifecycle import (
    JOINER_PRINCIPALS,
    JOINER_STATUS,
    LEAVER_STATUS,
    OFFBOARD_REVOKE_REASON,
    TRANSFER_REVOKE_REASON,
    UNUSABLE_PASSWORD_HASH,
    USER_OFFBOARDED_EVENT_TYPE,
    USER_ONBOARDED_EVENT_TYPE,
    USER_TRANSFERRED_EVENT_TYPE,
    AlreadyLeaverError,
    DuplicateEmailError,
    LeaverTransferError,
    OffboardResult,
    OnboardResult,
    SameDepartmentError,
    SkippedRole,
    TransferResult,
    offboard_user,
    onboard_user,
    transfer_user,
)
from helpdesk.access.requests import (
    REQUEST_REJECTED_EVENT_TYPE,
    append_request_rejected,
    approval_slots,
    create_access_request,
    list_requests_for_requester,
    lock_open_requests_for_target,
    resolved_approver_ids,
    set_request_status,
)
from helpdesk.access.reviews import (
    REVIEW_ITEM_DECIDED_EVENT_TYPE,
    REVIEW_OPENED_EVENT_TYPE,
    REVIEW_REVOKE_REASON,
    STALE_WINDOW,
    DecideItemResult,
    ItemAlreadyDecidedError,
    OpenReviewResult,
    ReviewAlreadyOpenError,
    ReviewScope,
    decide_item,
    find_item,
    find_review,
    list_items,
    open_review,
)
from helpdesk.access.revoke import (
    GRANT_REVOKED_EVENT_TYPE,
    REVOKED_GRANT_STATUS,
    GrantNotActiveError,
    revoke_grant_row,
    revoke_role_access,
)
from helpdesk.access.rollback import (
    GRANT_ROLLED_BACK_EVENT_TYPE,
    ROLLBACK_REVOKE_REASON,
    ROLLBACK_WINDOW,
    RequestNotExecutedError,
    RollbackResult,
    RollbackWindowExpiredError,
    rollback_request,
    within_rollback_window,
)
from helpdesk.access.snapshot import build_request_snapshot

__all__ = [
    "EXPIRED_GRANT_STATUS",
    "GRANT_EXECUTED_EVENT_TYPE",
    "GRANT_EXPIRED_EVENT_TYPE",
    "GRANT_REVOKED_EVENT_TYPE",
    "GRANT_ROLLED_BACK_EVENT_TYPE",
    "GROUP_SLOTS",
    "JOINER_PRINCIPALS",
    "JOINER_STATUS",
    "LEAVER_STATUS",
    "OFFBOARD_REVOKE_REASON",
    "REQUEST_REJECTED_EVENT_TYPE",
    "REVIEW_ITEM_DECIDED_EVENT_TYPE",
    "REVIEW_OPENED_EVENT_TYPE",
    "REVIEW_REVOKE_REASON",
    "REVOKED_GRANT_STATUS",
    "ROLLBACK_REVOKE_REASON",
    "ROLLBACK_WINDOW",
    "STALE_WINDOW",
    "TRANSFER_REVOKE_REASON",
    "UNUSABLE_PASSWORD_HASH",
    "USER_OFFBOARDED_EVENT_TYPE",
    "USER_ONBOARDED_EVENT_TYPE",
    "USER_TRANSFERRED_EVENT_TYPE",
    "AlreadyLeaverError",
    "DecideItemResult",
    "DirectoryProvider",
    "DirectoryReader",
    "DuplicateEmailError",
    "GrantNotActiveError",
    "ItemAlreadyDecidedError",
    "LeaverTransferError",
    "NotAnApproverError",
    "OffboardResult",
    "OnboardResult",
    "OpenReviewResult",
    "PolicyBlockedAtExecutionError",
    "RequestNotExecutedError",
    "RequestNotPendingError",
    "ReviewAlreadyOpenError",
    "ReviewScope",
    "RollbackResult",
    "RollbackWindowExpiredError",
    "SameDepartmentError",
    "SimulatedDirectory",
    "SkippedRole",
    "SlotDecision",
    "SlotState",
    "SodConflictAtExecutionError",
    "TransferResult",
    "append_request_rejected",
    "approval_slots",
    "build_request_snapshot",
    "chain_is_complete",
    "create_access_request",
    "decide_item",
    "decide_slot",
    "dump_slots",
    "execute_grant",
    "expire_grants",
    "find_item",
    "find_review",
    "first_fillable_index",
    "is_rejected",
    "list_items",
    "list_pending_for_approver",
    "list_requests_for_requester",
    "lock_open_requests_for_target",
    "lock_request",
    "may_fill",
    "offboard_user",
    "onboard_user",
    "open_review",
    "parse_slots",
    "reclassify",
    "request_in_scope",
    "resolved_approver_ids",
    "revoke_grant_row",
    "revoke_role_access",
    "rollback_request",
    "set_request_status",
    "transfer_user",
    "within_rollback_window",
]
