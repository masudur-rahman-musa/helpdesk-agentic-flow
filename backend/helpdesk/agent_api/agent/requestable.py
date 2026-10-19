"""The rows of `list_requestable_roles`, sorted and summed up by code.

Pure: no I/O. The model gets the rows already grouped, template roles first,
and the roles the person holds are said by `held_sentence`, never by the
model, so two replies cannot disagree about what is held. `mixes_groups`
checks the model kept the two groups apart, and `groups_reply` is the list
code writes when it did not, twice.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

TEMPLATE_GROUP = "template"
OTHER_GROUP = "other"

CRITICAL_SENSITIVITY = "critical"
"""A role on a critical system always needs approval under the policy."""


@dataclass(frozen=True)
class MarkedRow:
    """One role the person may ask for, marked when its system is critical."""

    system_slug: str
    system_name: str
    role_slug: str
    role_name: str
    critical: bool


@dataclass(frozen=True)
class Group:
    label: str
    rows: tuple[MarkedRow, ...]


def _marked(row: Mapping[str, Any]) -> MarkedRow:
    return MarkedRow(
        system_slug=str(row["system_slug"]),
        system_name=str(row["system_name"]),
        role_slug=str(row["role_slug"]),
        role_name=str(row["role_name"]),
        critical=row["system_sensitivity"] == CRITICAL_SENSITIVITY,
    )


def _sorted(rows: Sequence[Mapping[str, Any]]) -> tuple[MarkedRow, ...]:
    marked = (_marked(row) for row in rows)
    return tuple(sorted(marked, key=lambda row: (row.system_slug, row.role_slug)))


def ordered_requestable(rows: Sequence[Mapping[str, Any]]) -> tuple[Group, ...]:
    """The roles not held, template group first, empty groups left out."""
    open_rows = [row for row in rows if not row["already_held"]]
    groups = (
        Group(TEMPLATE_GROUP, _sorted([row for row in open_rows if row["in_my_template"]])),
        Group(OTHER_GROUP, _sorted([row for row in open_rows if not row["in_my_template"]])),
    )
    return tuple(group for group in groups if group.rows)


def held_sentence(rows: Sequence[Mapping[str, Any]]) -> str | None:
    """One sentence naming every held role, or `None` when nothing is held."""
    held = [_marked(row) for row in rows if row["already_held"]]
    if not held:
        return None
    held.sort(key=lambda row: (row.system_slug, row.role_slug))
    names = ", ".join(f"{row.system_name} {row.role_name}" for row in held)
    return f"You already hold: {names}."


MANAGER_WORDS = frozenset({"manager", "approval"})
"""Both must appear in a reply that names a role outside the person's job."""

_SENTENCES = re.compile(r"[.!?\n]+")
_WORDS = re.compile(r"[a-z]+")
CRITICAL_MARK = "(critical, always needs approval)"


def _said(name: str, sentence: str) -> bool:
    return re.search(rf"\b{re.escape(name.lower())}\b", sentence) is not None


def _positions(reply: str, rows: Sequence[MarkedRow]) -> list[int]:
    """The numbers of the sentences naming one of `rows`. A role counts as
    named only where its role name and its system name share the sentence."""
    return [
        number
        for number, sentence in enumerate(_SENTENCES.split(reply.lower()))
        if any(_said(row.role_name, sentence) and _said(row.system_name, sentence) for row in rows)
    ]


def mixes_groups(reply: str, groups: Sequence[Group]) -> bool:
    """Whether `reply` mixes the template group with the other group.

    It does when an other-group role is named before the last named template
    role, or when any other-group role is named without both words "manager"
    and "approval".
    """
    template = [row for group in groups if group.label == TEMPLATE_GROUP for row in group.rows]
    other = [row for group in groups if group.label == OTHER_GROUP for row in group.rows]
    other_at = _positions(reply, other)
    if not other_at:
        return False
    template_at = _positions(reply, template)
    if template_at and min(other_at) < max(template_at):
        return True
    return not frozenset(_WORDS.findall(reply.lower())) >= MANAGER_WORDS


def _listed(rows: Sequence[MarkedRow]) -> str:
    return ", ".join(
        f"{row.system_name} {row.role_name}" + (f" {CRITICAL_MARK}" if row.critical else "")
        for row in rows
    )


_HEADINGS = {
    TEMPLATE_GROUP: "Roles that fit your job",
    OTHER_GROUP: "Other roles you can ask for, which need your manager's approval",
}


def groups_reply(groups: Sequence[Group]) -> str:
    """The roles as code writes them: template group first, then the other
    group with the manager approval line, one line per group."""
    return "\n".join(f"{_HEADINGS[group.label]}: {_listed(group.rows)}." for group in groups)
