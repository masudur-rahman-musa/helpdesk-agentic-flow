"""The one shared rule that names a role in plain words.

Surface, in `helpdesk.agent_api.agent.role_words`:
`plain_role_name(system_name, role_slug) -> str` (the system's name, then the
slug with `-` and `_` read as spaces).

Nothing backing: pure functions on plain strings.
"""

import pytest

from helpdesk.agent_api.agent.role_words import plain_role_name


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
