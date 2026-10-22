"""The README tells the conversation eval score honestly.

The score is read from `backend/evals/engine-scores.json`, never typed from
memory: the README must hold `<cases_passed> of <cases>` of the NEWEST entry,
beside the word "conversation", and must name every group of cases that
`backend/evals/reports/chat-report.json` scores under half, with the date of
the newest entry (the report itself carries no date).

The two checkers live in this file and are fed made-up scores and reports in
the unhappy-path rows, so a checker that always says yes is caught. Reads three
files from the repo. No service, no network.
"""

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
README = _REPO_ROOT / "README.md"
SCORES = _REPO_ROOT / "backend" / "evals" / "engine-scores.json"
REPORT = _REPO_ROOT / "backend" / "evals" / "reports" / "chat-report.json"

HALF = 0.5


def _newest(scores: object) -> Mapping[str, Any]:
    """The entry with the latest date; on a tie, the one written last."""
    assert isinstance(scores, dict) and scores, "engine-scores.json holds no entries"
    newest: Mapping[str, Any] | None = None
    for entry in scores.values():
        if newest is None or str(entry["date"]) >= str(newest["date"]):
            newest = entry
    assert newest is not None
    return newest


def _paragraphs(readme: str) -> list[str]:
    return [part for part in re.split(r"\n\s*\n", readme) if part.strip()]


def readme_states_newest_rate(readme: str, scores: object) -> bool:
    """Whether one paragraph holds both the word "conversation" and the newest
    entry's `<cases_passed> of <cases>`, as whole numbers (so `121 of 33` is
    not `21 of 33`)."""
    newest = _newest(scores)
    shown = re.compile(rf"(?<!\d){newest['cases_passed']} of {newest['cases']}(?!\d)")
    return any(
        "conversation" in part.lower() and shown.search(part) is not None
        for part in _paragraphs(readme)
    )


def _names(kind: str) -> tuple[str, ...]:
    return (kind.lower(), kind.lower().replace("_", " "))


def groups_under_half_missing(readme: str, report: object) -> list[str]:
    """The groups in `per_kind` scoring strictly under half that the README
    does not name, written with the underscore or with a space."""
    assert isinstance(report, dict)
    per_kind: Mapping[str, float] = report["summary"]["per_kind"]
    text = readme.lower()
    return [
        kind
        for kind, rate in per_kind.items()
        if rate < HALF and not any(name in text for name in _names(kind))
    ]


def _scores() -> object:
    return json.loads(SCORES.read_text(encoding="utf-8"))


def _report() -> object:
    return json.loads(REPORT.read_text(encoding="utf-8"))


def test_the_readme_states_the_newest_pass_rate() -> None:
    """Catches a README that still shows an old or hand-typed number (the
    plain "33 conversation cases" line with no pass rate), or that shows the
    rate of an older engine entry instead of the newest.

    The newest entry of the real scores file is compared, so a score file that
    moves on makes this README stale and red.
    """
    scores = _scores()
    newest = _newest(scores)

    assert readme_states_newest_rate(README.read_text(encoding="utf-8"), scores), (
        f"the README does not say '{newest['cases_passed']} of {newest['cases']}' "
        "next to the word conversation"
    )


def test_the_readme_names_every_group_under_half() -> None:
    """Catches a README that gives the rate but hides the groups that still
    fail (short answers, other people, other languages and the rest), and one
    that leaves out the date of the run it reports.

    The groups come from `per_kind` of the real report, and the date is the
    newest entry's date in the scores file, so a group added later is demanded
    too.
    """
    readme = README.read_text(encoding="utf-8")
    newest = _newest(_scores())

    assert groups_under_half_missing(readme, _report()) == []
    assert str(newest["date"]) in readme


def test_a_readme_with_a_stale_number_fails() -> None:
    """Unhappy path. Catches a checker that accepts any rate in the README, one
    that reads an older entry instead of the newest, one that takes `121 of 33`
    for `21 of 33`, one that drops the "conversation" rule, and a group checker
    that forgets a group or counts exactly half as failing.

    The made-up scores have the newest entry LAST by date and an older entry
    that equals the planted text, so only reading the newest fails it.
    """
    scores = {
        "pipeline": {"cases_passed": 15, "cases": 33, "date": "2026-10-22"},
        "graph": {"cases_passed": 21, "cases": 33, "date": "2026-10-23"},
    }
    fresh = "The 21 of 33 conversation cases pass on the graph engine."

    assert readme_states_newest_rate(fresh, scores)
    assert not readme_states_newest_rate("The 20 of 33 conversation cases pass.", scores)
    assert not readme_states_newest_rate("Scored 15 of 33 conversation cases.", scores)
    assert not readme_states_newest_rate("The 121 of 33 conversation cases pass.", scores)
    assert not readme_states_newest_rate("The 21 of 330 conversation cases pass.", scores)
    assert not readme_states_newest_rate("It passes 21 of 33 cases.\n\nA conversation.", scores)

    report = {"summary": {"per_kind": {"typo": 0.49, "short_answer": 0.0, "attack": 0.5}}}
    assert groups_under_half_missing("typo and short answer fail", report) == []
    assert groups_under_half_missing("short_answer only", report) == ["typo"]
    assert groups_under_half_missing("nothing named", report) == ["typo", "short_answer"]
