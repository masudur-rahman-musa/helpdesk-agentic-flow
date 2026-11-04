"""What this chat has found: one record, read by every check.

The record holds the ok results kept from earlier turns, then this turn's ok
results. Holding a change, naming its ids, the written question and the reply
check all read it, so no check knows a fact another one refuses. Ids and
names count from anywhere in the chat; decision and status words still need
this turn's results, since a status can change, so those checks read the
turn's own results and not this record.

Kept results come from tool answers the server stored, never from the model
or the chat text, so an id only written in the chat is in no row here.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Self

from helpdesk.agent_api.agent.matching import fold
from helpdesk.agent_api.agent.role_words import plain_role_name, with_plain_role_names
from helpdesk.agent_api.agent.state import ToolResult

ID_SUFFIX = "_id"
"""An argument or row field whose name ends so holds an id."""


@dataclass(frozen=True)
class Fact:
    """One thing the written question must show, as code knows it."""

    text: str
    number: bool = False
    """Found as the digits standing alone, so "30 Tage" and "30 jours" both
    show 30 and "130" does not."""

    def found_in(self, question: str) -> bool:
        """Whether the question shows this fact, ignoring case and accents."""
        if self.number:
            return re.search(rf"(?<!\d){re.escape(self.text)}(?!\d)", question) is not None
        return fold(self.text) in fold(question)


@dataclass(frozen=True)
class Part:
    """One argument as the line shows it, and what the question must copy of it."""

    shown: str
    facts: tuple[Fact, ...] = ()


def stripped(value: object) -> str:
    """The value without spaces at its ends when it is text, else empty."""
    return value.strip() if isinstance(value, str) else ""


def _row_role(row: Mapping[str, Any]) -> str:
    """The row's role in plain words, or empty when it names no role."""
    system, slug = stripped(row.get("system_name")), stripped(row.get("role_slug"))
    if system and slug:
        return plain_role_name(system, slug)
    return stripped(row.get("role_name"))


def _ids(row: Mapping[str, Any]) -> list[str]:
    return [key for key, value in row.items() if key.endswith(ID_SUFFIX) and stripped(value)]


def _name_row(row: Mapping[str, Any], names: dict[str, Part]) -> None:
    """Name every id the row holds: by its `<x>_name`, else by the row's
    `name` when the row has one id of its own, else the row's first id by its
    request number and role. An id with none of these stays unnamed."""
    ids = _ids(row)
    for place, key in enumerate(ids):
        own = stripped(row.get(f"{key.removesuffix(ID_SUFFIX)}_name"))
        if not own and len(ids) == 1:
            own = stripped(row.get("name"))
        if own:
            names.setdefault(stripped(row[key]), Part(own, (Fact(own),)))
            continue
        if place:
            continue
        number, role = stripped(row.get("request_number")), _row_role(row)
        listed = f"{number} {role}".strip()
        if listed:
            names.setdefault(stripped(row[key]), Part(listed, (Fact(number or role),)))


def _all_rows(value: Any) -> list[Mapping[str, Any]]:
    """Every mapping at any depth, outer ones first."""
    if isinstance(value, Mapping):
        return [value, *(row for child in value.values() for row in _all_rows(child))]
    if isinstance(value, list):
        return [row for item in value for row in _all_rows(item)]
    return []


def _found_names(rows: Sequence[Mapping[str, Any]]) -> dict[str, Part]:
    """Each id the rows name, under any key, with role names in plain words;
    the first row to name an id wins. Found by shape, so a new lookup needs
    no code here."""
    names: dict[str, Part] = {}
    for row in rows:
        _name_row(row, names)
    return names


@dataclass(frozen=True)
class Found:
    """The chat's found facts. Build it with `of`."""

    results: tuple[ToolResult, ...] = ()
    """The ok results, kept ones first, then this turn's."""
    names: dict[str, Part] = field(default_factory=dict)
    """How each found id is shown."""
    people: dict[str, str] = field(default_factory=dict)
    """Each found person's name by their id: a row whose one id is `user_id`."""
    roles: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Each found role row by its id: a row with `role_id`, `name` and `system`."""
    rows: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    """Each found row by its first id, the record that id is about."""

    @classmethod
    def of(cls, kept: Sequence[ToolResult], results: Sequence[ToolResult]) -> Self:
        ok = tuple(result for result in (*kept, *results) if result.ok)
        rows = [row for result in ok for row in _all_rows(with_plain_role_names(result.payload))]
        people: dict[str, str] = {}
        roles: dict[str, dict[str, Any]] = {}
        by_id: dict[str, Mapping[str, Any]] = {}
        for row in rows:
            ids = _ids(row)
            if not ids:
                continue
            by_id.setdefault(stripped(row[ids[0]]), row)
            name = stripped(row.get("name"))
            if ids == ["user_id"] and name:
                people.setdefault(stripped(row["user_id"]), name)
            if "role_id" in ids and name and stripped(row.get("system")):
                roles.setdefault(stripped(row["role_id"]), dict(row))
        return cls(ok, _found_names(rows), people, roles, by_id)
