"""What the assistant tells a person they can do, one plain sentence per tool.

Pure: no I/O. The lines come from the RBAC matrix through `rbac.tools_for`, so
the assistant never offers an action the person's token would be refused. A
tool with no sentence is never shown, and a check fails when a
tool someone can call has none, so the list cannot drift from the real tools.
"""

from collections.abc import Mapping, Sequence
from types import MappingProxyType

from helpdesk.shared.auth.rbac import Tool, tools_for

SENTENCES: Mapping[Tool, str] = MappingProxyType(
    {
        Tool.GET_ME: "Tell you which account you are signed in with.",
        Tool.LIST_MY_ACCESS: "Show the access you hold right now.",
        Tool.LIST_MY_REQUESTS: "Show the access requests you have filed and where each one stands.",
        Tool.LIST_REQUESTABLE_ROLES: "List the systems and roles you can ask for.",
        Tool.RESET_OWN_PASSWORD: "Reset your own password.",
        Tool.UNLOCK_OWN_ACCOUNT: "Unlock your own account.",
        Tool.CREATE_ACCESS_REQUEST_SELF: "Ask for access to a system for yourself.",
        Tool.CREATE_ACCESS_REQUEST_ON_BEHALF: "Ask for access on behalf of someone else.",
        Tool.SEARCH_POLICY: "Answer questions about the access rules, with sources.",
        Tool.SUGGEST_ROLES: "Suggest the closest role when you are not sure of its name.",
        Tool.CREATE_TICKET: "Open a ticket for a person to pick up.",
        Tool.LIST_MY_TICKETS: (
            "Show the tickets you have opened and whether each is open or solved."
        ),
        Tool.LIST_OPEN_TICKETS: "Show every open ticket in your company.",
        Tool.SOLVE_TICKET: "Mark an open ticket solved.",
        Tool.CLASSIFY_ACCESS_REQUEST: "Check if a request would need approval, before you file it.",
        Tool.LIST_PENDING_APPROVALS: "Show the requests waiting for your approval.",
        Tool.APPROVE_REQUEST: "Approve a request that is waiting on you.",
        Tool.REJECT_REQUEST: "Reject a request that is waiting on you.",
        Tool.CANCEL_REQUEST: "Cancel a request of yours that is still waiting for approval.",
        Tool.REVOKE_ACCESS: "Remove access you hold, or access you look after for others.",
        Tool.ONBOARD_USER: "Set up a new joiner with their starting access.",
        Tool.TRANSFER_USER: "Move a person to another department.",
        Tool.OFFBOARD_USER: "Remove a leaver's access.",
        Tool.OPEN_ACCESS_REVIEW: "Open an access review.",
        Tool.LIST_REVIEW_ITEMS: "Show the flagged grants in an access review.",
        Tool.LIST_REVIEWS: "Show the access reviews you can work on.",
        Tool.DECIDE_REVIEW_ITEM: "Keep or remove a flagged grant in a review.",
        Tool.SEARCH_AUDIT: "Search the audit log.",
        Tool.ROLLBACK_REQUEST: "Roll back a request that was granted.",
        Tool.SET_KILL_SWITCH: "Stop or restart a tool for the whole company.",
        Tool.LIST_KILL_SWITCHES: "Show which tools are stopped.",
        Tool.SET_TOOL_STEPS_VISIBLE: "Choose whether the chat shows its steps.",
        Tool.LIST_SOD_RULES: "Show the rules on which roles may not be held together.",
        Tool.LIST_POLICY_DOCUMENTS: "Show the policy files and their versions.",
        Tool.REINGEST_POLICY_DOCUMENT: "Reload a policy file.",
        Tool.READ_POLICIES: (
            "Point you to the company's full access rules, through the link under the chat heading."
        ),
        Tool.LIST_NOTIFICATIONS: "Show your notifications.",
        Tool.MARK_NOTIFICATION_READ: "Mark a notification as read.",
    }
)
"""One sentence per tool, written to the person. `execute_grant` has none: no
user may call it."""


def capability_lines(principals: Sequence[str]) -> list[str]:
    """The sentence of each tool these principals may call, in matrix order.
    Unknown principals give nothing."""
    return [SENTENCES[tool] for tool in tools_for(principals) if tool in SENTENCES]
