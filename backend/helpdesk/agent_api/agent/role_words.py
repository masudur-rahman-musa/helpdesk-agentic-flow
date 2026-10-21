"""Roles in plain words, the one rule every code-written chat line uses.

Pure: no I/O. The plain-name rule lives in `shared/access/role_names.py`, so
notifications use the same one. A role reads as its system's name, then the role slug with `-`
and `_` read as spaces ("Internal Wiki editor"). The stored role name
(`wiki.editor`) never reaches the chat; it stays what matching and the tools
read (`test_role_words.py`).
"""

from collections.abc import Sequence
from typing import Any

from helpdesk.shared.access.role_names import plain_role_name, role_word

__all__ = [
    "grouped_suggestions",
    "joined_with_or",
    "plain_role_name",
    "role_word",
    "with_plain_role_names",
]


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


def with_plain_role_names(data: Any) -> Any:
    """A copy of `data` where every dict holding `system_name` and `role_name`
    has its `role_name` made plain, at any depth.

    The slug is used when the row has one; else the part of `role_name` after
    its last "." (review rows carry no slug). The writer only sees plain
    names, so a stored value like `wiki.editor` never reaches a reply. The
    stored result is left as it is (`test_reply_names_roles_plainly.py`).
    """
    if isinstance(data, dict):
        rewritten = {key: with_plain_role_names(value) for key, value in data.items()}
        system_name = data.get("system_name")
        role_name = data.get("role_name")
        if isinstance(system_name, str) and isinstance(role_name, str):
            slug = data.get("role_slug")
            if not isinstance(slug, str) or not slug:
                slug = role_name.rsplit(".", 1)[-1]
            rewritten["role_name"] = plain_role_name(system_name, slug)
        return rewritten
    if isinstance(data, list | tuple):
        return [with_plain_role_names(item) for item in data]
    return data
