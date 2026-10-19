"""The rows of `list_requestable_roles`, sorted and summed up by code.

Pure: no I/O. The model gets the rows already grouped, template roles first,
and the roles the person holds are said by `held_sentence`, never by the
model, so two replies cannot disagree about what is held.
"""

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
