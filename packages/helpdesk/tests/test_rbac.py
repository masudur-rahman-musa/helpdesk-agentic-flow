"""Tests for helpdesk.auth.rbac.

Expected implementation surface (the implementer must provide exactly this):

``helpdesk.auth.rbac`` (pure: no I/O; identity comes ONLY from claims)
    ``class Tool(StrEnum)``  -- one member per tool in the the design matrix;
        ``CREATE_ACCESS_REQUEST_SELF = "create_access_request_self"`` and
        ``CREATE_ACCESS_REQUEST_ON_BEHALF = "create_access_request_on_behalf"``
        are two distinct members.
    ``class Principal(StrEnum)`` -- employee, manager, system_owner, hr,
        it_admin, auditor, platform_admin.
    ``class Scope(StrEnum)`` -- any, own_department, own_systems, self_only,
        system_only.
    ``class RbacDenied(ToolError)`` with attribute ``code == "FORBIDDEN"``.
    ``class UnknownToolError(ToolError)`` for a tool name not in the matrix.
    ``def authorize(claims, tool) -> Authorization`` -- frozen model carrying
        at least ``tool: Tool`` and ``scope: Scope``. Raises ``RbacDenied``
        when no principal allows, ``UnknownToolError`` for an unknown tool.

Scope width total order (widest first): any > own_department > own_systems
> self_only. A claims with several principals gets the widest scope among
the principals that allow the tool.
"""

import inspect

import pytest
from helpdesk.auth.jwt import TokenClaims
from helpdesk.auth.rbac import (
    Authorization,
    Principal,
    RbacDenied,
    Scope,
    Tool,
    UnknownToolError,
    authorize,
    principals_of,
)

PRINCIPALS: tuple[str, ...] = (
    "employee",
    "manager",
    "system_owner",
    "hr",
    "it_admin",
    "auditor",
    "platform_admin",
)

# The the design matrix, written out literally per tool and per principal so the
# test cannot drift together with the implementation. Value is the expected
# scope string on allow, or None for deny.
#
# Column order everywhere: employee, manager, system_owner, hr, it_admin,
# auditor, platform_admin.
MATRIX: dict[str, dict[str, str | None]] = {
    # Self-scoped tools: allowed for all seven principals with scope self_only.
    "get_me": dict.fromkeys(PRINCIPALS, "self_only"),
    "list_my_access": dict.fromkeys(PRINCIPALS, "self_only"),
    "list_my_requests": dict.fromkeys(PRINCIPALS, "self_only"),
    "reset_own_password": dict.fromkeys(PRINCIPALS, "self_only"),
    "unlock_own_account": dict.fromkeys(PRINCIPALS, "self_only"),
    "create_access_request_self": dict.fromkeys(PRINCIPALS, "self_only"),
    # An earlier change: a notification is addressed to ONE person and read
    # and cleared by that person, so both rows join the self-scoped block
    # above rather than the review rows they arrive beside. the design gives
    # `/notifications` to every principal, so a None in either row would be
    # somebody who cannot read — or cannot clear — their own bell.
    "list_notifications": dict.fromkeys(PRINCIPALS, "self_only"),
    "mark_notification_read": dict.fromkeys(PRINCIPALS, "self_only"),
    # Allowed for all seven principals with scope any.
    "search_policy": dict.fromkeys(PRINCIPALS, "any"),
    "create_ticket": dict.fromkeys(PRINCIPALS, "any"),
    "classify_access_request": dict.fromkeys(PRINCIPALS, "any"),
    "create_access_request_on_behalf": {
        "employee": None,
        "manager": "own_department",
        "system_owner": None,
        "hr": "any",
        "it_admin": "any",
        "auditor": None,
        "platform_admin": "any",
    },
    # System-only: DENIED for every user principal.
    "execute_grant": dict.fromkeys(PRINCIPALS, None),
    "list_pending_approvals": {
        "employee": None,
        "manager": "own_department",
        "system_owner": "own_systems",
        "hr": None,
        "it_admin": "any",
        "auditor": None,
        "platform_admin": "any",
    },
    "approve_request": {
        "employee": None,
        "manager": "own_department",
        "system_owner": "own_systems",
        "hr": None,
        "it_admin": "any",
        "auditor": None,
        "platform_admin": "any",
    },
    "reject_request": {
        "employee": None,
        "manager": "own_department",
        "system_owner": "own_systems",
        "hr": None,
        "it_admin": "any",
        "auditor": None,
        "platform_admin": "any",
    },
    "revoke_access": {
        "employee": None,
        "manager": "own_department",
        "system_owner": "own_systems",
        "hr": "any",
        "it_admin": "any",
        "auditor": None,
        "platform_admin": "any",
    },
    "onboard_user": {
        "employee": None,
        "manager": None,
        "system_owner": None,
        "hr": "any",
        "it_admin": "any",
        "auditor": None,
        "platform_admin": "any",
    },
    "transfer_user": {
        "employee": None,
        "manager": None,
        "system_owner": None,
        "hr": "any",
        "it_admin": "any",
        "auditor": None,
        "platform_admin": "any",
    },
    "offboard_user": {
        "employee": None,
        "manager": None,
        "system_owner": None,
        "hr": "any",
        "it_admin": None,  # deliberately denied for it_admin
        "auditor": None,
        "platform_admin": "any",
    },
    "open_access_review": {
        "employee": None,
        "manager": "own_department",
        "system_owner": "own_systems",
        "hr": "any",
        "it_admin": "any",
        "auditor": None,
        "platform_admin": "any",
    },
    "list_review_items": {
        "employee": None,
        "manager": "own_department",
        "system_owner": "own_systems",
        "hr": "any",
        "it_admin": "any",
        "auditor": None,
        "platform_admin": "any",
    },
    "decide_review_item": {
        "employee": None,
        "manager": "own_department",
        "system_owner": "own_systems",
        "hr": "any",
        "it_admin": "any",
        "auditor": None,
        "platform_admin": "any",
    },
    # An earlier change: the review QUEUE carries the same cell as the three
    # rows above. A manager reads their own department's reviews, a system
    # owner their own systems', and the three `any` principals every one in the
    # tenant; `employee` and `auditor` hold nothing, so neither can learn which
    # scopes are under review and how far along each one is.
    "list_reviews": {
        "employee": None,
        "manager": "own_department",
        "system_owner": "own_systems",
        "hr": "any",
        "it_admin": "any",
        "auditor": None,
        "platform_admin": "any",
    },
    "search_audit": {
        "employee": None,
        "manager": "own_department",
        "system_owner": "own_systems",
        "hr": "any",
        "it_admin": "any",
        "auditor": "any",
        "platform_admin": "any",
    },
    "rollback_request": {
        "employee": None,
        "manager": None,
        "system_owner": None,
        "hr": None,
        "it_admin": "any",
        "auditor": None,
        "platform_admin": "any",
    },
    "set_kill_switch": {
        "employee": None,
        "manager": None,
        "system_owner": None,
        "hr": None,
        "it_admin": None,
        "auditor": None,
        "platform_admin": "any",
    },
}

ALL_CASES: list[tuple[str, str, str | None]] = [
    (tool, principal, expected)
    for tool, row in MATRIX.items()
    for principal, expected in row.items()
]


def _claims(principals: list[str]) -> TokenClaims:
    """Build a valid TokenClaims carrying the given principals."""
    return TokenClaims(
        sub="0b1f8a52-1111-4aaa-8aaa-000000000001",
        tenant_id="0b1f8a52-2222-4bbb-8bbb-000000000002",
        department_id="0b1f8a52-3333-4ccc-8ccc-000000000003",
        principals=principals,
        sid="0b1f8a52-4444-4ddd-8ddd-000000000004",
        iat=1_788_000_000,
        exp=1_788_028_800,
    )


# --- 1. Exhaustive matrix: every tool x every single principal ---------------


@pytest.mark.parametrize(("tool", "principal", "expected"), ALL_CASES)
def test_matrix_single_principal(tool: str, principal: str, expected: str | None) -> None:
    claims = _claims([principal])
    if expected is None:
        with pytest.raises(RbacDenied) as excinfo:
            authorize(claims, tool)
        assert excinfo.value.code == "FORBIDDEN"
    else:
        result = authorize(claims, tool)
        assert result.tool == Tool(tool)
        assert result.scope == Scope(expected)


def test_matrix_covers_every_tool_enum_member() -> None:
    """The literal table above and the Tool enum must cover the same tools."""
    assert {member.value for member in Tool} == set(MATRIX)


def test_principal_enum_matches_expected_members() -> None:
    assert {member.value for member in Principal} == set(PRINCIPALS)


def test_scope_enum_matches_expected_members() -> None:
    assert {member.value for member in Scope} == {
        "any",
        "own_department",
        "own_systems",
        "self_only",
        "system_only",
    }


def test_tool_enum_distinguishes_self_and_on_behalf() -> None:
    assert Tool.CREATE_ACCESS_REQUEST_SELF.value == "create_access_request_self"
    assert Tool.CREATE_ACCESS_REQUEST_ON_BEHALF.value == "create_access_request_on_behalf"
    assert len({Tool.CREATE_ACCESS_REQUEST_SELF, Tool.CREATE_ACCESS_REQUEST_ON_BEHALF}) == 2


# --- 2. Multi-principal union: any allowing principal allows, widest scope ---
# Width total order used here (widest first):
#   any > own_department > own_systems > self_only


@pytest.mark.parametrize(
    ("principals", "tool", "expected_scope"),
    [
        # employee does not allow approve_request; manager does -> allowed.
        (["employee", "manager"], "approve_request", "own_department"),
        # it_admin's `any` is wider than manager's own_department.
        (["manager", "it_admin"], "approve_request", "any"),
        # auditor's `any` wins even though employee is denied outright.
        (["employee", "auditor"], "search_audit", "any"),
        # own_department is wider than own_systems in the total order.
        (["manager", "system_owner"], "revoke_access", "own_department"),
        # Adding non-allowing principals must never turn an allow into a deny.
        (["employee", "auditor", "hr"], "onboard_user", "any"),
        (["employee", "system_owner", "auditor", "platform_admin"], "set_kill_switch", "any"),
    ],
)
def test_multi_principal_union_widest_scope(
    principals: list[str], tool: str, expected_scope: str
) -> None:
    result = authorize(_claims(principals), tool)
    assert result.scope == Scope(expected_scope)


def test_multi_principal_denied_only_when_no_principal_allows() -> None:
    with pytest.raises(RbacDenied) as excinfo:
        authorize(_claims(["employee", "auditor"]), "onboard_user")
    assert excinfo.value.code == "FORBIDDEN"


# --- 3. execute_grant is system-only: denied even with all seven principals --


def test_execute_grant_denied_with_all_principals() -> None:
    with pytest.raises(RbacDenied) as excinfo:
        authorize(_claims(list(PRINCIPALS)), "execute_grant")
    assert excinfo.value.code == "FORBIDDEN"


# --- 4. Unknown tool -> UnknownToolError, never a silent allow ---------------


@pytest.mark.parametrize("tool", ["drop_database", "", "GET_ME", "execute_grant "])
def test_unknown_tool_raises(tool: str) -> None:
    with pytest.raises(UnknownToolError):
        authorize(_claims(["platform_admin"]), tool)


# --- 5. Purity: authorize takes exactly claims + tool, no identity args ------


def test_authorize_signature_has_no_identity_parameters() -> None:
    params = list(inspect.signature(authorize).parameters)
    assert len(params) == 2
    assert "user_id" not in params
    assert "tenant_id" not in params


# --- 6. Unknown principal strings in claims are ignored, not honoured --------


def test_unknown_principal_string_is_ignored() -> None:
    with pytest.raises(RbacDenied) as excinfo:
        authorize(_claims(["root"]), "get_me")
    assert excinfo.value.code == "FORBIDDEN"


def test_unknown_principal_does_not_widen_scope() -> None:
    result = authorize(_claims(["root", "employee"]), "get_me")
    assert result.scope == Scope("self_only")


# --- Authorization result shape ----------------------------------------------


def test_authorization_result_is_frozen() -> None:
    result: Authorization = authorize(_claims(["employee"]), "get_me")
    with pytest.raises(Exception):  # noqa: B017 - frozen models raise a validation error
        setattr(result, "scope", Scope("any"))  # noqa: B010 - static assignment is rejected


# --- 7. `scopes`: every allowing principal's scope, not just the widest ------
#
# An earlier change decision B. `scope` keeps its meaning (the widest, still what the
# pipeline records on the audit payload) and `scopes` is added beside it, so
# the one enforcement that needs the full set — the approval queue, whose
# manager+system_owner caller must see the UNION of two incomparable reaches —
# can stop collapsing it. Every case below is additive: nothing above changes.


@pytest.mark.parametrize(("tool", "principal", "expected"), ALL_CASES)
def test_matrix_single_principal_scopes(tool: str, principal: str, expected: str | None) -> None:
    """Catches `scopes` left empty, or built from the whole matrix row rather
    than from the principals the TOKEN carries.

    Walks the same exhaustive (tool, principal) table as the `scope` case
    above, so a tool whose row is right for `scope` and wrong for `scopes`
    cannot hide behind a sampled case. One allowing principal means exactly
    one scope in the set — a set that also contains the scopes of principals
    the token does not hold would widen every single-principal caller.
    """
    claims = _claims([principal])
    if expected is None:
        with pytest.raises(RbacDenied) as excinfo:
            authorize(claims, tool)
        assert excinfo.value.code == "FORBIDDEN"
    else:
        result = authorize(claims, tool)
        assert result.scopes == frozenset({Scope(expected)})
        assert result.scope in result.scopes


@pytest.mark.parametrize(
    ("principals", "tool", "expected_scope", "expected_scopes"),
    [
        # The case an earlier change exists for: two incomparable reaches, one caller.
        # `scope` collapses them; `scopes` must not.
        (
            ["manager", "system_owner"],
            "approve_request",
            "own_department",
            {"own_department", "own_systems"},
        ),
        (
            ["manager", "system_owner"],
            "list_pending_approvals",
            "own_department",
            {"own_department", "own_systems"},
        ),
        # A non-allowing principal contributes nothing to the set.
        (["employee", "manager"], "approve_request", "own_department", {"own_department"}),
        # The widest is in the set beside the narrow one, never instead of it.
        (["manager", "it_admin"], "approve_request", "any", {"own_department", "any"}),
        (
            ["manager", "system_owner", "it_admin", "platform_admin"],
            "list_pending_approvals",
            "any",
            {"own_department", "own_systems", "any"},
        ),
        # Three allowing principals, one scope: a set, not a per-principal
        # list, so a caller cannot be counted three times.
        (["hr", "it_admin", "platform_admin"], "search_audit", "any", {"any"}),
        (["employee", "auditor", "hr"], "onboard_user", "any", {"any"}),
        (["employee", "manager"], "get_me", "self_only", {"self_only"}),
    ],
)
def test_multi_principal_scopes_carry_every_allowing_scope(
    principals: list[str],
    tool: str,
    expected_scope: str,
    expected_scopes: set[str],
) -> None:
    """Catches `scopes` built as `{scope}` — the widest wrapped in a set,
    which reads like the union and is the exact narrowing this plan removes:
    a manager who also owns a system would keep seeing only their department's
    queue. Also catches the reverse regression, `scope` widened or narrowed to
    match the set, which would change what every existing caller records."""
    result = authorize(_claims(principals), tool)

    assert result.scope == Scope(expected_scope)
    assert result.scopes == {Scope(value) for value in expected_scopes}


def test_unknown_principal_strings_never_appear_in_scopes() -> None:
    """Catches an implementation that maps principal strings to scopes before
    filtering them through the enum: a token carrying `root` beside a real
    principal must contribute exactly one scope, not two."""
    result = authorize(_claims(["root", "employee"]), "get_me")

    assert result.scopes == frozenset({Scope.SELF_ONLY})


def test_scopes_is_an_immutable_set() -> None:
    """Catches `scopes` typed as a mutable set on a frozen model: a caller
    holding the `Authorization` could add `Scope.ANY` to it and widen a queue
    that was already authorized, with the frozen model none the wiser."""
    result = authorize(_claims(["manager", "system_owner"]), "approve_request")

    assert isinstance(result.scopes, frozenset)


def test_principals_of_keeps_the_matrix_members_and_drops_the_rest() -> None:
    """`principals_of` alone decides who may fill a the design group approver
    seat (`helpdesk.access.approvals.may_fill` takes its output), so it needs
    its own pin rather than riding on `authorize`'s.

    Catches the two ways it can go wrong: raising on a principal string
    outside the enum, which would turn a token carrying a stale principal
    into an `INTERNAL` on every approval, and passing raw strings through,
    which would let a token claiming `it_admin ` or `hr-team` fill a group
    seat the matrix never granted. The known principals are asserted present
    in the same call, so "return an empty set" cannot pass it.
    """
    result = principals_of(_claims(["it_admin", "root", "hr-team", "manager", ""]))

    assert result == frozenset({Principal.IT_ADMIN, Principal.MANAGER})
    assert isinstance(result, frozenset)
