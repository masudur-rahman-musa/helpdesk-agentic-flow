"""Access domain: the directory port, the snapshot builder, and execution.

`directory.py` is the design's `DirectoryProvider` seam (read half from plan
11a, `apply_grant` from 11b). `snapshot.py` turns repository state into plan
08's `RequestSnapshot`. `requests.py` holds the access-request reads and
writes the tools consume. `execute.py` is the one place a grant is written,
with the design's re-check in front of it.
"""

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
)
from helpdesk.access.snapshot import build_request_snapshot

__all__ = [
    "GRANT_EXECUTED_EVENT_TYPE",
    "DirectoryProvider",
    "PolicyBlockedAtExecutionError",
    "SimulatedDirectory",
    "SodConflictAtExecutionError",
    "approval_slots",
    "build_request_snapshot",
    "create_access_request",
    "execute_grant",
    "list_requests_for_requester",
    "resolved_approver_ids",
]
