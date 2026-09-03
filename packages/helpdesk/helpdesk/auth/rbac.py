"""RBAC matrix: who may call which tool, with what scope.

Single source of truth for the design. Pure: no I/O, no clock, no DB. Identity
comes only from a verified `TokenClaims` — `authorize` derives the principal
set from `claims.principals` and never accepts user or tenant identity as an
argument, so an identity-from-arguments path cannot exist.

Scope here is declarative: the matrix records *how far* an allowing
principal may reach (the design writes it in the table), while enforcement
with data — which department, which systems — belongs to the tool
implementations that hold that data. `create_access_request` appears twice
(self vs on-behalf-of-report) because the spec's table has two rows with
different principal sets.

`execute_grant` is system-only: it stays in the matrix so an unknown-tool
error cannot mask it, but no principal allows it — the system invokes it
internally, never a user.
"""

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict

from helpdesk.auth.jwt import TokenClaims


class Tool(StrEnum):
    GET_ME = "get_me"
    LIST_MY_ACCESS = "list_my_access"
    LIST_MY_REQUESTS = "list_my_requests"
    RESET_OWN_PASSWORD = "reset_own_password"
    UNLOCK_OWN_ACCOUNT = "unlock_own_account"
    CREATE_ACCESS_REQUEST_SELF = "create_access_request_self"
    CREATE_ACCESS_REQUEST_ON_BEHALF = "create_access_request_on_behalf"
    SEARCH_POLICY = "search_policy"
    CREATE_TICKET = "create_ticket"
    CLASSIFY_ACCESS_REQUEST = "classify_access_request"
    EXECUTE_GRANT = "execute_grant"
    LIST_PENDING_APPROVALS = "list_pending_approvals"
    APPROVE_REQUEST = "approve_request"
    REJECT_REQUEST = "reject_request"
    REVOKE_ACCESS = "revoke_access"
    ONBOARD_USER = "onboard_user"
    TRANSFER_USER = "transfer_user"
    OFFBOARD_USER = "offboard_user"
    OPEN_ACCESS_REVIEW = "open_access_review"
    LIST_REVIEW_ITEMS = "list_review_items"
    DECIDE_REVIEW_ITEM = "decide_review_item"
    SEARCH_AUDIT = "search_audit"
    ROLLBACK_REQUEST = "rollback_request"
    SET_KILL_SWITCH = "set_kill_switch"


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


# Total width order, widest first. A multi-principal user gets the widest
# scope among the principals that allow the tool. system_only never appears
# on an allowing entry (nothing user-callable carries it) but has a defined
# rank so the order stays total.
_SCOPE_WIDTH: Mapping[Scope, int] = MappingProxyType(
    {
        Scope.ANY: 4,
        Scope.OWN_DEPARTMENT: 3,
        Scope.OWN_SYSTEMS: 2,
        Scope.SELF_ONLY: 1,
        Scope.SYSTEM_ONLY: 0,
    }
)


class RbacDenied(Exception):
    """No principal on the token allows the tool. the design typed code only."""

    code: str = "FORBIDDEN"


class UnknownToolError(Exception):
    """The tool name is not in the matrix: refuse loudly, never silently allow."""


class Authorization(BaseModel):
    model_config = ConfigDict(frozen=True)

    tool: Tool
    scope: Scope


def _all(scope: Scope) -> dict[Principal, Scope]:
    return dict.fromkeys(Principal, scope)


# the design, one entry per table row; principals absent from an entry are denied.
_MATRIX: Mapping[Tool, Mapping[Principal, Scope]] = MappingProxyType(
    {
        Tool.GET_ME: _all(Scope.SELF_ONLY),
        Tool.LIST_MY_ACCESS: _all(Scope.SELF_ONLY),
        Tool.LIST_MY_REQUESTS: _all(Scope.SELF_ONLY),
        Tool.RESET_OWN_PASSWORD: _all(Scope.SELF_ONLY),
        Tool.UNLOCK_OWN_ACCOUNT: _all(Scope.SELF_ONLY),
        Tool.CREATE_ACCESS_REQUEST_SELF: _all(Scope.SELF_ONLY),
        Tool.SEARCH_POLICY: _all(Scope.ANY),
        Tool.CREATE_TICKET: _all(Scope.ANY),
        Tool.CLASSIFY_ACCESS_REQUEST: _all(Scope.ANY),
        Tool.CREATE_ACCESS_REQUEST_ON_BEHALF: {
            Principal.MANAGER: Scope.OWN_DEPARTMENT,
            Principal.HR: Scope.ANY,
            Principal.IT_ADMIN: Scope.ANY,
            Principal.PLATFORM_ADMIN: Scope.ANY,
        },
        Tool.EXECUTE_GRANT: {},  # system-only: no user principal, ever
        Tool.LIST_PENDING_APPROVALS: {
            Principal.MANAGER: Scope.OWN_DEPARTMENT,
            Principal.SYSTEM_OWNER: Scope.OWN_SYSTEMS,
            Principal.IT_ADMIN: Scope.ANY,
            Principal.PLATFORM_ADMIN: Scope.ANY,
        },
        Tool.APPROVE_REQUEST: {
            Principal.MANAGER: Scope.OWN_DEPARTMENT,
            Principal.SYSTEM_OWNER: Scope.OWN_SYSTEMS,
            Principal.IT_ADMIN: Scope.ANY,
            Principal.PLATFORM_ADMIN: Scope.ANY,
        },
        Tool.REJECT_REQUEST: {
            Principal.MANAGER: Scope.OWN_DEPARTMENT,
            Principal.SYSTEM_OWNER: Scope.OWN_SYSTEMS,
            Principal.IT_ADMIN: Scope.ANY,
            Principal.PLATFORM_ADMIN: Scope.ANY,
        },
        Tool.REVOKE_ACCESS: {
            Principal.MANAGER: Scope.OWN_DEPARTMENT,
            Principal.SYSTEM_OWNER: Scope.OWN_SYSTEMS,
            Principal.HR: Scope.ANY,
            Principal.IT_ADMIN: Scope.ANY,
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
    return Authorization(tool=tool_key, scope=max(granted, key=_SCOPE_WIDTH.__getitem__))


def _as_principal(raw: str) -> Principal | None:
    try:
        return Principal(raw)
    except ValueError:
        return None
