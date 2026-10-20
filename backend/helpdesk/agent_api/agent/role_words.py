"""Roles in plain words, the one rule every code-written chat line uses.

Pure: no I/O. A role reads as its system's name, then the role slug with `-`
and `_` read as spaces ("Internal Wiki editor"). The stored role name
(`wiki.editor`) never reaches the chat; it stays what matching and the tools
read (`test_role_words.py`).
"""

from collections.abc import Sequence


def role_word(role_slug: str) -> str:
    """The role's own word: its slug with `-` and `_` read as spaces."""
    return role_slug.replace("-", " ").replace("_", " ")


def plain_role_name(system_name: str, role_slug: str) -> str:
    """ "Internal Wiki editor": the system's name, then the role's own word."""
    return f"{system_name} {role_word(role_slug)}"


def joined_with_or(words: Sequence[str]) -> str:
    """ "a", "a or b", "a, b or c"."""
    if len(words) <= 1:
        return "".join(words)
    return f"{', '.join(words[:-1])} or {words[-1]}"


def grouped_suggestions(roles: Sequence[tuple[str, str]]) -> str:
    """The offered roles, one group per system: "Internal Wiki: editor or
    viewer; Mail and Files: user". `roles` are (system name, role slug) pairs,
    best first; a group sits where its system is first seen."""
    groups: dict[str, list[str]] = {}
    for system_name, role_slug in roles:
        groups.setdefault(system_name, []).append(role_word(role_slug))
    return "; ".join(f"{system}: {joined_with_or(words)}" for system, words in groups.items())
