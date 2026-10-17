"""Guards for plain comments in the backend.

Two sweeps read every `.py` file from disk (no service, no fixture): one finds
internal references in comments and docstrings, one caps how much of the
production code is comment. Python comments come from `tokenize`, docstrings
from `ast`. The finder is one function, driven by one table.
"""

import ast
import io
import re
import tokenize
from dataclasses import dataclass
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
SELF = Path(__file__).resolve()
PRODUCTION_DIRS = ("helpdesk", "migrations", "scripts")
ALL_DIRS = (*PRODUCTION_DIRS, "tests")
SKIP_DIRS = frozenset(
    {
        "node_modules",
        ".venv",
        "__pycache__",
        ".git",
        ".mypy_cache",
        ".ruff_cache",
        ".next",
        "dist",
        "coverage",
        "generated",
        "__generated__",
    }
)
MAX_SHARE_PERCENT = 25

# (kind, pattern, ignore case, production code only)
REFERENCE_KINDS: tuple[tuple[str, str, bool, bool], ...] = (
    ("plan number", r"\bplans?\s+\d", True, False),
    ("section sign", r"§\s*\d", True, False),
    ("adr number", r"\badr[\s-]?\d", True, False),
    ("row number", r"\brows?\s+\d", True, False),
    ("rule number", r"\brules?\s+\d", True, False),
    ("numbered item", r"\b(?:requirements|item|req)\s*#\d", True, False),
    ("decision letter", r"\b[Dd]ecision\s+[A-Z]\b", False, False),
    ("dev path", r"(?<![\w/.])dev/", True, False),
    ("moved from", r"\bmoved from\b", True, False),
    ("used to live", r"\bused to live\b", True, False),
    ("lived in", r"\blived in\b", True, False),
    ("formerly", r"\bformerly\b", True, False),
    ("now lives in", r"\bnow lives in\b", True, False),
    ("was called", r"\bwas called\b", True, False),
    ("renamed from", r"\brenamed from\b", True, False),
    ("test name", r"\btest_\w+", True, True),
    ("test path", r"\btests/", True, True),
)

DIRECTIVE = re.compile(
    r"^(?:noqa\b|type:\s*ignore|pragma\b|fmt:|-\*-|eslint-disable|@ts-expect-error"
    r"|@ts-ignore|!|<reference)",
    re.IGNORECASE,
)


def is_directive(comment: str) -> bool:
    """A tool directive: a lint switch, a type switch, a shebang or an encoding line."""
    return DIRECTIVE.match(comment.lstrip("#/* ").strip()) is not None


def find_references(text: str, *, production: bool) -> list[tuple[str, int]]:
    """Every internal reference in `text` as (kind, offset). Case-insensitive.

    Test names and test paths count only when `production` is true.
    """
    hits: list[tuple[str, int]] = []
    for kind, pattern, ignore_case, production_only in REFERENCE_KINDS:
        if production_only and not production:
            continue
        flags = re.IGNORECASE if ignore_case else 0
        hits.extend((kind, match.start()) for match in re.finditer(pattern, text, flags))
    return hits


@dataclass(frozen=True)
class Comment:
    line: int
    end_line: int
    own_line: bool
    text: str


def python_comments(source: str) -> list[Comment]:
    """Comments (tokenize) and docstrings, including bare strings (ast)."""
    lines = source.splitlines()
    found: list[Comment] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.COMMENT or is_directive(token.string):
            continue
        row, column = token.start
        found.append(Comment(row, row, lines[row - 1][:column].strip() == "", token.string))
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            found.append(
                Comment(node.lineno, node.end_lineno or node.lineno, True, node.value.value)
            )
    return found


def within_share_limit(comment_lines: int, total_lines: int) -> bool:
    """True when comment lines are at most 25% of all lines (25 in 100 passes)."""
    return comment_lines * 100 <= total_lines * MAX_SHARE_PERCENT


def source_files(dirs: tuple[str, ...]) -> list[Path]:
    return sorted(
        path
        for name in dirs
        for path in (BACKEND / name).rglob("*.py")
        if path.resolve() != SELF and not SKIP_DIRS.intersection(path.relative_to(BACKEND).parts)
    )


def comment_line_numbers(comments: list[Comment]) -> set[int]:
    return {
        number
        for comment in comments
        if comment.own_line
        for number in range(comment.line, comment.end_line + 1)
    }


FLAGGED: list[tuple[str, str, bool]] = [
    ("see plan 42", "plan number", False),
    ("plans 60-63", "plan number", False),
    ("plan 11b", "plan number", False),
    ("SPEC §4.3", "section sign", False),
    ("§19", "section sign", False),
    ("ADR 0031", "adr number", False),
    ("ADR-0012", "adr number", False),
    ("row 21", "row number", False),
    ("rows 30ba", "row number", False),
    ("standing rule 38", "rule number", False),
    ("hard rule 4", "rule number", False),
    ("rule 62", "rule number", False),
    ("REQUIREMENTS #41", "numbered item", False),
    ("item #41", "numbered item", False),
    ("decision C", "decision letter", False),
    ("dev/plans/80-plain-comments.md", "dev path", False),
    ("moved from shared/rag", "moved from", False),
    ("used to live in core", "used to live", False),
    ("it lived in core", "lived in", False),
    ("formerly called", "formerly", False),
    ("now lives in", "now lives in", False),
    ("was called helper", "was called", False),
    ("renamed from old_name", "renamed from", False),
    ("PLAN 42 UPPER", "plan number", False),
    ("pinned by test_seed_is_fake", "test name", True),
    ("covered in tests/shared/x.py", "test path", True),
]

PLAIN: list[tuple[str, bool]] = [
    ("step 2 of 3", True),
    ("HTTP 404", True),
    ("retries 3 times", True),
    ("one approval row per seat", True),
    ("the tool spec is mutating", True),
    ("rules run twice", True),
    ("plan_tools picks the tool", True),
    ("/dev/null is fine", True),
    ("a decision is made here", True),
    ("test_seed_is_fake", False),
    ("see tests/shared/x.py", False),
]


@pytest.mark.parametrize(("text", "kind", "production"), FLAGGED)
def test_reference_finder_flags_each_kind(text: str, kind: str, production: bool) -> None:
    """Catches a finder that drops this one kind of reference (one row per kind,
    asserting the kind's own name so a neighbouring pattern cannot cover for it)."""
    assert kind in {name for name, _ in find_references(text, production=True)}


@pytest.mark.parametrize(
    "text", [text for text, _, production in FLAGGED if production], ids=lambda t: t
)
def test_test_names_are_flagged_only_in_production(text: str) -> None:
    """Catches a finder that flags test names inside test files too, which may
    name a sibling test."""
    assert find_references(text, production=False) == []


@pytest.mark.parametrize(("text", "production"), PLAIN)
def test_reference_finder_passes_plain_text(text: str, production: bool) -> None:
    """Catches a finder so greedy it flags plain words: a tool name with
    `plan`, the word `rules`, a bare `row`, a number with no label, or a test
    name inside a test file."""
    assert find_references(text, production=production) == []


@pytest.mark.parametrize(
    "comment",
    [
        "# noqa: E501",
        "# type: ignore[attr-defined]",
        "# pragma: no cover",
        "# fmt: off",
        "#!/usr/bin/env python",
        "# -*- coding: utf-8 -*-",
    ],
)
def test_tool_directives_are_not_comments(comment: str) -> None:
    """Catches an extractor that counts a tool directive as a comment, which
    would flag it and push the share over the cap for no reason."""
    assert is_directive(comment)
    assert python_comments(f"x = 1  {comment}\n") == []


def test_extractor_reads_comments_and_docstrings_not_strings() -> None:
    """Catches an extractor that scans raw lines: a `#` inside a string is not a
    comment, while module, function and bare-string docstrings are."""
    source = (
        '"""module doc"""\n'
        "VALUE = 'not # a comment'\n"
        "# own line\n"
        "def f() -> None:\n"
        '    """function doc"""\n'
        "    y = 1  # trailing\n"
        '\nNOTE = 1\n"""attribute doc"""\n'
    )
    comments = python_comments(source)
    assert sorted((c.line, c.text) for c in comments) == [
        (1, "module doc"),
        (3, "# own line"),
        (5, "function doc"),
        (6, "# trailing"),
        (9, "attribute doc"),
    ]
    assert [c.own_line for c in sorted(comments, key=lambda c: c.line)] == [
        True,
        True,
        True,
        False,
        True,
    ]


@pytest.mark.parametrize(
    ("comment_lines", "total_lines", "passes"),
    [(24, 100, True), (25, 100, True), (26, 100, False), (1, 4, True), (2, 7, False)],
)
def test_comment_share_limit_is_inclusive(
    comment_lines: int, total_lines: int, passes: bool
) -> None:
    """Catches a limit written as `<` instead of `<=` (25 in 100 would fail) and
    a limit that drifts upward (26 in 100 would pass); pins the cap at 25."""
    assert within_share_limit(comment_lines, total_lines) is passes


def test_backend_comments_carry_no_internal_references() -> None:
    """Catches any comment or docstring left naming a plan, section, decision,
    row, rule, requirement, `dev/` path, folder history or (in production) a
    test. Lists every `file:line` at once."""
    hits: list[str] = []
    for path in source_files(ALL_DIRS):
        relative = path.relative_to(BACKEND)
        production = relative.parts[0] != "tests"
        for comment in python_comments(path.read_text(encoding="utf-8")):
            for kind, offset in find_references(comment.text, production=production):
                line = comment.line + comment.text[:offset].count("\n")
                hits.append(f"{relative}:{line} ({kind})")
    assert sorted(set(hits)) == [], f"{len(set(hits))} internal references: {sorted(set(hits))}"


def test_backend_comment_share_is_at_most_25_percent() -> None:
    """Catches production code that explains too much: comment plus docstring
    lines over all lines of helpdesk, migrations and scripts must stay within
    25%. The failure names the ten files with the highest share."""
    per_file: list[tuple[float, str]] = []
    comment_total = 0
    line_total = 0
    for path in source_files(PRODUCTION_DIRS):
        source = path.read_text(encoding="utf-8")
        total = len(source.splitlines())
        commented = len(comment_line_numbers(python_comments(source)))
        comment_total += commented
        line_total += total
        if total:
            per_file.append((commented / total, str(path.relative_to(BACKEND))))
    worst = [f"{name} {share:.0%}" for share, name in sorted(per_file, reverse=True)[:10]]
    assert within_share_limit(comment_total, line_total), (
        f"{comment_total} of {line_total} lines are comments "
        f"({comment_total / line_total:.1%}); highest ten: {worst}"
    )
