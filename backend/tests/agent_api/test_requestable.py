"""The requestable rows, sorted and summed up by pure code.

Expected implementation surface (names fixed by this test file):

``agent_api.agent.requestable`` (PURE: no I/O, no framework)
    ``TEMPLATE_GROUP = "template"`` and ``OTHER_GROUP = "other"``.
    ``@dataclass(frozen=True) class MarkedRow`` with ``system_slug``,
        ``system_name``, ``role_slug``, ``role_name`` (all ``str``) and
        ``critical: bool`` (True exactly when ``system_sensitivity`` is
        ``"critical"``).
    ``@dataclass(frozen=True) class Group`` with ``label: str`` and
        ``rows: tuple[MarkedRow, ...]``.
    ``def ordered_requestable(rows: Sequence[Mapping[str, Any]]) -> tuple[Group, ...]``
        takes the ``roles`` rows of ``list_requestable_roles`` as they come
        from the tool. Held rows (``already_held``) are left out. Returns the
        ``template`` group first (``in_my_template`` and not held), then the
        ``other`` group (not held). A group with no rows is left out. Inside a
        group the rows sort by system slug, then role slug.
    ``def held_sentence(rows: Sequence[Mapping[str, Any]]) -> str | None``
        one sentence naming each held role by its ``role_name`` (and its
        ``system_name``), or ``None`` when no row is held.

Nothing here needs a database, a socket or a graph. The rows are obviously fake.
"""

from collections.abc import Mapping
from typing import Any

from helpdesk.agent_api.agent.requestable import (
    OTHER_GROUP,
    TEMPLATE_GROUP,
    held_sentence,
    ordered_requestable,
)


def _row(
    system: str,
    role: str,
    *,
    template: bool = False,
    held: bool = False,
    sensitivity: str = "low",
) -> Mapping[str, Any]:
    return {
        "system_slug": f"fake-{system}",
        "system_name": f"Fake {system.title()}",
        "system_sensitivity": sensitivity,
        "role_slug": f"fake-{role}",
        "role_name": f"Fake {role.title()} Role",
        "privilege": "read",
        "default_expiry_days": 365,
        "in_my_template": template,
        "already_held": held,
    }


def test_template_roles_come_first() -> None:
    """Catches rows left in tool order, or sorted by system alone: the other
    role sorts first by name (aaa before zzz), so only the template-first rule
    puts the template role at the head of the list the model reads."""
    rows = [_row("aaa", "other", template=False), _row("zzz", "mine", template=True)]

    groups = ordered_requestable(rows)

    assert [group.label for group in groups] == [TEMPLATE_GROUP, OTHER_GROUP]
    assert TEMPLATE_GROUP == "template"
    assert OTHER_GROUP == "other"
    assert [row.role_slug for row in groups[0].rows] == ["fake-mine"]
    assert [row.role_slug for row in groups[1].rows] == ["fake-other"]


def test_rows_inside_a_group_sort_by_system_then_role() -> None:
    """Catches a sort that ignores the second key or the first: the same system
    must order its roles, and systems must order before roles, so the same
    tool rows always give the same reply order."""
    rows = [
        _row("beta", "b"),
        _row("alpha", "z"),
        _row("beta", "a"),
        _row("alpha", "y"),
    ]

    (group,) = ordered_requestable(rows)

    assert [(row.system_slug, row.role_slug) for row in group.rows] == [
        ("fake-alpha", "fake-y"),
        ("fake-alpha", "fake-z"),
        ("fake-beta", "fake-a"),
        ("fake-beta", "fake-b"),
    ]


def test_held_roles_are_only_in_the_held_sentence() -> None:
    """Catches a held role that also shows as requestable, the contradiction in
    the live chat (a role in both the held and the requestable list): a held
    template role is named in the sentence by its role name and is in no group."""
    rows = [
        _row("one", "held", template=True, held=True),
        _row("two", "free", template=True),
        _row("three", "free", template=False),
    ]

    groups = ordered_requestable(rows)
    sentence = held_sentence(rows)

    listed = {row.role_slug for group in groups for row in group.rows}
    assert "fake-held" not in listed
    assert listed == {"fake-free"}
    assert sentence is not None
    assert "Fake Held Role" in sentence
    assert "Fake Free Role" not in sentence


def test_critical_rows_are_marked() -> None:
    """Catches a mark taken from the wrong field, or set on every row: in one
    group, the critical system's row is marked and the low one is not, and a
    "high" system is not marked either (only critical needs approval wording)."""
    rows = [
        _row("a", "crit", sensitivity="critical"),
        _row("b", "low", sensitivity="low"),
        _row("c", "high", sensitivity="high"),
    ]

    (group,) = ordered_requestable(rows)

    marks = {row.role_slug: row.critical for row in group.rows}
    assert marks == {"fake-crit": True, "fake-low": False, "fake-high": False}


def test_no_rows_gives_no_held_sentence() -> None:
    """Catches a sentence written even when nothing is held ("You hold no
    roles" would be an invented fact) and groups that come back as empty shells:
    empty rows give no sentence and no groups, and rows with nothing held also
    give no sentence."""
    assert held_sentence([]) is None
    assert ordered_requestable([]) == ()
    assert held_sentence([_row("a", "free")]) is None
