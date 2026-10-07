"""RBAC matrix: who may call which tool, with what scope.

Single source of truth for the design. Pure: no I/O, no clock, no DB. Identity
comes only from a verified `TokenClaims` — `authorize` derives the principal
set from `claims.principals` and never accepts user or tenant identity as an
argument, so an identity-from-arguments path cannot exist.

Scope here is declarative: the matrix records *how far* an allowing
principal may reach (the design writes it in the table), while enforcement
with data — which department, which systems — belongs to the tool
implementations that hold that data. An `Authorization` therefore carries
both `scope` (the widest allowing scope, one value) and `scopes` (all of
them): a caller who is both a manager and a system owner holds two
incomparable scopes, and enforcement that can act on both must not see only
the wider one. `create_access_request` appears twice
(self vs on-behalf-of-report) because the spec's table has two rows with
different principal sets.

`execute_grant` is system-only: it stays in the matrix so an unknown-tool
error cannot mask it, but no principal allows it — the system invokes it
internally, never a user.
"""

from collections.abc import Mapping, Sequence
from enum import StrEnum
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict

from helpdesk.auth.jwt import TokenClaims
from helpdesk.errors import ErrorCode, ToolError


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
    LIST_REVIEWS = "list_reviews"
    DECIDE_REVIEW_ITEM = "decide_review_item"
    SEARCH_AUDIT = "search_audit"
    ROLLBACK_REQUEST = "rollback_request"
    SET_KILL_SWITCH = "set_kill_switch"
    LIST_KILL_SWITCHES = "list_kill_switches"
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


class RbacDenied(ToolError):
    """No principal on the token allows the tool. the design typed code only."""

    code = ErrorCode.FORBIDDEN


class UnknownToolError(ToolError):
    """The tool name is not in the matrix: refuse loudly, never silently allow.

    Not a user-facing condition: a name outside the matrix means the caller
    is wrong, not the user, so it carries `INTERNAL` rather than `FORBIDDEN`
    (which would read as a legitimate permission decision).
    """

    code = ErrorCode.INTERNAL


class Authorization(BaseModel):
    model_config = ConfigDict(frozen=True)

    tool: Tool
    scope: Scope
    """The WIDEST scope among the allowing principals, and what the MCP
    pipeline records on every audit payload. Total-order collapse: a caller
    holding two incomparable scopes reads as the wider one alone."""

    scopes: frozenset[Scope]
    """Every allowing principal's scope, un-collapsed.

    `scope` is a total-order maximum, so a caller who is both a `manager`
    (`own_department`) and a `system_owner` (`own_systems`) reads as
    `own_department` and their owned systems disappear — the narrowing plan
    06 recorded and an earlier change re-owed. Enforcement that has the data to act on
    both reads THIS instead and unions the two, which is what
    `mcp_server.tools.approvals.list_pending_approvals` does. Pinned by
    `test_rbac.py::test_multi_principal_scopes_carry_every_allowing_scope`
    (which fails for `scopes = {scope}`) and, end to end, by
    `test_mcp_approvals.py::test_a_manager_and_system_owner_caller_sees_the_union_of_both_queues`."""


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
        # An earlier change, a design note: a notification is addressed to ONE person,
        # so reading the bell and clearing one entry are both `self_only`
        # for every principal. the design gives `/notifications` to all seven,
        # and an absent principal on either row would be somebody who
        # cannot read — or cannot clear — their own messages.
        Tool.LIST_NOTIFICATIONS: _all(Scope.SELF_ONLY),
        Tool.MARK_NOTIFICATION_READ: _all(Scope.SELF_ONLY),
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
        # `hr` carries `any` on all three by a design note.
        # the design puts an `hr` seat on a `critical` HRIS chain while the design
        # originally gave `hr` no cell here, so that seat was fillable by
        # NOBODY — an `hr` caller was refused before any seat was examined,
        # and an `it_admin` or `platform_admin` reaching the tool does not
        # hold the `hr` principal the seat names — and such a request parked
        # in `pending` for ever with no error anywhere. `any` is the scope
        # `hr` already holds on every other row it appears in; an HR officer
        # has no department or system to narrow against. The seat exclusions
        # are untouched: requester and target still fill no seat, and one
        # person still fills at most one seat of a chain.
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
        # An earlier change: the queue that answers "which reviews are waiting on me",
        # carrying the same cell as the three tools above it. A queue wider
        # than the tools it feeds would name scopes, reviewers and progress to
        # a caller who may not open the reviews themselves.
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
        # The READ half of the row above, with the same single cell (ADR
        # 0019). the design gives `/admin/killswitch` to `platform_admin`
        # alone, so a board whose audience is wider than the write's would
        # show six principals switches none of them can change.
        Tool.LIST_KILL_SWITCHES: {
            Principal.PLATFORM_ADMIN: Scope.ANY,
        },
        # An earlier change: the separation-of-duties rules read as rules, for SPEC
        # §14's `/admin/rules` page, which that section gives to `it_admin`
        # and `platform_admin`. Not `auditor`: an auditor reads
        # what HAPPENED through `search_audit`, and the rules are
        # configuration a principal with no route to the page cannot act on.
        Tool.LIST_SOD_RULES: {
            Principal.IT_ADMIN: Scope.ANY,
            Principal.PLATFORM_ADMIN: Scope.ANY,
        },
        # An earlier change: the policy corpus read as documents, for the design's
        # `/admin/policies` page, which that section gives to `it_admin` and
        # `platform_admin` — the same audience as the rules board above, for
        # the same reason (a design note, cited rather than re-opened). Not
        # `auditor`: an auditor reads what HAPPENED through `search_audit`,
        # and the corpus is configuration. Not `employee` either, though
        # `SEARCH_POLICY` gives every principal the corpus itself: searching
        # returns the spans an answer may cite, while this row is the ingest's
        # version history, which only an admin can act on.
        Tool.LIST_POLICY_DOCUMENTS: {
            Principal.IT_ADMIN: Scope.ANY,
            Principal.PLATFORM_ADMIN: Scope.ANY,
        },
        # An earlier change: making an edited corpus file live is the WRITE half of the
        # row above, so it carries exactly that row's audience and no wider
        # one. The five other principals are refused whether the corpus file
        # exists or not, so the tool never reports on a tenant's corpus to
        # someone who may not read it.
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

    The same read `authorize` does, exposed for the enforcement that needs to
    know WHICH principal a caller holds rather than how far they may reach:
    a the design group approver seat is filled by a holder of that group's
    principal, and `Authorization` carries scopes, not principals. Dropping
    an unknown string here rather than raising keeps it identical to
    `authorize`'s own behaviour — a principal outside the enum is never
    honoured and never widens anything. Pinned by
    `test_rbac.py::test_principals_of_keeps_the_matrix_members_and_drops_the_rest`,
    which matters because this function alone decides who may fill a SPEC
    §9.3 group approver seat.
    """
    return frozenset(
        principal for raw in claims.principals if (principal := _as_principal(raw)) is not None
    )


def _as_principal(raw: str) -> Principal | None:
    try:
        return Principal(raw)
    except ValueError:
        return None


# An earlier change: the web shell's ten tabs by stable key, in link order. The first
# four are open to every principal and gated by no tool; each other key is
# tied to the ONE read tool its page calls, so a tab shows exactly when the
# page's own read would not be refused. Pinned by
# `test_rbac_tabs.py::test_a_gated_tab_is_shown_exactly_when_its_tool_is_allowed`.
_TAB_TOOLS: Mapping[str, Tool | None] = MappingProxyType(
    {
        "chat": None,
        "requests": None,
        "access": None,
        "approvals": Tool.LIST_PENDING_APPROVALS,
        "reviews": Tool.LIST_REVIEWS,
        "notifications": None,
        "audit": Tool.SEARCH_AUDIT,
        "killswitch": Tool.LIST_KILL_SWITCHES,
        "rules": Tool.LIST_SOD_RULES,
        "policies": Tool.LIST_POLICY_DOCUMENTS,
    }
)


def tabs_for(principals: Sequence[str]) -> list[str]:
    """The tab keys these principal strings may see, in link order.

    Reads `_MATRIX` with the same enum match `authorize` uses, so an unknown
    string (`PLATFORM_ADMIN`, `platform_admin `) is dropped and never widens
    (`test_rbac_tabs.py::test_an_unknown_principal_string_never_adds_a_tab`).
    The four universal keys come back even for an empty list. Hiding a tab is
    cosmetic: the tool's own `authorize` stays the gate.
    """
    held = {principal for raw in principals if (principal := _as_principal(raw)) is not None}
    return [
        key
        for key, tool in _TAB_TOOLS.items()
        if tool is None or any(principal in _MATRIX[tool] for principal in held)
    ]
