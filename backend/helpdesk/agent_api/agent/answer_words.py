"""Yes and no words in English, German, French and Spanish, and a guess at
which of the four the person writes in.

Pure and fixed: the yes that files a change is read by code, never by the
model. Words are compared after `fold`, so "sí" and "si" are one word.
"""

import re
import unicodedata
from collections.abc import Sequence
from enum import StrEnum


class Language(StrEnum):
    """The languages the code-written lines come in."""

    ENGLISH = "en"
    GERMAN = "de"
    FRENCH = "fr"
    SPANISH = "es"


YES_WORDS = frozenset(
    {
        # English
        "yes",
        "y",
        "yep",
        "yup",
        "ya",
        "yea",
        "yeah",
        "correct",
        "right",
        "sure",
        "ok",
        "okay",
        "alright",
        "confirm",
        # German
        "ja",
        "jawohl",
        "jep",
        "klar",
        "genau",
        "gerne",
        "richtig",
        # French
        "oui",
        "ouais",
        "d'accord",
        "exact",
        "absolument",
        # Spanish
        "si",
        "claro",
        "vale",
        "dale",
        "correcto",
        "exacto",
    }
)
NO_WORDS = frozenset(
    {
        # English
        "no",
        "nope",
        "wrong",
        # German
        "nein",
        "nee",
        "falsch",
        # French
        "non",
        # Spanish
        "incorrecto",
    }
)

_WORD = re.compile(r"[a-z0-9']+")
"""Digits count as letters, so "nein1" is one unknown word, not a no."""

_LANGUAGE_WORDS: dict[Language, frozenset[str]] = {
    Language.ENGLISH: frozenset(
        {"i", "need", "please", "the", "my", "access", "to", "and", "can", "you", "want", "for"}
        | {"would", "like", "with", "is", "it", "me", "have", "give", "thanks", "of", "this"}
    ),
    Language.GERMAN: frozenset(
        {"ich", "brauche", "bitte", "zugang", "zum", "zur", "der", "die", "das", "und", "mein"}
        | {"meine", "mir", "nicht", "ist", "fur", "mit", "danke", "mochte", "habe", "kann"}
        | {"rolle", "ein", "eine", "auf", "auch"}
    ),
    Language.FRENCH: frozenset(
        {"je", "voudrais", "un", "une", "acces", "au", "s'il", "vous", "plait", "le", "la"}
        | {"les", "mon", "ma", "mes", "et", "pour", "avec", "est", "besoin", "merci", "j'ai"}
        | {"du", "des", "pas", "role"}
    ),
    Language.SPANISH: frozenset(
        {"necesito", "acceso", "al", "por", "favor", "el", "los", "las", "mi", "mis", "y"}
        | {"para", "con", "es", "quiero", "gracias", "tengo", "del", "una", "rol", "puedes"}
    ),
}


def fold(text: str) -> str:
    """Casefold and take accents off: "Mühle" is "muhle", "Straße" is "strasse"."""
    plain = text.casefold().replace("\N{RIGHT SINGLE QUOTATION MARK}", "'")
    decomposed = unicodedata.normalize("NFKD", plain)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def words(text: str) -> list[str]:
    """The folded words of `text`, apostrophes kept."""
    return _WORD.findall(fold(text))


def is_bare_answer(text: str) -> bool:
    """Whether `text` holds words and every one is a yes or no word."""
    found = words(text)
    return bool(found) and all(word in YES_WORDS | NO_WORDS for word in found)


def _clear_winner(message: str) -> Language | None:
    """The language with strictly the most known words in `message`, if any."""
    found = words(message)
    counts = sorted(
        (
            (sum(word in known for word in found), language)
            for language, known in _LANGUAGE_WORDS.items()
        ),
        reverse=True,
    )
    (best, language), (second, _) = counts[0], counts[1]
    return language if best > second else None


def guess_language(said: Sequence[str]) -> Language:
    """The person's language: the newest message with a clear winner decides,
    oldest first in `said`. No clear winner anywhere is English."""
    for message in reversed(said):
        language = _clear_winner(message)
        if language is not None:
            return language
    return Language.ENGLISH
