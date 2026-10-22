"""What the assistant says a person can do, built from the RBAC matrix.

Surface under test (written before it exists):

``helpdesk.shared.auth.rbac``
    ``Tool.LIST_REQUESTABLE_ROLES`` (``"list_requestable_roles"``) and
    ``tools_for(principals) -> list[Tool]``: every tool at least one of the
    principals may call, in a fixed order that does not depend on the order
    of the principals. Unknown principals contribute nothing.

``helpdesk.shared.auth.capabilities``
    ``SENTENCES: Mapping[rbac.Tool, str]`` — one plain sentence per tool.
    ``capability_lines(principals) -> list[str]`` — the sentence of each tool
    in ``tools_for(principals)``, no repeats; a tool with no sentence is not
    shown. Pure: no I/O.

The wording of a sentence is not pinned here; the tests compare lines with the
table, so rewording a sentence never breaks them but offering the wrong tool does.
"""

from helpdesk.shared.auth import rbac
from helpdesk.shared.auth.capabilities import SENTENCES, capability_lines
from helpdesk.shared.auth.rbac import Tool, tools_for

EMPLOYEE_TOOLS = (
    Tool.RESET_OWN_PASSWORD,
    Tool.UNLOCK_OWN_ACCOUNT,
    Tool.CREATE_ACCESS_REQUEST_SELF,
    Tool.LIST_MY_ACCESS,
    Tool.LIST_MY_REQUESTS,
)
"""The five self-service actions an employee is told about, written out."""

APPROVE_TOOLS = (Tool.APPROVE_REQUEST, Tool.LIST_PENDING_APPROVALS)


def test_every_matrix_tool_has_an_entry() -> None:
    """Catches a tool added to the matrix with no sentence, which the lines
    would then silently leave out, so the list drifts from the real tools.
    The one tool no user may call (`execute_grant`) is exempt: it is never
    shown to anyone."""
    callable_by_someone = set(tools_for([p.value for p in rbac.Principal]))

    assert callable_by_someone <= set(SENTENCES)
    assert Tool.EXECUTE_GRANT not in callable_by_someone
    assert Tool.LIST_REQUESTABLE_ROLES in callable_by_someone


def test_an_employee_gets_only_self_service_lines() -> None:
    """Catches lines built from a fixed list instead of the matrix: the
    employee must get the five self-service sentences and not one of the
    approval sentences, which an employee's token is refused."""
    lines = capability_lines(("employee",))

    for tool in EMPLOYEE_TOOLS:
        assert SENTENCES[tool] in lines, tool
    for tool in APPROVE_TOOLS:
        assert SENTENCES[tool] not in lines, tool
    assert set(lines) <= {SENTENCES[tool] for tool in tools_for(("employee",)) if tool in SENTENCES}
    assert len(lines) == len(set(lines))


def test_a_manager_also_gets_approvals() -> None:
    """Catches lines that ignore the principal: a manager must get everything
    an employee gets, plus the approvals sentence the matrix gives a manager."""
    employee = capability_lines(("employee",))
    manager = capability_lines(("manager",))

    assert set(employee) < set(manager)
    assert SENTENCES[Tool.APPROVE_REQUEST] in manager
    assert SENTENCES[Tool.APPROVE_REQUEST] not in employee


def test_lines_are_exactly_the_sentences_of_the_tools_the_principals_may_call() -> None:
    """Catches a sentence shown for a tool the principals cannot call (a
    platform-only switch offered to an auditor) and a callable tool dropped:
    for every principal and for a mixed pair the lines equal the table
    restricted to `tools_for`."""
    cases = [(p.value,) for p in rbac.Principal] + [("manager", "system_owner")]
    for principals in cases:
        wanted = {SENTENCES[tool] for tool in tools_for(principals) if tool in SENTENCES}
        assert set(capability_lines(principals)) == wanted, principals


def test_tools_for_does_not_depend_on_the_order_of_the_principals() -> None:
    """Catches an ordered result that follows the token's principal order, which
    would reshuffle the capability lines between two turns of one person."""
    assert tools_for(("manager", "employee")) == tools_for(("employee", "manager"))
    assert len(tools_for(("manager", "employee"))) == len(set(tools_for(("manager", "employee"))))


def test_no_user_is_offered_the_system_only_tool() -> None:
    """Catches a sentence for `execute_grant` reaching a person: no principal
    holds it, so offering it would promise an action nobody can take."""
    for principal in rbac.Principal:
        assert Tool.EXECUTE_GRANT not in tools_for((principal.value,))


def test_unknown_principals_give_nothing() -> None:
    """Catches a fall-back to the widest or the employee row for a principal
    the matrix does not know: a forged or stale `root` must get no lines, and
    must not raise either."""
    assert capability_lines(("root",)) == []
    assert capability_lines(()) == []
    assert tools_for(("root",)) == []
    assert capability_lines(("root", "employee")) == capability_lines(("employee",))


def test_an_employee_is_told_they_can_see_their_tickets() -> None:
    """Catches a new tool with no sentence, or a sentence worded differently
    from the one the plan names: the employee's lines must hold exactly this
    text, and it must be the sentence of the new tool."""
    sentence = "Show the tickets you have opened and whether each is open or solved."

    assert SENTENCES[Tool.LIST_MY_TICKETS] == sentence
    assert sentence in capability_lines(("employee",))


def test_an_it_admin_is_told_they_can_see_open_tickets() -> None:
    """Catches a new tool with no sentence, a sentence worded differently from
    the one the plan names, and a sentence that also reaches the employee, who
    is refused the tool."""
    sentence = "Show every open ticket in your company."

    assert SENTENCES[Tool.LIST_OPEN_TICKETS] == sentence
    assert sentence in capability_lines(("it_admin",))
    assert sentence not in capability_lines(("employee",))


def test_an_it_admin_is_told_they_can_solve_tickets() -> None:
    """Catches a new tool with no sentence, a sentence worded differently from
    the one the plan names, and a sentence that also reaches the employee, who
    is refused the tool."""
    sentence = "Mark an open ticket solved."

    assert SENTENCES[Tool.SOLVE_TICKET] == sentence
    assert sentence in capability_lines(("it_admin",))
    assert sentence not in capability_lines(("employee",))


def test_an_employee_is_told_they_can_remove_their_own_access() -> None:
    """Catches a revoke sentence that still says "remove access" without the
    self wording (an employee told they can remove anyone's), and one that is
    missing from the employee's lines because the employee row has no revoke
    cell."""
    sentence = "Remove access you hold, or access you look after for others."

    assert SENTENCES[Tool.REVOKE_ACCESS] == sentence
    assert sentence in capability_lines(("employee",))
