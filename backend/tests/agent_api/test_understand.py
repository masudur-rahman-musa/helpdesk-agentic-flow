"""Understanding the message before the intent: spelling and picks.

Surface under test (written before it exists), all in
`helpdesk.agent_api.agent.understand`: `SystemWords`, `Catalogue`,
`fix_spelling(message, catalogue)` and `read_pick(message, options)`.

Cost: nothing. Pure functions on plain objects; no service, no model.
"""

import pytest

from helpdesk.agent_api.agent.understand import Catalogue, SystemWords, fix_spelling, read_pick

CATALOGUE = Catalogue(
    systems=(
        SystemWords(slug="internal-wiki", name="Internal Wiki", role_slugs=("editor", "viewer")),
        SystemWords(
            slug="data-warehouse", name="Data Warehouse", role_slugs=("engineer", "analyst")
        ),
        SystemWords(slug="mail-and-files", name="Mail and Files", role_slugs=("user",)),
        SystemWords(slug="crm", name="CRM", role_slugs=("editor", "viewer")),
        SystemWords(slug="vpn", name="VPN", role_slugs=("user",)),
    ),
    people=("Jon Smith", "Dana Fakename"),
)

WIKI_OPTIONS = ("Internal Wiki editor", "Mail and Files user")


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        pytest.param(
            "give me access to dta warhous eng",
            "give me access to Data Warehouse engineer",
            id="warehouse-engineer",
        ),
        pytest.param("wiki editr", "Internal Wiki editor", id="wiki-editor"),
        pytest.param(
            "I need wikI editr for 5 days",
            "I need Internal Wiki editor for 5 days",
            id="inside-a-sentence",
        ),
    ],
)
def test_typos_become_the_catalogue_name(message: str, expected: str) -> None:
    """Catches a message that reaches the model with its typos ("dta warhous
    eng"), so the role is never found: the run of words must become the one
    system and role it names, spelt as `plain_role_name` spells it, and the words
    around the run must stay as typed."""
    assert fix_spelling(message, CATALOGUE) == expected


@pytest.mark.parametrize(
    "message",
    [
        pytest.param("I need it for 5 days to check numbers", id="reason-words"),
        pytest.param("on", id="two-letter-word"),
        pytest.param("I need wiki", id="system-without-a-role"),
        pytest.param("editr access please", id="role-without-a-system"),
        pytest.param("", id="empty"),
    ],
)
def test_plain_words_are_left_alone(message: str) -> None:
    """Unhappy path. Catches a fixer that bends ordinary words toward the
    catalogue ("days" to "data", "on" to a prefix match) or turns a system word
    alone, or a role word alone, into a full role name that was never asked
    for. A run is replaced only when it names one system and one role of it."""
    assert fix_spelling(message, CATALOGUE) == message


def test_a_person_name_is_fixed() -> None:
    """Catches a fixer that knows systems but not people: "jon smyth" reads at
    0.8 against "Jon Smith" and becomes the display name; "the team" names no
    one and comes back unchanged. Both go through the same catalogue, so only
    the people list can make the first change."""
    assert fix_spelling("access for jon smyth", CATALOGUE) == "access for Jon Smith"
    assert fix_spelling("the team", CATALOGUE) == "the team"
    without_people = Catalogue(systems=CATALOGUE.systems)
    assert fix_spelling("access for jon smyth", without_people) == "access for jon smyth"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        pytest.param("1", WIKI_OPTIONS[0], id="one"),
        pytest.param(" 1 ", WIKI_OPTIONS[0], id="one-with-spaces"),
        pytest.param("first", WIKI_OPTIONS[0], id="first"),
        pytest.param("the first one", WIKI_OPTIONS[0], id="the-first-one"),
        pytest.param("2", WIKI_OPTIONS[1], id="two"),
        pytest.param("second", WIKI_OPTIONS[1], id="second"),
        pytest.param("the second one", WIKI_OPTIONS[1], id="the-second-one"),
        pytest.param("last", WIKI_OPTIONS[1], id="last"),
        pytest.param("the last one", WIKI_OPTIONS[1], id="the-last-one"),
        pytest.param("mail", WIKI_OPTIONS[1], id="word-of-one-option"),
        pytest.param("Mail", WIKI_OPTIONS[1], id="word-in-capitals"),
        pytest.param("mail and files", WIKI_OPTIONS[1], id="several-words-of-one-option"),
    ],
)
def test_picks_by_number_ordinal_and_word(message: str, expected: str) -> None:
    """Catches a pick that is left to the model: a number, an ordinal (with or
    without "the ... one") or a word sitting in one option alone must come back
    as that option's name, so "last" is the second of two and not the first."""
    assert read_pick(message, WIKI_OPTIONS) == expected


def test_third_and_last_follow_the_option_count() -> None:
    """Catches an ordinal table fixed at two options: with three options "third"
    and "last" are both option 3, and "3" is option 3."""
    options = ("A one", "B two", "C three")
    assert read_pick("third", options) == "C three"
    assert read_pick("last", options) == "C three"
    assert read_pick("3", options) == "C three"


@pytest.mark.parametrize(
    ("message", "options"),
    [
        pytest.param("3", WIKI_OPTIONS, id="number-past-the-end"),
        pytest.param("0", WIKI_OPTIONS, id="zero"),
        pytest.param("the third one", WIKI_OPTIONS, id="ordinal-past-the-end"),
        pytest.param("wiki", ("Internal Wiki editor", "Internal Wiki viewer"), id="two-match"),
        pytest.param("banana", WIKI_OPTIONS, id="matches-nothing"),
        pytest.param("mail banana", WIKI_OPTIONS, id="one-word-matches-one-does-not"),
        pytest.param("1", (), id="number-no-options"),
        pytest.param("first", (), id="ordinal-no-options"),
        pytest.param("mail", (), id="word-no-options"),
        pytest.param("", WIKI_OPTIONS, id="empty"),
    ],
)
def test_not_a_pick(message: str, options: tuple[str, ...]) -> None:
    """Unhappy path. Catches a pick that guesses: a number past the last option,
    a word that sits in two options, a word that sits in none (or only partly),
    and any message when no options were offered must give None, so the model
    reads the message as a normal one."""
    assert read_pick(message, options) is None
