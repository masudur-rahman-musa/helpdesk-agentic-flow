"""Understanding the message before the model picks the intent.

Pure: no I/O. Two things are read by code, never by the model.

Spelling. A word is fixed when it starts 3 or more letters of a catalogue word
("eng" for engineer) or reads at `WORD_CUTOFF` or closer to it ("dta" for
data). A run of words next to each other is replaced only when together it
names one system and one role of it, and then it becomes the role's plain
name ("dta warhous eng" becomes "Data Warehouse engineer"). A run naming one
person, word for word, becomes their display name. Anything else, a system
word alone or a role word alone included, is left as typed
(`test_understand.py`).

Picks. After a suggestion or a which-one question, "1", "the first one",
"last", or words that sit in exactly one offered option name that option. A
number past the end, or words that fit two options or none, are no pick.
"""

import difflib
import re
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from helpdesk.agent_api.agent.role_words import plain_role_name, role_word

WORD_CUTOFF = 0.75
"""Inclusive. How close a typed word must read to a catalogue word."""

PREFIX_MIN = 3
"""A typed word this long or longer may stand for a word it starts."""

_WORD = re.compile(r"[A-Za-z0-9]+")
_PICK_FILLER = frozenset({"the", "one", "that", "option", "number", "please"})
"""Words around a pick that pick nothing: "number 1 please", "the first one"."""
_ORDINALS = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2, "last": -1}


class SystemWords(BaseModel):
    """One system as the fixer reads it: its slug, its name and its role slugs."""

    model_config = ConfigDict(frozen=True)

    slug: str
    name: str
    role_slugs: tuple[str, ...]


class Catalogue(BaseModel):
    """The caller's own tenant's systems and people's display names. A model,
    not a dataclass, since it rides on the checkpointed state."""

    model_config = ConfigDict(frozen=True)

    systems: tuple[SystemWords, ...]
    people: tuple[str, ...] = ()


@dataclass(frozen=True)
class Understood:
    """The fixed message, and the system name and role word a fixed run named.
    Both are `None` when no run was changed."""

    message: str
    system: str | None = None
    role: str | None = None


def _words(text: str) -> list[str]:
    return [word.casefold() for word in _WORD.findall(text)]


def _fits(typed: str, word: str) -> bool:
    """Whether a typed word (casefolded) stands for a catalogue word."""
    if typed == word:
        return True
    if len(typed) < PREFIX_MIN:
        return False
    return word.startswith(typed) or (
        difflib.SequenceMatcher(None, typed, word).ratio() >= WORD_CUTOFF
    )


def _system_run(typed: Sequence[str], start: int, system: SystemWords) -> tuple[int, str] | None:
    """The end of the run from `start` naming this system and one role of it,
    with that role's slug, or `None`."""
    names = set(_words(system.name)) | set(_words(system.slug))
    roles = {slug: _words(role_word(slug)) for slug in system.role_slugs}
    end = start
    named_system = False
    named_roles: set[str] = set()
    while end < len(typed):
        word = typed[end]
        if any(_fits(word, name) for name in names):
            named_system = True
        else:
            hits = {slug for slug, words in roles.items() if any(_fits(word, w) for w in words)}
            if not hits:
                break
            named_roles |= hits
        end += 1
    if not named_system or len(named_roles) != 1:
        return None
    return end, next(iter(named_roles))


def _person_at(typed: Sequence[str], start: int, people: Sequence[str]) -> tuple[int, str] | None:
    """The person whose whole name starts at `start`, word for word, when
    exactly one does."""
    found = []
    for person in people:
        name = _words(person)
        if len(name) < 2 or start + len(name) > len(typed):
            continue
        window = typed[start : start + len(name)]
        if all(
            a == b or difflib.SequenceMatcher(None, a, b).ratio() >= WORD_CUTOFF
            for a, b in zip(window, name, strict=True)
        ):
            found.append((start + len(name), person))
    return found[0] if len(found) == 1 else None


def understand(message: str, catalogue: Catalogue) -> Understood:
    """The message with its runs fixed, and the last system and role a
    changed run named."""
    spans = [(match.start(), match.end()) for match in _WORD.finditer(message)]
    typed = [message[a:b].casefold() for a, b in spans]
    pieces: list[str] = []
    cursor = 0
    system: str | None = None
    role: str | None = None
    index = 0
    while index < len(typed):
        runs = [
            (run, item)
            for item in catalogue.systems
            if (run := _system_run(typed, index, item)) is not None
        ]
        replacement: tuple[int, str] | None = None
        if len(runs) == 1:
            (end, slug), item = runs[0]
            replacement = (end, plain_role_name(item.name, slug))
            found = (item.name, role_word(slug))
        else:
            replacement = _person_at(typed, index, catalogue.people)
            found = None
        if replacement is None:
            index += 1
            continue
        end, text = replacement
        start_at, end_at = spans[index][0], spans[end - 1][1]
        if message[start_at:end_at] != text:
            pieces.append(message[cursor:start_at])
            pieces.append(text)
            cursor = end_at
            if found is not None:
                system, role = found
        index = end
    pieces.append(message[cursor:])
    return Understood(message="".join(pieces), system=system, role=role)


def fix_spelling(message: str, catalogue: Catalogue) -> str:
    """The message with every run that names one system's role, or one
    person, spelt as the catalogue spells it."""
    return understand(message, catalogue).message


def read_pick(message: str, options: Sequence[str]) -> str | None:
    """The offered option the message picks, or `None`."""
    if not options:
        return None
    words = [word for word in _words(message) if word not in _PICK_FILLER]
    if not words:
        return None
    if len(words) == 1:
        word = words[0]
        if word.isdigit():
            number = int(word)
            return options[number - 1] if 1 <= number <= len(options) else None
        if word in _ORDINALS:
            place = _ORDINALS[word]
            return options[place] if -1 <= place < len(options) else None
    fitting = [option for option in options if set(words) <= set(_words(option))]
    return fitting[0] if len(fitting) == 1 else None
