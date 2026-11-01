"""The agent in German, French and Spanish: answers, closing lines, names.

Cost and isolation. Pure: plain values and one tiny fake directory. No
Postgres, no gateway, no socket, no waiting.

Surface the implementer must provide: `helpdesk.agent_api.agent.answer_words`
(`Language`, `guess_language`); in `confirmation.py` the `language` keyword on
`summary_reply` and `change_summary`, the `CHANGE_LINES` and `FILE_LINES`
tables keyed by `Language`, and `next_step(results, language)`; the reject
reason check in `tool_args.py`; accent-free `matching.normalise` and
`loop_tools.find_person`.
"""

import asyncio
import uuid
from collections.abc import Mapping, Sequence

import pytest
from pydantic import ValidationError

from helpdesk.agent_api.agent import confirmation
from helpdesk.agent_api.agent.answer_words import Language, guess_language
from helpdesk.agent_api.agent.confirmation import (
    Answer,
    change_summary,
    next_step,
    read_answer,
    summary_reply,
)
from helpdesk.agent_api.agent.loop_tools import PersonRow, find_person
from helpdesk.agent_api.agent.matching import normalise
from helpdesk.agent_api.agent.state import ResolvedEntities, ToolResult, ValidatedCall
from helpdesk.agent_api.agent.tool_args import RejectRequestArgs

OTHER_LANGUAGES = [Language.GERMAN, Language.FRENCH, Language.SPANISH]

# Obviously fake ids, tests only.
USER_ID = uuid.UUID("44444444-4444-5444-8444-444444444444")
ROLE_ID = uuid.UUID("55555555-5555-5555-8555-555555555555")
SYSTEM_ID = uuid.UUID("66666666-6666-5666-8666-666666666666")

# --- reading the answer -----------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    ["ja", "oui", "sí", "si", "Ja, bitte", "Oui, d'accord", "Sí, claro"],
)
def test_yes_words_in_german_french_and_spanish_read_as_yes(message: str) -> None:
    """Catches a reader with only English yes words: a French "oui" would be
    read as other and the confirmation would ask again instead of filing."""
    assert read_answer(message) is Answer.YES


@pytest.mark.parametrize("message", ["nein", "non", "no", "Nein danke"])
def test_no_words_in_german_french_and_spanish_read_as_no(message: str) -> None:
    """Catches a reader with only English no words: "nein" or "non" would be
    read as other, so a refusal is treated as a new question."""
    assert read_answer(message) is Answer.NO


@pytest.mark.parametrize("message", ["sí", "si", "SÍ", "Sí.", "sÍ!"])
def test_an_accented_yes_matches_its_plain_form(message: str) -> None:
    """Catches a word list matched with the accent kept: only one of "sí" and
    "si" would read as yes, and the other would file nothing."""
    assert read_answer(message) is Answer.YES


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("oui mais je veux changer le rôle", Answer.OTHER),
        ("ja bitte mach das jetzt", Answer.YES),
    ],
)
def test_a_foreign_yes_keeps_the_length_caps(message: str, expected: Answer) -> None:
    """Catches a foreign yes list that skips the five-word cap: the seven-word
    French correction would file the old request instead of being heard, and a
    cap of 4 would refuse the five-word German yes."""
    assert read_answer(message) is expected


@pytest.mark.parametrize("message", ["jamais", "simple", "nonsense", "nein1", "sinceramente"])
def test_a_word_that_only_starts_like_a_yes_is_other(message: str) -> None:
    """Catches a prefix match on the foreign words ("ja" in "jamais", "si" in
    "simple", "no" in "nonsense"), which would file on an unrelated message."""
    assert read_answer(message) is Answer.OTHER


# --- guessing the language --------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Ich brauche bitte Zugang zum CRM", Language.GERMAN),
        ("Je voudrais un accès au CRM s'il vous plaît", Language.FRENCH),
        ("Necesito acceso al CRM por favor", Language.SPANISH),
        ("I need CRM access", Language.ENGLISH),
    ],
)
def test_the_language_is_guessed_from_the_persons_words(message: str, expected: Language) -> None:
    """Catches a guess that always answers English, or that mixes up the three
    other languages, so the closing lines would come out in the wrong tongue."""
    assert guess_language([message]) is expected


@pytest.mark.parametrize("message", ["", "CRM", "ok"])
def test_an_unclear_message_falls_back_to_english(message: str) -> None:
    """Catches a guess that raises on an empty message or picks a language when
    no word points to one: the person would get lines they did not write in."""
    assert guess_language([message]) is Language.ENGLISH


def test_an_unclear_message_now_uses_the_earlier_german_one() -> None:
    """Catches a guess that reads only the last message: after a German request
    a bare "CRM" would flip the closing line back to English."""
    said = ["Ich brauche bitte Zugang zum CRM", "CRM"]
    assert guess_language(said) is Language.GERMAN


def test_the_newest_clear_message_wins_over_an_older_one() -> None:
    """Catches a guess that pools or prefers the oldest message: a person who
    wrote German first and switched to French must get French."""
    said = ["Ich brauche bitte Zugang zum CRM", "Je voudrais un accès au CRM s'il vous plaît"]
    assert guess_language(said) is Language.FRENCH


def test_no_messages_at_all_is_english() -> None:
    """Catches a guess that raises on an empty history instead of falling back."""
    said: Sequence[str] = []
    assert guess_language(said) is Language.ENGLISH


# --- the lines the code writes ----------------------------------------------------


def _classified() -> ToolResult:
    return ToolResult(
        tool="classify_access_request",
        ok=True,
        payload={
            "decision": "auto",
            "risk_class": "low",
            "reasons": [],
            "required_approvers": [],
            "effective_expiry_days": 10,
        },
    )


def _resolved() -> ResolvedEntities:
    return ResolvedEntities(
        system_id=SYSTEM_ID,
        system_slug="internal-wiki",
        system_name="Internal Wiki",
        role_id=ROLE_ID,
        role_slug="editor",
        role_name="Internal Wiki editor",
    )


def _held() -> tuple[ValidatedCall, ...]:
    return (
        ValidatedCall(
            tool="classify_access_request",
            arguments={"role_id": str(ROLE_ID), "requested_expiry_days": 99},
        ),
        ValidatedCall(
            tool="create_access_request",
            arguments={
                "target_user_id": str(USER_ID),
                "role_id": str(ROLE_ID),
                "justification": "Obviously fake reason.",
                "requested_expiry_days": None,
                "citations": [],
                "idempotency_key": "fake-key-1",
            },
        ),
    )


def test_the_english_lines_are_the_exact_old_words() -> None:
    """Catches English lines reworded by the change: the English table must hold
    the old words exactly, so no English chat changes."""
    assert confirmation.CHANGE_LINES[Language.ENGLISH] == "Say yes to go ahead, or no to stop."
    assert (
        confirmation.FILE_LINES[Language.ENGLISH] == "Say yes to file, or tell me what to change."
    )


def test_every_language_has_its_own_lines() -> None:
    """Catches a table that fills a language with the English text, or reuses one
    translation for two languages: all four lines of each kind must differ."""
    changes = {confirmation.CHANGE_LINES[language] for language in Language}
    files = {confirmation.FILE_LINES[language] for language in Language}
    assert len(changes) == len(Language)
    assert len(files) == len(Language)


def test_the_change_line_is_in_the_persons_language() -> None:
    """Catches a change summary that ignores the language and always ends in the
    English line, even in the middle of a German chat."""
    calls = [ValidatedCall(tool="reset_own_password", arguments={"idempotency_key": "fake-key"})]
    no_results: tuple[ToolResult, ...] = ()

    for language in OTHER_LANGUAGES:
        summary = change_summary(calls, no_results, ResolvedEntities(), language=language)
        assert summary.endswith(confirmation.CHANGE_LINES[language])
        assert confirmation.CHANGE_LINE not in summary

    english = change_summary(calls, no_results, ResolvedEntities(), language=Language.ENGLISH)
    assert english.endswith("Say yes to go ahead, or no to stop.")
    assert change_summary(calls, no_results, ResolvedEntities()) == english


def test_the_file_line_and_next_step_are_in_the_persons_language() -> None:
    """Catches an access summary that translates only one of its two closing
    lines, or leaves the "If you say yes" sentence English in a German chat."""
    results = (_classified(),)
    english_next = next_step(results, Language.ENGLISH)
    assert english_next == "If you say yes, it is granted right away."

    for language in OTHER_LANGUAGES:
        reply = summary_reply(_resolved(), results, _held(), language=language)
        translated_next = next_step(results, language)
        assert translated_next != english_next
        assert confirmation.FILE_LINES[language] in reply
        assert translated_next in reply
        assert reply.endswith(translated_next)
        assert confirmation.FILE_LINE not in reply
        assert english_next not in reply


def test_the_default_language_keeps_the_english_summary() -> None:
    """Catches a changed default: with no language given the summary must stay
    exactly the English one every old caller and test expects."""
    results = (_classified(),)
    reply = summary_reply(_resolved(), results, _held())
    assert reply.endswith(f"{confirmation.FILE_LINE} {next_step(results, Language.ENGLISH)}")


# --- a reject's reason ------------------------------------------------------------


def _reject(reason: str) -> RejectRequestArgs:
    return RejectRequestArgs(
        request_id=uuid.UUID(int=7), reason=reason, idempotency_key="fake-key-1"
    )


@pytest.mark.parametrize(
    "reason",
    ["oui", "Ja", "sí", "si", "yes", "no", "non", "nein", "Oui.", "  ja  ", "yes\n", "oui\r\n"],
)
def test_a_reject_whose_reason_is_a_yes_or_no_word_is_refused(reason: str) -> None:
    """Catches a reject that saves the yes word itself as its reason (the French
    "oui" case): only a check over all four languages, accents off and
    whitespace trimmed, refuses every one of these."""
    with pytest.raises(ValidationError):
        _reject(reason)


@pytest.mark.parametrize(
    "reason", ["no budget left this quarter", "pas de budget", "ja, aber nein"]
)
def test_a_reject_with_a_real_reason_is_accepted(reason: str) -> None:
    """Catches a reason check that refuses anything starting with or holding a
    yes or no word: only a reason made of nothing but those words is bad."""
    assert _reject(reason).reason == reason


# --- names with accents -----------------------------------------------------------


class _Directory:
    def __init__(self, people: Sequence[PersonRow]) -> None:
        self._people = people

    async def role_ids(self) -> Mapping[tuple[str, str], uuid.UUID]:
        return {}

    async def people(self) -> Sequence[PersonRow]:
        return self._people


def _person(number: int, name: str) -> PersonRow:
    return PersonRow(id=uuid.UUID(int=number), display_name=name, department="Sales")


def test_a_person_search_ignores_accents() -> None:
    """Catches a search that compares accents: "Kristian Muhle" would not find
    "Kristian Mühle", and the other way round for "José Núñez"."""
    directory = _Directory([_person(1, "Kristian Mühle"), _person(2, "Jose Nunez")])
    found = asyncio.run(find_person("Kristian Muhle", directory=directory))
    assert [row["name"] for row in found] == ["Kristian Mühle"]

    found = asyncio.run(find_person("José Núñez", directory=directory))
    assert [row["name"] for row in found] == ["Jose Nunez"]


def test_a_person_search_with_accents_on_both_sides_finds_the_person() -> None:
    """Catches a fix that strips accents on the typed side only: both sides
    carry accents here and must still meet."""
    directory = _Directory([_person(1, "Kristian Mühle")])
    found = asyncio.run(find_person("Kristián Mühle", directory=directory))
    assert [row["name"] for row in found] == ["Kristian Mühle"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [("Mühle", "muhle"), ("Straße", "strasse"), ("Billing-Admin", "billing admin")],
)
def test_normalise_takes_accents_off(text: str, expected: str) -> None:
    """Catches a normaliser that keeps accents or leaves "ß" alone, and one that
    breaks the old rule of turning a hyphen into a space."""
    assert normalise(text) == expected
