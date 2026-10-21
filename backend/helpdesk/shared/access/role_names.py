"""Roles in plain words: the one rule every person-facing line uses.

Pure: no I/O. A role reads as its system's name, then the role slug with `-`
and `_` read as spaces ("Internal Wiki editor"). The stored role name
(`wiki.editor`) is what matching and the tools read; people never see it.
"""


def role_word(role_slug: str) -> str:
    """The role's own word: its slug with `-` and `_` read as spaces."""
    return role_slug.replace("-", " ").replace("_", " ")


def plain_role_name(system_name: str, role_slug: str) -> str:
    """ "Internal Wiki editor": the system's name, then the role's own word."""
    return f"{system_name} {role_word(role_slug)}"
