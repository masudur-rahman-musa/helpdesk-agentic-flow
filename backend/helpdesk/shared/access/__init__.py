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
`expiry.py` holds the scheduler's two expiry jobs: `expire_grants`,
the one writer of the `expired` status, which is deliberately not the
revocation path, and `warn_expiring_grants`, which tells a holder a
fortnight before that happens and writes nothing but a notification.
`leavers.py` is the third daily job: it answers who is overdue to
leave and hands each of them to `offboard_user` as the `system` actor, deciding
nothing else itself.
`review_schedule.py` is the last pair: `generate_reviews` answers
which scopes are due a review this quarter and hands each to `open_review` as
that same `system` actor, and `send_review_reminders` tells a late reviewer and,
after a fortnight, every `it_admin`. Neither decides anything about access.
`rules.py` is the read side of the design's `/admin/rules`: the
tenant's separation-of-duties rules with both sides resolved to role and
system names, in three statements and one total order. It judges nothing;
`snapshot.py` keeps the narrow shape the policy engine decides on.
`approvals.py` is the
approval slot state machine — who may fill which seat, when a chain is
finished — and the scoped queue of what is waiting for one approver.
"""

from helpdesk.shared.access.approvals import (
    GROUP_SLOTS,
    NotAnApproverError,
    PendingWithContext,
    RequestNotPendingError,
    SlotDecision,
    SlotState,
    chain_is_complete,
    decide_slot,
    dump_slots,
    first_fillable_index,
    is_rejected,
    list_pending_for_approver,
    list_pending_with_context_for_approver,
    lock_request,
    may_fill,
    parse_slots,
    request_in_scope,
)
from helpdesk.shared.access.directory import (
    DirectoryProvider,
    DirectoryReader,
    SimulatedDirectory,
)
from helpdesk.shared.access.execute import (
    GRANT_EXECUTED_EVENT_TYPE,
    PolicyBlockedAtExecutionError,
    SodConflictAtExecutionError,
    execute_grant,
    reclassify,
)
from helpdesk.shared.access.expiry import (
    EXPIRED_GRANT_STATUS,
    EXPIRY_WARNING_HORIZON_DAYS,
    GRANT_EXPIRED_EVENT_TYPE,
    expire_grants,
    warn_expiring_grants,
)
from helpdesk.shared.access.leavers import LEAVER_SWEEP_ACTOR_REF, sweep_leavers
from helpdesk.shared.access.lifecycle import (
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
from helpdesk.shared.access.requests import (
    REQUEST_REJECTED_EVENT_TYPE,
    append_request_rejected,
    approval_slots,
    create_access_request,
    list_requests_for_requester,
    lock_open_requests_for_target,
    resolved_approver_ids,
    set_request_status,
)
from helpdesk.shared.access.review_schedule import (
    GENERATED_SYSTEM_SENSITIVITIES,
    REVIEW_DUE_DAYS,
    REVIEW_ESCALATION_DAYS,
    REVIEW_GENERATION_ACTOR_REF,
    generate_reviews,
    send_review_reminders,
)
from helpdesk.shared.access.reviews import (
    PERIOD_LENGTH,
    REVIEW_ITEM_DECIDED_EVENT_TYPE,
    REVIEW_OPENED_EVENT_TYPE,
    REVIEW_PERIOD_PATTERN,
    REVIEW_REVOKE_REASON,
    STALE_WINDOW,
    DecideItemResult,
    InvalidPeriodError,
    ItemAlreadyDecidedError,
    OpenReviewResult,
    ReviewAlreadyOpenError,
    ReviewItemWithContext,
    ReviewScope,
    ReviewWithCounts,
    UnassignableReviewError,
    check_period,
    decide_item,
    find_item,
    find_review,
    list_items,
    list_items_with_context,
    list_owned_system_ids,
    list_reviews_for_scope,
    open_review,
    quarter_period,
)
from helpdesk.shared.access.revoke import (
    GRANT_REVOKED_EVENT_TYPE,
    REVOKED_GRANT_STATUS,
    SYSTEM_ACTOR_KIND,
    USER_ACTOR_KIND,
    GrantNotActiveError,
    MissingActorError,
    resolve_actor_ref,
    revoke_grant_row,
    revoke_role_access,
)
from helpdesk.shared.access.snapshot import build_request_snapshot

__all__ = [
    "EXPIRED_GRANT_STATUS",
    "EXPIRY_WARNING_HORIZON_DAYS",
    "GENERATED_SYSTEM_SENSITIVITIES",
    "GRANT_EXECUTED_EVENT_TYPE",
    "GRANT_EXPIRED_EVENT_TYPE",
    "GRANT_REVOKED_EVENT_TYPE",
    "GROUP_SLOTS",
    "JOINER_PRINCIPALS",
    "JOINER_STATUS",
    "LEAVER_STATUS",
    "LEAVER_SWEEP_ACTOR_REF",
    "OFFBOARD_REVOKE_REASON",
    "PERIOD_LENGTH",
    "REQUEST_REJECTED_EVENT_TYPE",
    "REVIEW_DUE_DAYS",
    "REVIEW_ESCALATION_DAYS",
    "REVIEW_GENERATION_ACTOR_REF",
    "REVIEW_ITEM_DECIDED_EVENT_TYPE",
    "REVIEW_OPENED_EVENT_TYPE",
    "REVIEW_PERIOD_PATTERN",
    "REVIEW_REVOKE_REASON",
    "REVOKED_GRANT_STATUS",
    "STALE_WINDOW",
    "SYSTEM_ACTOR_KIND",
    "TRANSFER_REVOKE_REASON",
    "UNUSABLE_PASSWORD_HASH",
    "USER_ACTOR_KIND",
    "USER_OFFBOARDED_EVENT_TYPE",
    "USER_ONBOARDED_EVENT_TYPE",
    "USER_TRANSFERRED_EVENT_TYPE",
    "AlreadyLeaverError",
    "DecideItemResult",
    "DirectoryProvider",
    "DirectoryReader",
    "DuplicateEmailError",
    "GrantNotActiveError",
    "InvalidPeriodError",
    "ItemAlreadyDecidedError",
    "LeaverTransferError",
    "MissingActorError",
    "NotAnApproverError",
    "OffboardResult",
    "OnboardResult",
    "OpenReviewResult",
    "PendingWithContext",
    "PolicyBlockedAtExecutionError",
    "RequestNotPendingError",
    "ReviewAlreadyOpenError",
    "ReviewItemWithContext",
    "ReviewScope",
    "ReviewWithCounts",
    "SameDepartmentError",
    "SimulatedDirectory",
    "SkippedRole",
    "SlotDecision",
    "SlotState",
    "SodConflictAtExecutionError",
    "TransferResult",
    "UnassignableReviewError",
    "append_request_rejected",
    "approval_slots",
    "build_request_snapshot",
    "chain_is_complete",
    "check_period",
    "create_access_request",
    "decide_item",
    "decide_slot",
    "dump_slots",
    "execute_grant",
    "expire_grants",
    "find_item",
    "find_review",
    "first_fillable_index",
    "generate_reviews",
    "is_rejected",
    "list_items",
    "list_items_with_context",
    "list_owned_system_ids",
    "list_pending_for_approver",
    "list_pending_with_context_for_approver",
    "list_requests_for_requester",
    "list_reviews_for_scope",
    "lock_open_requests_for_target",
    "lock_request",
    "may_fill",
    "offboard_user",
    "onboard_user",
    "open_review",
    "parse_slots",
    "quarter_period",
    "reclassify",
    "request_in_scope",
    "resolve_actor_ref",
    "resolved_approver_ids",
    "revoke_grant_row",
    "revoke_role_access",
    "send_review_reminders",
    "set_request_status",
    "sweep_leavers",
    "transfer_user",
    "warn_expiring_grants",
]
