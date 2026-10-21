"""The turn's budget and the tool names code reads, with no framework and no I/O."""

from collections.abc import Sequence

from helpdesk.agent_api.agent.state import ToolResult

MAX_TOOL_CALLS = 6
"""The most tool calls one turn may run, inclusive."""

MAX_TURN_TOKENS = 40000
"""Inclusive; a few loop steps plus a reply."""

TURN_DEADLINE_SECONDS = 90.0
"""Inclusive. Three MCP read timeouts, so a stalled tool times out first."""

CREATE_ACCESS_REQUEST = "create_access_request"
"""Named, because `classify_access_request` returns the same `decision`
field but files nothing."""

ALREADY_REQUESTED = "already_requested"
"""Set on a classify or create result when the same request is already open."""

APPROVE_REQUEST = "approve_request"
REJECT_REQUEST = "reject_request"

LIST_PENDING_APPROVALS = "list_pending_approvals"
"""The listing an approval in chat must come from."""


def listed_request_ids(results: Sequence[ToolResult]) -> frozenset[str]:
    """The request ids this turn's successful approval listings showed."""
    ids: set[str] = set()
    for result in results:
        if result.tool != LIST_PENDING_APPROVALS or not result.ok:
            continue
        rows: object = result.payload.get("approvals")
        if not isinstance(rows, list):
            continue
        ids.update(str(row.get("request_id")) for row in rows if isinstance(row, dict))
    return frozenset(ids)
