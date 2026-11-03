"""The prompts must not hold the words of the conversation cases.

Cost and isolation. Pure: reads the prompt files and the case file from the
repository, and builds the seed people in memory. No service, no `db` marker,
no socket, no waiting.

A prompt that shows the bot a case's own wording proves nothing when that case
runs, so every example in a prompt has to be made up.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from helpdesk.shared.services.seed import TENANTS, generate_tenant

ROOT = Path(__file__).resolve().parents[2]
PROMPTS = sorted((ROOT / "helpdesk" / "agent_api" / "agent" / "prompts").glob("*.md"))
CASES = ROOT / "evals" / "conversations" / "cases.jsonl"
CASE_FIELDS = ("opening", "person_knows", "goal", "script")
RUN = 6
"""The shortest run of shared words that counts as a copy."""


def words(text: str) -> list[str]:
    """Case-folded words with the punctuation dropped."""
    return re.findall(r"\w+", text.casefold())


def runs(text: str, size: int = RUN) -> set[tuple[str, ...]]:
    """Every run of `size` consecutive words in the text."""
    found = words(text)
    return {tuple(found[i : i + size]) for i in range(len(found) - size + 1)}


def shared_runs(prompt: str, case_texts: list[str], size: int = RUN) -> list[str]:
    """The runs of `size` words that the prompt and any case text both hold."""
    prompt_runs = runs(prompt, size)
    return sorted(
        " ".join(run) for text in case_texts for run in runs(text, size) if run in prompt_runs
    )


def flatten(value: Any) -> list[str]:
    """Every string inside a value that may be a string, a list or a dict."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in flatten(item)]
    if isinstance(value, dict):
        return [text for item in value.values() for text in flatten(item)]
    return []


def case_texts() -> list[str]:
    """One text per field per case: what a prompt must not echo."""
    texts: list[str] = []
    for line in CASES.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        for field in CASE_FIELDS:
            texts.extend(flatten(case.get(field)))
    return texts


def seeded_people() -> list[str]:
    """The full display name of every person the seed makes."""
    return sorted(
        {user.display_name for tenant in TENANTS for user in generate_tenant(tenant).users}
    )


def test_the_files_to_check_are_found() -> None:
    """Catches a check that passes because it found no prompt or no case to
    compare, for instance after a folder move.

    There are prompt files, case texts and seeded people to check against.
    """
    assert len(PROMPTS) >= 3
    assert len(case_texts()) >= 42
    assert len(seeded_people()) >= 100


def test_no_prompt_shares_six_words_with_a_case() -> None:
    """Catches a prompt example copied from, or worded like, a conversation
    case: the bot would be shown the test before it takes it.

    Every prompt file against every case field: no run of 6 words appears in
    both.
    """
    texts = case_texts()

    copied = {path.name: shared_runs(path.read_text(encoding="utf-8"), texts) for path in PROMPTS}

    assert {name: found for name, found in copied.items() if found} == {}


def test_no_prompt_names_a_seeded_request_or_person() -> None:
    """Catches a prompt that uses a seeded request number or a seeded person's
    name as an example, which ties the prompt to the demo data.

    No prompt holds a `REQ-` number, and none holds the full name of a seeded
    person (matched whole-word, case-folded).
    """
    people = seeded_people()
    found: dict[str, list[str]] = {}
    for path in PROMPTS:
        text = path.read_text(encoding="utf-8")
        folded = " ".join(words(text))
        hits = re.findall(r"REQ-\d+", text)
        hits += [name for name in people if f" {' '.join(words(name))} " in f" {folded} "]
        if hits:
            found[path.name] = hits

    assert found == {}


COPIED_OPENING = (
    "Can I get access to the finance dashboard? I need the cost figures for the line 3 redesign."
)


@pytest.mark.parametrize(
    ("prompt", "fails"),
    [
        (f"Example. {COPIED_OPENING} Then ask which role.", True),
        (f"Example. {COPIED_OPENING.upper().replace('?', '!')}", True),
        ("Example. May I see the canteen menu for next week? It is for a visit.", False),
        ("Can I get access to a weekly menu", False),
    ],
    ids=["copied_line", "copied_shouting_no_punctuation", "made_up", "five_words_only"],
)
def test_the_overlap_check_catches_a_copied_line(prompt: str, fails: bool) -> None:
    """Catches an overlap check that is blind to a copied line (wrong case,
    punctuation kept, runs never compared) or one that flags every shared short
    phrase.

    A made-up prompt holding a case's opening fails; the same line in capitals
    with other punctuation fails; a clean prompt and a prompt sharing only five
    words pass.
    """
    found = shared_runs(prompt, [COPIED_OPENING])

    assert bool(found) is fails
