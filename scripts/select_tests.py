"""Which tests actually execute the lines this change touched.

`/adjacent-regression` used to select tests by grepping for module names,
which is a guess: a grep written slightly wrong silently selects too little
and nothing reports it. This selects by evidence instead — coverage.py
records, per test, which source lines that test executed, so the question
"who touched this function" has a real answer rather than a heuristic one.

Why not a static call graph: nothing in this repo calls a tool handler by
name. The pipeline reaches it as `REGISTRY[name].handler(...)`, a table
lookup, and reaches the directory through a `Protocol`. A static analyser
sees no caller and would under-select exactly the new code. Dynamic dispatch
is the design, so the selector has to be dynamic too.

The baseline is written by a full `make verify` run (`/close` step 2) with
`--cov-context=test`. Between full runs it goes stale in one direction only,
and every stale case is handled by falling back to MORE tests, never fewer:

* a file with no coverage history at all (a new module) selects the whole
  test suite of the packages that could reach it, because nothing recorded
  can prove what does not;
* a missing or unreadable baseline selects everything;
* a changed test file always selects itself.

Line numbers are read from the diff's NEW side and matched against the
baseline's OLD line numbers, which is wrong the moment a hunk shifts the
lines below it. That is deliberate and safe in this direction: it widens the
selection (neighbouring tests come along), it never narrows it. The full
suite at `/close` is the backstop that makes an approximate selector
acceptable at all — this is a speed optimisation, never a safety net.

Usage:
    uv run python scripts/select_tests.py            # vs HEAD
    uv run python scripts/select_tests.py --base main
    uv run python scripts/select_tests.py --explain  # why each test was picked
"""

from __future__ import annotations

import argparse
import ast
import re
import sqlite3
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

BASELINE = REPO_ROOT / ".coverage-baseline"
"""Where the full run leaves its per-test context database (gitignored)."""

TEST_ROOTS = (
    "packages/helpdesk/tests",
    "services/agent-api/tests",
    "services/mcp-server/tests",
)

# A source change anywhere in one of these packages can only be reached by
# tests under the matching roots. Used only for the no-history fallback,
# where the honest answer is "everything that could possibly reach it".
PACKAGE_TEST_ROOTS: dict[str, tuple[str, ...]] = {
    "packages/helpdesk/": TEST_ROOTS,  # the shared lib; every service uses it
    "services/agent-api/": ("services/agent-api/tests",),
    "services/mcp-server/": ("services/mcp-server/tests",),
    "scripts/": TEST_ROOTS,  # the seed is imported by fixtures everywhere
}

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout


def changed_files(base: str | None) -> list[str]:
    """Every file this change touches: staged, unstaged, and untracked."""
    paths: set[str] = set()
    diff = ["diff", "--name-only"]
    paths.update(_git(*diff, base).split() if base else _git(*diff, "HEAD").split())
    paths.update(_git("status", "--short", "--porcelain").splitlines() and [])
    for line in _git("status", "--porcelain").splitlines():
        status, _, path = line[:2], line[2], line[3:]
        if "?" in status or status.strip():
            paths.add(path.strip().strip('"'))
    return sorted(p for p in paths if p.endswith(".py"))


def changed_lines(path: str, base: str | None) -> set[int]:
    """The NEW-side line numbers this change added or edited in `path`.

    An untracked file has no diff, so it comes back empty and is treated as
    having no history, which selects the wide fallback.
    """
    try:
        diff = _git("diff", "-U0", base or "HEAD", "--", path)
    except subprocess.CalledProcessError:
        return set()
    lines: set[int] = set()
    for line in diff.splitlines():
        match = _HUNK.match(line)
        if match:
            start = int(match.group(1))
            count = int(match.group(2) or 1)
            lines.update(range(start, start + count))
    return lines


def _baseline_rows(db: sqlite3.Connection) -> dict[str, dict[int, set[str]]]:
    """path -> line -> the tests recorded as executing it."""
    covered: dict[str, dict[int, set[str]]] = defaultdict(lambda: defaultdict(set))
    numbits = db.execute(
        "SELECT f.path, c.context, lb.numbits FROM line_bits lb"
        " JOIN file f ON f.id = lb.file_id"
        " JOIN context c ON c.id = lb.context_id"
        " WHERE c.context != ''"
    ).fetchall()
    for path, context, bits in numbits:
        test = context.split("|")[0]
        if not test:
            continue
        rel = _relative(path)
        for line in _numbits_to_lines(bits):
            covered[rel][line].add(test)
    return covered


def _numbits_to_lines(bits: bytes) -> list[int]:
    """coverage.py stores a line set as a bitmap; unpack it."""
    return [
        byte_index * 8 + bit
        for byte_index, byte in enumerate(bits)
        for bit in range(8)
        if byte & (1 << bit)
    ]


def _relative(path: str) -> str:
    absolute = Path(path)
    try:
        return str(absolute.relative_to(REPO_ROOT))
    except ValueError:
        return path


MEASURED_PACKAGES = (
    "packages/helpdesk/helpdesk/",
    "services/agent-api/agent_api/",
    "services/mcp-server/mcp_server/",
)
"""What `[tool.coverage.run] source` actually measures.

A file outside these has no coverage history for a reason that says nothing
about risk: coverage was never watching it. Widening to the whole suite for
every such file would make the selector useless the moment anyone edits a
script, so those are decided by import instead (see `_importers`).
"""


def _fallback_roots(path: str) -> list[str]:
    for prefix, roots in PACKAGE_TEST_ROOTS.items():
        if path.startswith(prefix):
            return list(roots)
    return list(TEST_ROOTS)


def _importers(path: str) -> list[str]:
    """Does anything under test actually import this unmeasured module?

    Static, and only ever used for files coverage does not measure — tooling
    under `scripts/`, mostly. `scripts/seed.py` IS imported (every fixture
    seeds through it) and correctly widens; `scripts/select_tests.py` is not
    imported by anything and correctly selects nothing. A static check is
    sound here in a way it is not for handlers, because these modules are
    reached by a real `import` statement or not at all.
    """
    module = Path(path).stem
    if module == "__init__":
        return list(TEST_ROOTS)
    hits = subprocess.run(
        [
            "grep",
            "-rlE",
            rf"^\s*(from|import)\s+({module}|[A-Za-z_.]+\.{module})\b",
            *TEST_ROOTS,
            *MEASURED_PACKAGES,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    return sorted(set(hits.stdout.split()))


def select(base: str | None) -> tuple[list[str], dict[str, list[str]], list[str]]:
    """(selection, why each entry was picked, warnings)."""
    files = changed_files(base)
    if not files:
        return [], {}, ["no python files changed"]

    reasons: dict[str, list[str]] = defaultdict(list)
    warnings: list[str] = []
    selection: set[str] = set()

    for path in files:
        if any(path.startswith(root) for root in TEST_ROOTS):
            selection.add(path)
            reasons[path].append("the changed test file itself")

    sources = [path for path in files if not any(path.startswith(r) for r in TEST_ROOTS)]
    if not sources:
        return sorted(selection), dict(reasons), warnings

    if not BASELINE.exists():
        warnings.append(
            f"no coverage baseline at {BASELINE.name}: selecting every test root. "
            "Run `make verify-baseline` (or one `make verify`) to build it."
        )
        for root in TEST_ROOTS:
            selection.add(root)
            reasons[root].append("no baseline recorded")
        return sorted(selection), dict(reasons), warnings

    db = sqlite3.connect(f"file:{BASELINE}?mode=ro", uri=True)
    try:
        covered = _baseline_rows(db)
    finally:
        db.close()

    for path in sources:
        if not path.startswith(MEASURED_PACKAGES):
            # Coverage never watched this file, so its empty history means
            # nothing. Decide by whether anything under test imports it.
            importers = _importers(path)
            if not importers:
                warnings.append(f"{path} is unmeasured and imported by nothing: selecting no tests")
                continue
            for root in _fallback_roots(path):
                selection.add(root)
                reasons[root].append(f"{path} is unmeasured and imported by {len(importers)} files")
            continue
        history = covered.get(path)
        if not history:
            for root in _fallback_roots(path):
                selection.add(root)
                reasons[root].append(f"{path} has no coverage history (new module)")
            continue
        lines = changed_lines(path, base)
        if not lines:
            hits = {test for tests in history.values() for test in tests}
            note = f"{path} changed with no readable diff hunks"
        else:
            probe = _expand_to_enclosing_blocks(path, lines)
            hits = {test for line in probe for test in history.get(line, set())}
            note = f"{path} lines {_compact(lines)}"
        if not hits:
            for root in _fallback_roots(path):
                selection.add(root)
                reasons[root].append(f"{note}: no test covered those lines")
            continue
        for test in hits:
            selection.add(test)
            reasons[test].append(note)

    return sorted(selection), dict(reasons), warnings


def _expand_to_enclosing_blocks(path: str, lines: set[int]) -> set[int]:
    """Widen each changed line to the whole function or class containing it.

    A `def` line, a decorator and a docstring all execute at IMPORT time, so
    coverage records them under no test context and a literal line lookup
    finds nobody — which would send a one-word signature edit to the full
    suite. The body of that function is what tests actually execute, so the
    enclosing block is the honest unit to ask about.

    Parsed from the file as it is NOW; a file that does not parse (mid-edit,
    or a syntax error) returns the lines unchanged and lets the caller fall
    back, which is the safe direction.
    """
    source = REPO_ROOT / path
    try:
        tree = ast.parse(source.read_text())
    except (OSError, SyntaxError):
        return lines

    blocks: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
            end = node.end_lineno or node.lineno
            blocks.append((start, end))

    expanded = set(lines)
    for line in lines:
        # Innermost enclosing block: the narrowest range that contains it.
        enclosing = [(s, e) for s, e in blocks if s <= line <= e]
        if enclosing:
            start, end = min(enclosing, key=lambda span: span[1] - span[0])
            expanded.update(range(start, end + 1))
    return expanded


def _compact(lines: set[int]) -> str:
    ordered = sorted(lines)
    if len(ordered) <= 6:
        return ",".join(str(line) for line in ordered)
    return f"{ordered[0]}-{ordered[-1]} ({len(ordered)} lines)"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=None, help="git ref to diff against (default: HEAD)")
    parser.add_argument("--explain", action="store_true", help="print why each test was selected")
    args = parser.parse_args()

    selection, reasons, warnings = select(args.base)

    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if args.explain:
        for entry in selection:
            print(f"{entry}", file=sys.stderr)
            for reason in sorted(set(reasons.get(entry, []))):
                print(f"    <- {reason}", file=sys.stderr)
        print(f"{len(selection)} selected", file=sys.stderr)
    print(" ".join(selection))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
