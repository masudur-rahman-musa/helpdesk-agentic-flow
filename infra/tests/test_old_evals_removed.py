"""The old single-reply eval is gone and nothing live names it.

What is pinned: the golden set, its checker test and the split script no
longer exist; the 38 conversation cases do; the Makefile has no `eval` target
but still has `eval-chat`; and no tracked file outside the history paths and
the tests names the old route, files, models or command.

The name scan is a function that takes a root and a list of files, so the
planted-line case can feed it a fake tree. Pure filesystem and `git ls-files`:
no service, no network, no import of application code.
"""

import re
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest


def _repo_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / ".git").exists():
            return candidate
    raise RuntimeError("could not locate the repo root (.git) above this test file")


REPO_ROOT = _repo_root()

GONE_FILES = (
    "backend/evals/golden/helpdesk-v1.jsonl",
    "backend/evals/golden/example.jsonl",
    "dev/scripts/eval_sets.py",
    "backend/tests/scripts/test_eval_golden_set.py",
    "backend/tests/scripts/test_eval_sets.py",
)

CONVERSATION_CASES = "backend/evals/conversations/cases.jsonl"

# Tests may name the old things (they check they are gone); history files
# record what happened, not what exists.
EXCLUDED_PREFIXES = (
    "backend/tests/",
    "infra/tests/",
    "dev/plans/",
    "dev/adr/",
    "dev/notes/",
    "dev/docs/log.md",
    "dev/docs/placeholders.md",
    "dev/docs/restructure.md",
)

OLD_NAMES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("/v1/eval/turn", re.compile(re.escape("/v1/eval/turn"))),
    ("helpdesk-v1.jsonl", re.compile(re.escape("helpdesk-v1.jsonl"))),
    ("golden/example.jsonl", re.compile(re.escape("golden/example.jsonl"))),
    ("eval_sets.py", re.compile(re.escape("eval_sets.py"))),
    ("EvalTurnRequest", re.compile(re.escape("EvalTurnRequest"))),
    ("EvalEnvelope", re.compile(re.escape("EvalEnvelope"))),
    ("test_eval_golden_set", re.compile(re.escape("test_eval_golden_set"))),
    ("make eval", re.compile(r"make eval(?![-\w])")),
)


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, check=True
    ).stdout
    return [name for name in out.decode("utf-8").split("\0") if name]


def scan_for_old_names(root: Path, files: Sequence[str]) -> list[tuple[str, str]]:
    """Return (file, old name) for every live file under `root` that names one."""
    found: list[tuple[str, str]] = []
    for name in files:
        if name.startswith(EXCLUDED_PREFIXES):
            continue
        path = root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        found.extend((name, label) for label, pattern in OLD_NAMES if pattern.search(text))
    return found


def test_the_old_eval_files_are_gone() -> None:
    """Catches a retirement that leaves any of the five old files behind, or
    that cuts too far and shrinks the conversation cases.

    Each file is checked on its own, so one survivor is named. The case count
    is exactly 38 lines: a range would admit a lost or an added case.
    """
    survivors = [name for name in GONE_FILES if (REPO_ROOT / name).exists()]
    assert survivors == []

    lines = (REPO_ROOT / CONVERSATION_CASES).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 38


def test_the_makefile_has_no_eval_target() -> None:
    """Catches a Makefile that still carries the `eval` target or its `.PHONY`
    word, and one that drops `eval-chat` along with it.

    The `.PHONY` words are split on spaces, so `eval-chat` does not count as
    `eval`; the target line is matched at the start of a line with the colon
    right after the word, so `eval-chat:` does not match `eval:`.
    """
    lines = (REPO_ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
    assert [line for line in lines if line.startswith("eval:")] == []
    phony = next(line for line in lines if line.startswith(".PHONY:"))
    assert "eval" not in phony.removeprefix(".PHONY:").split()
    assert any(line.startswith("eval-chat:") for line in lines)


def test_no_live_file_names_the_old_eval() -> None:
    """Catches a doc, config, script, duration list or status line that still
    names the old route, files, models or command (the listed test names in
    `.github/scripts/test_durations.json` and the carry-forward lines in
    the project notes included).

    The scan reads every tracked file outside the tests and the history
    paths, so a stale mention anywhere live is reported with its file.
    """
    assert scan_for_old_names(REPO_ROOT, _tracked_files()) == []


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        pytest.param("run make eval to score it", [("note.md", "make eval")], id="make-eval"),
        pytest.param("make eval\n", [("note.md", "make eval")], id="make-eval-at-line-end"),
        pytest.param("run make eval-chat to score it", [], id="make-eval-chat"),
        pytest.param("see /v1/eval/turn", [("note.md", "/v1/eval/turn")], id="old-route"),
    ],
)
def test_the_name_scan_reports_a_planted_line(
    tmp_path: Path, line: str, expected: list[tuple[str, str]]
) -> None:
    """Catches a scan that reports nothing (so the live-file check above could
    never fail), one that matches `make eval-chat` as `make eval`, and one that
    misses `make eval` at the end of a line.

    A fake tree holds one file with the planted line; an excluded history file
    holding the same line must not be reported.
    """
    (tmp_path / "note.md").write_text(line, encoding="utf-8")
    (tmp_path / "dev" / "plans").mkdir(parents=True)
    (tmp_path / "dev" / "plans" / "old.md").write_text(line, encoding="utf-8")

    found = scan_for_old_names(tmp_path, ["note.md", "dev/plans/old.md"])

    assert found == expected
