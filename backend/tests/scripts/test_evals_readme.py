"""The evals README says what the cases file and the settings hold."""

import re
from pathlib import Path

from helpdesk.shared.core.config import Settings

EVALS = Path(__file__).resolve().parents[2] / "evals"
README = EVALS / "README.md"
CASES = EVALS / "conversations" / "cases.jsonl"


def test_the_readme_gives_the_real_case_count() -> None:
    """Catches a case added or removed with the README left on the old count."""
    count = sum(1 for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip())
    said = {int(n) for n in re.findall(r"\b(\d+) (?:chosen )?multi-turn", README.read_text())}

    assert said == {count}


def test_every_cut_off_the_readme_names_is_a_setting() -> None:
    """Catches a cut-off listed after its setting was removed."""
    named = set(re.findall(r"^\| `([A-Z_]+)` \|", README.read_text(), flags=re.MULTILINE))

    assert named
    assert {name.lower() for name in named} <= set(Settings.model_fields)
