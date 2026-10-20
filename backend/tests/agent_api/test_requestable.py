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
        one sentence naming each held role in plain words: the
        ``system_name`` then the ``role_slug`` with ``-`` and ``_`` read as
        spaces, or ``None`` when no row is held. ``groups_reply`` names roles
        the same way.

Nothing here needs a database, a socket or a graph. The rows are obviously fake.
"""

from collections.abc import Mapping
from typing import Any

import pytest

from helpdesk.agent_api.agent.requestable import (
    OTHER_GROUP,
    TEMPLATE_GROUP,
    Group,
    groups_reply,
    held_sentence,
    mixes_groups,
    ordered_requestable,
)
from helpdesk.agent_api.agent.role_words import plain_role_name


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
    template role is named in the sentence by its plain name and is in no group."""
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
    assert "Fake One fake held" in sentence
    assert "Fake Held Role" not in sentence
    assert "fake free" not in sentence


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


# --- the two groups kept apart -------------------------------------------------

MANAGER_LINE = "need your manager's approval"
CRITICAL_MARK = "(critical, always needs approval)"


def _groups(*, extra_template: bool = False) -> tuple[Group, ...]:
    """Template: wiki reader (and wiki writer when asked). Other: crm editor."""
    rows = [_row("wiki", "reader", template=True), _row("crm", "editor")]
    if extra_template:
        rows.append(_row("wiki", "writer", template=True))
    return ordered_requestable(rows)


def test_template_then_other_with_manager_approval_passes() -> None:
    """Catches a check that flags a good reply (template group first, other
    group after, manager approval said): a too-eager check would retry every
    correct list and end on the fallback."""
    reply = (
        "Roles that fit your job: Fake Wiki Fake Reader Role. "
        "Other roles need your manager's approval: Fake Crm Fake Editor Role."
    )

    assert not mixes_groups(reply, _groups())


def test_names_match_without_regard_to_case() -> None:
    """Catches a name match that is case sensitive: a lower-case reply that
    keeps the groups apart must pass, and the same words out of order must still
    fail, so the case rule is shown in both directions."""
    good = (
        "roles that fit your job: fake wiki fake reader role. "
        "other roles need your manager's approval: fake crm fake editor role."
    )
    swapped = (
        "other roles need your manager's approval: fake crm fake editor role. "
        "roles that fit your job: fake wiki fake reader role."
    )

    assert not mixes_groups(good, _groups())
    assert mixes_groups(swapped, _groups())


def test_other_role_before_a_template_role_is_mixed() -> None:
    """Catches a check that only looks for the manager words: the manager
    approval line is present, so only the order rule can flag the other-group
    role coming before the template role."""
    reply = (
        "Other roles need your manager's approval: Fake Crm Fake Editor Role. "
        "Roles that fit your job: Fake Wiki Fake Reader Role."
    )

    assert mixes_groups(reply, _groups())


def test_other_role_between_two_template_roles_is_mixed() -> None:
    """Catches an order rule that compares with the FIRST named template role
    instead of the LAST: the other role sits after the first template role but
    before the second, and must still be flagged."""
    reply = (
        "Fake Wiki Fake Reader Role fits your job. "
        "Fake Crm Fake Editor Role needs your manager's approval. "
        "Fake Wiki Fake Writer Role fits your job."
    )

    assert mixes_groups(reply, _groups(extra_template=True))


@pytest.mark.parametrize(
    "tail",
    [
        "Other roles: Fake Crm Fake Editor Role.",
        "Other roles need your manager: Fake Crm Fake Editor Role.",
        "Other roles need approval: Fake Crm Fake Editor Role.",
    ],
    ids=["neither word", "manager only", "approval only"],
)
def test_other_role_without_manager_approval_is_mixed(tail: str) -> None:
    """Catches a check that accepts one of the two words, or neither: the
    groups are in the right order in every row, so only the manager-approval
    rule (both words) can flag it."""
    reply = f"Roles that fit your job: Fake Wiki Fake Reader Role. {tail}"

    assert mixes_groups(reply, _groups())


def test_role_name_on_another_system_does_not_count() -> None:
    """Catches a match on the role name alone: "Fake Reader Role" here sits on a
    system that is not the template role's system, so it is not the template
    role. If it counted, it would sit after the other role and flag the reply."""
    reply = (
        "Other roles need your manager's approval: Fake Crm Fake Editor Role. "
        "Fake Billing Fake Reader Role is a different thing."
    )

    assert not mixes_groups(reply, _groups())


def test_only_template_roles_need_no_manager_words() -> None:
    """Catches a check that asks for the manager words on every reply: no
    other-group role is named, so a reply with template roles only passes even
    though the other group exists."""
    reply = "Roles that fit your job: Fake Wiki Fake Reader Role."

    assert "manager" not in reply
    assert not mixes_groups(reply, _groups())


def test_groups_reply_lists_template_first_and_says_manager_approval() -> None:
    """Catches a fallback text that lists the groups in one run, in the wrong
    order, without the manager line, with the critical mark on the wrong row, or
    that still prints the stored role name ("Fake Mine-A Role") instead of the
    plain name: it pins both headings, the order, the names and the mark."""
    rows = [
        _row("zeta", "other-low"),
        _row("crm", "other-crit", sensitivity="critical"),
        _row("wiki", "mine-a", template=True),
        _row("wiki", "mine-b", template=True),
    ]
    groups = ordered_requestable(rows)

    text = groups_reply(groups)

    assert "Roles that fit your job:" in text
    assert "Other roles you can ask for, which need your manager's approval:" in text
    template_end = max(text.index("Fake Wiki fake mine a"), text.index("Fake Wiki fake mine b"))
    assert template_end < text.index("Other roles you can ask for")
    assert text.index("Other roles you can ask for") < text.index("Fake Crm fake other crit")
    for group in groups:
        for row in group.rows:
            assert plain_role_name(row.system_name, row.role_slug) in text
            assert row.role_name not in text
    assert text.count(CRITICAL_MARK) == 1
    crit_at = text.index("Fake Crm fake other crit")
    assert crit_at < text.index(CRITICAL_MARK) < text.index("Fake Zeta fake other low")


def test_groups_reply_passes_its_own_check() -> None:
    """Catches a fallback the check would reject: the text code writes must keep
    the groups apart and say manager approval, or the last resort would itself
    be a mixed list."""
    groups = _groups(extra_template=True)

    assert not mixes_groups(groups_reply(groups), groups)


def test_groups_reply_with_only_other_roles() -> None:
    """Catches a fallback that prints an empty template heading, or drops the
    manager words, when the person has no template roles left: one line, the
    other group only."""
    groups = ordered_requestable([_row("crm", "editor")])

    text = groups_reply(groups).strip()

    assert "\n" not in text
    assert MANAGER_LINE in text
    assert "Fake Crm fake editor" in text
    assert "Fake Editor Role" not in text
    assert "Roles that fit your job" not in text


def test_held_roles_use_plain_names() -> None:
    """Catches a held sentence built from the stored role name ("wiki.editor")
    or the slug alone: a held `wiki.editor` on "Internal Wiki" must read as the
    plain name, and two held roles keep the slug order, comma separated."""
    one = {
        **_row("wiki", "editor", held=True),
        "system_name": "Internal Wiki",
        "role_slug": "editor",
        "role_name": "wiki.editor",
    }
    two = {
        **_row("wiki", "viewer", held=True),
        "system_name": "Internal Wiki",
        "role_slug": "payroll-viewer",
        "role_name": "wiki.viewer",
    }

    assert held_sentence([one]) == "You already hold: Internal Wiki editor."
    assert held_sentence([two, one]) == (
        "You already hold: Internal Wiki editor, Internal Wiki payroll viewer."
    )


def test_a_plain_name_in_a_reply_counts_as_named() -> None:
    """Catches a role check that still looks for the stored role name: the
    stored names here ("mail.user", "wiki.viewer") never appear in the reply, so
    only the plain names ("Mail and Files user", "Internal Wiki viewer") can
    show that the other-group role comes before the template role. The manager
    words are in the reply, so only the order rule can flag it."""
    mail = {
        **_row("mail", "user"),
        "system_name": "Mail and Files",
        "role_slug": "user",
        "role_name": "mail.user",
    }
    wiki = {
        **_row("wiki", "viewer", template=True),
        "system_name": "Internal Wiki",
        "role_slug": "viewer",
        "role_name": "wiki.viewer",
    }
    groups = ordered_requestable([wiki, mail])
    mixed = "Mail and Files user needs your manager's approval. Internal Wiki viewer fits your job."
    apart = "Internal Wiki viewer fits your job. Mail and Files user needs your manager's approval."

    assert mixes_groups(mixed, groups)
    assert not mixes_groups(apart, groups)


def test_the_old_role_and_system_words_still_count_as_named() -> None:
    """Catches a check that swaps the old rule for the plain name: a reply
    that says the stored role name and the system name ("Fake Wiki Fake Reader
    Role") must still be placed, so the other-group role named first is flagged."""
    reply = (
        "Fake Crm Fake Editor Role needs your manager's approval. "
        "Fake Wiki Fake Reader Role fits your job."
    )

    assert mixes_groups(reply, _groups())
