"""The one shared rule that names a role in plain words.

Surface the implementer must provide, in `helpdesk.agent_api.agent.role_words`:
`plain_role_name(system_name, role_slug) -> str` (the system's name, then the
slug with `-` and `_` read as spaces) and `grouped_suggestions(roles) -> str`
(pairs of (system_name, role_slug), best first; grouped by system in
first-seen order; "a or b", "a, b or c"; groups joined by "; ").

Nothing backing: pure functions on plain strings.
"""

import pytest

from helpdesk.agent_api.agent.role_words import grouped_suggestions, plain_role_name


@pytest.mark.parametrize(
    ("system_name", "role_slug", "expected"),
    [
        ("Internal Wiki", "editor", "Internal Wiki editor"),
        ("Production AWS", "deployer", "Production AWS deployer"),
    ],
)
def test_the_plain_name_is_system_name_then_role_word(
    system_name: str, role_slug: str, expected: str
) -> None:
    """Catches a name built from the stored role name ("wiki.editor"), from the
    system slug, or with the two parts swapped: only the system's own name then
    the role's own word gives these strings."""
    assert plain_role_name(system_name, role_slug) == expected


@pytest.mark.parametrize(
    ("system_name", "role_slug", "expected"),
    [
        ("Payroll", "payroll-approver", "Payroll payroll approver"),
        ("Warehouse", "goods_receipt", "Warehouse goods receipt"),
        ("Warehouse", "a-b_c", "Warehouse a b c"),
    ],
)
def test_dashes_and_underscores_read_as_spaces(
    system_name: str, role_slug: str, expected: str
) -> None:
    """Catches a rule that swaps only one of the two separators, so
    "goods_receipt" or "payroll-approver" would reach the chat with a dash or
    an underscore. The third row has both in one slug."""
    assert plain_role_name(system_name, role_slug) == expected


def test_suggestions_are_grouped_by_system() -> None:
    """Catches a text that lists every role on its own with the system repeated
    ("Internal Wiki editor; Internal Wiki viewer; Mail and Files user") instead
    of one group per system."""
    roles = [
        ("Internal Wiki", "editor"),
        ("Internal Wiki", "viewer"),
        ("Mail and Files", "user"),
    ]

    assert grouped_suggestions(roles) == "Internal Wiki: editor or viewer; Mail and Files: user"


def test_a_single_role_in_a_group_has_no_joiner() -> None:
    """Catches a join that leaves a stray "or" or comma on a one-role group."""
    assert grouped_suggestions([("Mail and Files", "user")]) == "Mail and Files: user"


def test_three_roles_on_one_system_join_with_commas_then_or() -> None:
    """Catches a join that uses "or" between every pair ("admin or editor or
    viewer") or "and", or puts a comma before the last "or"."""
    roles = [
        ("Internal Wiki", "admin"),
        ("Internal Wiki", "editor"),
        ("Internal Wiki", "viewer"),
    ]

    assert grouped_suggestions(roles) == "Internal Wiki: admin, editor or viewer"


def test_groups_keep_the_best_first_order() -> None:
    """Catches a grouping that sorts systems by name, or that puts the system of
    the first wiki role first: the mail role is scored above both wiki roles, so
    its group must come first, and the wiki roles keep their given order."""
    roles = [
        ("Mail and Files", "user"),
        ("Internal Wiki", "viewer"),
        ("Internal Wiki", "editor"),
    ]

    assert grouped_suggestions(roles) == "Mail and Files: user; Internal Wiki: viewer or editor"


def test_a_system_seen_again_later_joins_its_first_group() -> None:
    """Catches a grouping that only merges neighbours: the wiki roles are split
    by a mail role in the list, and must still land in one wiki group placed at
    the first wiki role's position."""
    roles = [
        ("Internal Wiki", "editor"),
        ("Mail and Files", "user"),
        ("Internal Wiki", "viewer"),
    ]

    assert grouped_suggestions(roles) == "Internal Wiki: editor or viewer; Mail and Files: user"


def test_slug_separators_read_as_spaces_in_suggestions_too() -> None:
    """Catches a suggestion text that prints the raw slug while the plain name
    function rewrites it: "payroll-approver" must read "payroll approver"."""
    assert grouped_suggestions([("Payroll", "payroll-approver")]) == "Payroll: payroll approver"
