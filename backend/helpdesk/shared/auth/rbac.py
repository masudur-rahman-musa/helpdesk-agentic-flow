"""Who may call which tool, and how far they may reach.

Pure: no I/O, no clock, no DB. Principals come only from a checked token,
never from arguments. The scope here only says how far a caller may reach;
the tools that hold the data enforce it. A caller can hold several scopes
that do not compare (manager and system owner), so `Authorization` carries
all of them as well as the widest. `execute_grant` is in the table so an
unknown-tool error cannot hide it, but no user may call it.
"""

from collections.abc import Mapping, Sequence
from enum import StrEnum
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict

from helpdesk.shared.auth.jwt import TokenClaims
from helpdesk.shared.core.errors import ErrorCode, ToolError


class Tool(StrEnum):
    GET_ME = "get_me"
    LIST_MY_ACCESS = "list_my_access"
    LIST_MY_REQUESTS = "list_my_requests"
    LIST_REQUESTABLE_ROLES = "list_requestable_roles"
    RESET_OWN_PASSWORD = "reset_own_password"
    UNLOCK_OWN_ACCOUNT = "unlock_own_account"
    CREATE_ACCESS_REQUEST_SELF = "create_access_request_self"
    CREATE_ACCESS_REQUEST_ON_BEHALF = "create_access_request_on_behalf"
    SEARCH_POLICY = "search_policy"
    SUGGEST_ROLES = "suggest_roles"
    CREATE_TICKET = "create_ticket"
    LIST_MY_TICKETS = "list_my_tickets"
    LIST_OPEN_TICKETS = "list_open_tickets"
    SOLVE_TICKET = "solve_ticket"
    CLASSIFY_ACCESS_REQUEST = "classify_access_request"
    EXECUTE_GRANT = "execute_grant"
    LIST_PENDING_APPROVALS = "list_pending_approvals"
    APPROVE_REQUEST = "approve_request"
    REJECT_REQUEST = "reject_request"
    CANCEL_REQUEST = "cancel_request"
    REVOKE_ACCESS = "revoke_access"
    ONBOARD_USER = "onboard_user"
    TRANSFER_USER = "transfer_user"
    OFFBOARD_USER = "offboard_user"
    OPEN_ACCESS_REVIEW = "open_access_review"
    LIST_REVIEW_ITEMS = "list_review_items"
    LIST_REVIEWS = "list_reviews"
    DECIDE_REVIEW_ITEM = "decide_review_item"
    SEARCH_AUDIT = "search_audit"
    ROLLBACK_REQUEST = "rollback_request"
    SET_KILL_SWITCH = "set_kill_switch"
    LIST_KILL_SWITCHES = "list_kill_switches"
    SET_TOOL_STEPS_VISIBLE = "set_tool_steps_visible"
    LIST_SOD_RULES = "list_sod_rules"
    LIST_POLICY_DOCUMENTS = "list_policy_documents"
    REINGEST_POLICY_DOCUMENT = "reingest_policy_document"
    LIST_NOTIFICATIONS = "list_notifications"
    MARK_NOTIFICATION_READ = "mark_notification_read"


class Principal(StrEnum):
    EMPLOYEE = "employee"
    MANAGER = "manager"
    SYSTEM_OWNER = "system_owner"
    HR = "hr"
    IT_ADMIN = "it_admin"
    AUDITOR = "auditor"
    PLATFORM_ADMIN = "platform_admin"


class Scope(StrEnum):
    ANY = "any"
    OWN_DEPARTMENT = "own_department"
    OWN_SYSTEMS = "own_systems"
    SELF_ONLY = "self_only"
    SYSTEM_ONLY = "system_only"


# Widest first. A caller with several principals gets the widest scope that
# allows the tool.
_SCOPE_WIDTH: Mapping[Scope, int] = MappingProxyType(
    {
        Scope.ANY: 4,
        Scope.OWN_DEPARTMENT: 3,
        Scope.OWN_SYSTEMS: 2,
        Scope.SELF_ONLY: 1,
        Scope.SYSTEM_ONLY: 0,
    }
)


class RbacDenied(ToolError):
    """No principal on the token allows the tool."""

    code = ErrorCode.FORBIDDEN


class UnknownToolError(ToolError):
    """The tool name is not in the table. `INTERNAL`: the calling code is wrong, not the user."""

    code = ErrorCode.INTERNAL


class Authorization(BaseModel):
    model_config = ConfigDict(frozen=True)

    tool: Tool
    scope: Scope
    """The widest allowing scope; this is what the audit payload records."""

    scopes: frozenset[Scope]
    """Every allowing scope. A manager who is also a system owner would lose
    their systems under `scope` alone, so code that can act on both (like
    the approvals queue) reads this and joins them."""


def _all(scope: Scope) -> dict[Principal, Scope]:
    return dict.fromkeys(Principal, scope)


# One entry per tool; a principal missing from an entry is refused.
_MATRIX: Mapping[Tool, Mapping[Principal, Scope]] = MappingProxyType(
    {
        Tool.GET_ME: _all(Scope.SELF_ONLY),
        Tool.LIST_MY_ACCESS: _all(Scope.SELF_ONLY),
        Tool.LIST_MY_REQUESTS: _all(Scope.SELF_ONLY),
        Tool.LIST_MY_TICKETS: _all(Scope.SELF_ONLY),
        # The tenant's catalogue plus the caller's own template and grants;
        # nothing about another person.
        Tool.LIST_REQUESTABLE_ROLES: _all(Scope.SELF_ONLY),
        Tool.RESET_OWN_PASSWORD: _all(Scope.SELF_ONLY),
        Tool.UNLOCK_OWN_ACCOUNT: _all(Scope.SELF_ONLY),
        Tool.CREATE_ACCESS_REQUEST_SELF: _all(Scope.SELF_ONLY),
        # Only a request the caller filed or is the target of; the tool checks which.
        Tool.CANCEL_REQUEST: _all(Scope.SELF_ONLY),
        # A notification goes to one person, so everyone may read and
        # clear their own.
        Tool.LIST_NOTIFICATIONS: _all(Scope.SELF_ONLY),
        Tool.MARK_NOTIFICATION_READ: _all(Scope.SELF_ONLY),
        Tool.SEARCH_POLICY: _all(Scope.ANY),
        Tool.SUGGEST_ROLES: _all(Scope.ANY),
        Tool.CREATE_TICKET: _all(Scope.ANY),
        Tool.CLASSIFY_ACCESS_REQUEST: _all(Scope.ANY),
        Tool.CREATE_ACCESS_REQUEST_ON_BEHALF: {
            Principal.MANAGER: Scope.OWN_DEPARTMENT,
            Principal.HR: Scope.ANY,
            Principal.IT_ADMIN: Scope.ANY,
            Principal.PLATFORM_ADMIN: Scope.ANY,
        },
        Tool.EXECUTE_GRANT: {},  # system-only: no user principal, ever
        # `hr` needs these three: a critical HRIS chain has an `hr` seat,
        # and without it that request would wait in `pending` for ever.
        Tool.LIST_PENDING_APPROVALS: {
            Principal.MANAGER: Scope.OWN_DEPARTMENT,
            Principal.SYSTEM_OWNER: Scope.OWN_SYSTEMS,
            Principal.HR: Scope.ANY,
            Principal.IT_ADMIN: Scope.ANY,
            Principal.PLATFORM_ADMIN: Scope.ANY,
        },
        Tool.APPROVE_REQUEST: {
            Principal.MANAGER: Scope.OWN_DEPARTMENT,
            Principal.SYSTEM_OWNER: Scope.OWN_SYSTEMS,
            Principal.HR: Scope.ANY,
            Principal.IT_ADMIN: Scope.ANY,
            Principal.PLATFORM_ADMIN: Scope.ANY,
        },
        Tool.REJECT_REQUEST: {
            Principal.MANAGER: Scope.OWN_DEPARTMENT,
            Principal.SYSTEM_OWNER: Scope.OWN_SYSTEMS,
            Principal.HR: Scope.ANY,
            Principal.IT_ADMIN: Scope.ANY,
            Principal.PLATFORM_ADMIN: Scope.ANY,
        },
        # Every role may give up its own access; `self_only` reaches no one
        # else's grant, so an employee still cannot touch a colleague's.
        Tool.REVOKE_ACCESS: {
            Principal.EMPLOYEE: Scope.SELF_ONLY,
            Principal.MANAGER: Scope.OWN_DEPARTMENT,
            Principal.SYSTEM_OWNER: Scope.OWN_SYSTEMS,
            Principal.HR: Scope.ANY,
            Principal.IT_ADMIN: Scope.ANY,
            Principal.AUDITOR: Scope.SELF_ONLY,
            Principal.PLATFORM_ADMIN: Scope.ANY,
        },
        Tool.ONBOARD_USER: {
            Principal.HR: Scope.ANY,
            Principal.IT_ADMIN: Scope.ANY,
            Principal.PLATFORM_ADMIN: Scope.ANY,
        },
        Tool.TRANSFER_USER: {
            Principal.HR: Scope.ANY,
            Principal.IT_ADMIN: Scope.ANY,
            Principal.PLATFORM_ADMIN: Scope.ANY,
        },
        Tool.OFFBOARD_USER: {
            Principal.HR: Scope.ANY,
            Principal.PLATFORM_ADMIN: Scope.ANY,
        },
        Tool.OPEN_ACCESS_REVIEW: {
            Principal.MANAGER: Scope.OWN_DEPARTMENT,
            Principal.SYSTEM_OWNER: Scope.OWN_SYSTEMS,
            Principal.HR: Scope.ANY,
            Principal.IT_ADMIN: Scope.ANY,
            Principal.PLATFORM_ADMIN: Scope.ANY,
        },
        Tool.LIST_REVIEW_ITEMS: {
            Principal.MANAGER: Scope.OWN_DEPARTMENT,
            Principal.SYSTEM_OWNER: Scope.OWN_SYSTEMS,
            Principal.HR: Scope.ANY,
            Principal.IT_ADMIN: Scope.ANY,
            Principal.PLATFORM_ADMIN: Scope.ANY,
        },
        # Same audience as the review tools: a wider queue would show
        # reviews to callers who may not open them.
        Tool.LIST_REVIEWS: {
            Principal.MANAGER: Scope.OWN_DEPARTMENT,
            Principal.SYSTEM_OWNER: Scope.OWN_SYSTEMS,
            Principal.HR: Scope.ANY,
            Principal.IT_ADMIN: Scope.ANY,
            Principal.PLATFORM_ADMIN: Scope.ANY,
        },
        Tool.DECIDE_REVIEW_ITEM: {
            Principal.MANAGER: Scope.OWN_DEPARTMENT,
            Principal.SYSTEM_OWNER: Scope.OWN_SYSTEMS,
            Principal.HR: Scope.ANY,
            Principal.IT_ADMIN: Scope.ANY,
            Principal.PLATFORM_ADMIN: Scope.ANY,
        },
        Tool.SEARCH_AUDIT: {
            Principal.MANAGER: Scope.OWN_DEPARTMENT,
            Principal.SYSTEM_OWNER: Scope.OWN_SYSTEMS,
            Principal.HR: Scope.ANY,
            Principal.IT_ADMIN: Scope.ANY,
            Principal.AUDITOR: Scope.ANY,
            Principal.PLATFORM_ADMIN: Scope.ANY,
        },
        Tool.ROLLBACK_REQUEST: {
            Principal.IT_ADMIN: Scope.ANY,
            Principal.PLATFORM_ADMIN: Scope.ANY,
        },
        Tool.SET_KILL_SWITCH: {
            Principal.PLATFORM_ADMIN: Scope.ANY,
        },
        # Same audience as the write: others could not change what they see.
        Tool.LIST_KILL_SWITCHES: {
            Principal.PLATFORM_ADMIN: Scope.ANY,
        },
        # A tenant-wide choice about what the chat shows, so only the
        # platform admin makes it. The settings tab hangs on this tool.
        Tool.SET_TOOL_STEPS_VISIBLE: {
            Principal.PLATFORM_ADMIN: Scope.ANY,
        },
        # Admin setup, not history; an auditor reads what happened through
        # `search_audit`.
        Tool.LIST_SOD_RULES: {
            Principal.IT_ADMIN: Scope.ANY,
            Principal.PLATFORM_ADMIN: Scope.ANY,
        },
        # Every open ticket in the company is admin work, not a person's own.
        Tool.LIST_OPEN_TICKETS: {
            Principal.IT_ADMIN: Scope.ANY,
            Principal.PLATFORM_ADMIN: Scope.ANY,
        },
        # The write half of the row above, with the same audience.
        Tool.SOLVE_TICKET: {
            Principal.IT_ADMIN: Scope.ANY,
            Principal.PLATFORM_ADMIN: Scope.ANY,
        },
        # The policy files' version history, which only an admin can act
        # on. Everyone can still search the policy text itself.
        Tool.LIST_POLICY_DOCUMENTS: {
            Principal.IT_ADMIN: Scope.ANY,
            Principal.PLATFORM_ADMIN: Scope.ANY,
        },
        # The write half of the row above, with the same audience.
        Tool.REINGEST_POLICY_DOCUMENT: {
            Principal.IT_ADMIN: Scope.ANY,
            Principal.PLATFORM_ADMIN: Scope.ANY,
        },
    }
)


def authorize(claims: TokenClaims, tool: Tool | str) -> Authorization:
    """Allow-with-scope for the token's principals, or a typed refusal.

    Principal strings on the token that are not in the `Principal` enum are
    ignored (never honoured, never widening); an empty resulting set denies.
    """
    try:
        tool_key = Tool(tool)
    except ValueError:
        raise UnknownToolError(f"unknown tool: {tool!r}") from None

    entry = _MATRIX[tool_key]
    granted = [
        entry[principal]
        for raw in claims.principals
        if (principal := _as_principal(raw)) is not None and principal in entry
    ]
    if not granted:
        raise RbacDenied(tool_key.value)
    return Authorization(
        tool=tool_key,
        scope=max(granted, key=_SCOPE_WIDTH.__getitem__),
        scopes=frozenset(granted),
    )


def principals_of(claims: TokenClaims) -> frozenset[Principal]:
    """The token's principals as matrix members, unknown strings dropped.

    A group approver seat needs to know which principal a caller holds, and
    `Authorization` only carries scopes. This decides who may fill such a
    seat, so it matches `authorize`: an unknown string never counts.
    """
    return frozenset(
        principal for raw in claims.principals if (principal := _as_principal(raw)) is not None
    )


def tools_for(principals: Sequence[str]) -> list[Tool]:
    """Every tool at least one of these principal strings may call, in `Tool`
    order, so the result does not depend on the order of the principals.

    An unknown string adds nothing. Like `tabs_for`, this only says what to
    show; the tool's own `authorize` is still the real check.
    """
    held = {principal for raw in principals if (principal := _as_principal(raw)) is not None}
    return [tool for tool in Tool if any(principal in _MATRIX[tool] for principal in held)]


def _as_principal(raw: str) -> Principal | None:
    try:
        return Principal(raw)
    except ValueError:
        return None


# The web app's tabs in link order. Each gated tab is tied to the one read
# tool its page calls, so it shows exactly when that read would be allowed.
_TAB_TOOLS: Mapping[str, Tool | None] = MappingProxyType(
    {
        "chat": None,
        "requests": None,
        "access": None,
        "approvals": Tool.LIST_PENDING_APPROVALS,
        "reviews": Tool.LIST_REVIEWS,
        "notifications": None,
        "tickets": Tool.LIST_OPEN_TICKETS,
        "audit": Tool.SEARCH_AUDIT,
        "killswitch": Tool.LIST_KILL_SWITCHES,
        "rules": Tool.LIST_SOD_RULES,
        "policies": Tool.LIST_POLICY_DOCUMENTS,
        "settings": Tool.SET_TOOL_STEPS_VISIBLE,
    }
)


def tabs_for(principals: Sequence[str]) -> list[str]:
    """The tab keys these principal strings may see, in link order.

    An unknown string never adds a tab. Hiding a tab is only for looks; the
    tool's own `authorize` is still the real check.
    """
    held = {principal for raw in principals if (principal := _as_principal(raw)) is not None}
    return [
        key
        for key, tool in _TAB_TOOLS.items()
        if tool is None or any(principal in _MATRIX[tool] for principal in held)
    ]
