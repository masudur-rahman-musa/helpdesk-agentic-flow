"""An earlier change section 6: which web tabs a token's principals may see.

Surface under test (written before it exists; every case is red on the one
import below until step 2 lands):

``helpdesk.auth.rbac.tabs_for(principals: Sequence[str]) -> Iterable[str]``
    The stable tab KEYS the principals may see. Shapes this file fixes that the
    plan left open (a project rule and 55; they belong in 42's Deviations):
    - it lives in ``rbac.py`` itself, beside ``authorize``, and takes the raw
      principal STRINGS off the token (unknown strings included), not a set of
      ``Principal`` members, so the unknown-string row can be asked.
    - the keys are ``chat requests access approvals reviews notifications audit
      killswitch rules policies``, the same ten the web's link table is keyed
      by (``web/tests/role-nav.test.tsx`` holds the same literal list).
    - the four universal keys (``chat requests access notifications``) are
      ALWAYS returned, even for an empty or all-unknown principal list: they
      are not gated by any tool, so no principal string can take them away.
    - the result is compared as a set; order carries no meaning.

Each gated tab is tied to ONE read tool: approvals to ``list_pending_approvals``,
reviews to ``list_reviews``, audit to ``search_audit``, killswitch to
``list_kill_switches``, rules to ``list_sod_rules``, policies to
``list_policy_documents``. The literal table below pins the expected key sets;
the drift table derives them from ``authorize`` itself, so a tab whose tool is
swapped for a different one is caught by the drift table even where the two
tools happen to agree for every principal in the literal table.
"""

import pytest

from helpdesk.shared.auth.jwt import TokenClaims
from helpdesk.shared.auth.rbac import Principal, RbacDenied, Tool, authorize, tabs_for

UNIVERSAL: frozenset[str] = frozenset({"chat", "requests", "access", "notifications"})

# The ten keys, as literals: the web's link table is keyed by the same list.
ALL_KEYS: frozenset[str] = UNIVERSAL | {
    "approvals",
    "reviews",
    "audit",
    "killswitch",
    "rules",
    "policies",
}

QUEUES: frozenset[str] = frozenset({"approvals", "reviews", "audit"})

# Column order: principals on the token, then the exact key set expected.
LITERAL_ROWS: tuple[tuple[str, list[str], frozenset[str]], ...] = (
    ("employee", ["employee"], UNIVERSAL),
    ("manager", ["manager"], UNIVERSAL | QUEUES),
    ("system_owner", ["system_owner"], UNIVERSAL | QUEUES),
    ("hr", ["hr"], UNIVERSAL | QUEUES),
    ("it_admin", ["it_admin"], ALL_KEYS - {"killswitch"}),
    ("auditor", ["auditor"], UNIVERSAL | {"audit"}),
    ("platform_admin", ["platform_admin"], ALL_KEYS),
    ("manager and system_owner", ["manager", "system_owner"], UNIVERSAL | QUEUES),
)

# The tool each gated tab is tied to, written out independently of the code.
TAB_TOOLS: dict[str, Tool] = {
    "approvals": Tool.LIST_PENDING_APPROVALS,
    "reviews": Tool.LIST_REVIEWS,
    "audit": Tool.SEARCH_AUDIT,
    "killswitch": Tool.LIST_KILL_SWITCHES,
    "rules": Tool.LIST_SOD_RULES,
    "policies": Tool.LIST_POLICY_DOCUMENTS,
}


def _claims(principals: list[str]) -> TokenClaims:
    return TokenClaims(
        sub="0b1f8a52-1111-4aaa-8aaa-000000000001",
        tenant_id="0b1f8a52-2222-4bbb-8bbb-000000000002",
        department_id="0b1f8a52-3333-4ccc-8ccc-000000000003",
        principals=principals,
        sid="0b1f8a52-4444-4ddd-8ddd-000000000004",
        iat=1_788_000_000,
        exp=1_788_028_800,
    )


def _allowed(principals: list[str], tool: Tool) -> bool:
    try:
        authorize(_claims(principals), tool)
    except RbacDenied:
        return False
    return True


@pytest.mark.parametrize(
    ("principals", "expected"),
    [(row[1], row[2]) for row in LITERAL_ROWS],
    ids=[row[0] for row in LITERAL_ROWS],
)
def test_each_principal_sees_exactly_its_literal_tab_set(
    principals: list[str], expected: frozenset[str]
) -> None:
    """Catches a tab set that is wider or narrower than the plan's counts.

    Employee four, manager/system_owner/hr seven, it_admin nine, auditor five,
    platform_admin ten. An implementation giving managers the two admin boards,
    or it_admin the kill switch, fails its own row. The union row also pins that
    two principals never lose a tab to each other.
    """
    returned = list(tabs_for(principals))

    assert set(returned) == expected
    assert len(returned) == len(expected)


def test_the_literal_table_covers_every_principal() -> None:
    """Catches a principal added to the enum that no row above walks.

    Without this the table silently stops covering the matrix the day an
    eighth principal lands; the drift table below walks the enum itself.
    """
    walked = {row[1][0] for row in LITERAL_ROWS if len(row[1]) == 1}

    assert walked == {member.value for member in Principal}


@pytest.mark.parametrize("principal", [member.value for member in Principal])
def test_a_gated_tab_is_shown_exactly_when_its_tool_is_allowed(principal: str) -> None:
    """Catches a tab tied to a different tool than the one its page calls.

    For every principal and every gated tab the key is present if and only if
    `authorize` allows the tab's tool. A kill switch tab tied to
    `set_kill_switch` agrees with `list_kill_switches` today, so only a tab tied
    to a tool with a different cell (say `list_reviews` swapped for
    `onboard_user`) flips a row here.
    """
    returned = set(tabs_for([principal]))

    for key, tool in TAB_TOOLS.items():
        assert (key in returned) == _allowed([principal], tool), (principal, key)
    assert returned >= UNIVERSAL


def test_the_tab_table_covers_every_gated_key() -> None:
    """Catches a gated key the drift table has no tool for.

    A key returned for platform_admin that is neither universal nor in
    `TAB_TOOLS` would be a tab the drift test never compares to a tool.
    """
    assert set(tabs_for([Principal.PLATFORM_ADMIN.value])) == UNIVERSAL | set(TAB_TOOLS)


@pytest.mark.parametrize(
    "principals",
    [
        ["employee", "chief_of_staff"],
        ["employee", "admin"],
        ["employee", "PLATFORM_ADMIN"],
        ["employee", "platform_admin "],
        ["chief_of_staff"],
        [],
    ],
    ids=["unknown", "admin", "upper-case", "trailing-space", "unknown-only", "empty"],
)
def test_an_unknown_principal_string_never_adds_a_tab(principals: list[str]) -> None:
    """Catches a matcher that honours a near-miss string.

    A case-folding or `startswith` match would turn `PLATFORM_ADMIN` or
    `platform_admin ` into the real principal and open all ten tabs. The only
    known principal on any row is `employee` (or none), so the four universal
    tabs are all that may ever come back.
    """
    assert set(tabs_for(principals)) == UNIVERSAL
