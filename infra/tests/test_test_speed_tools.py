"""The test-speed tools are present and the everyday run drops only slow tests.

What is pinned: the backend pytest config registers a `slow` marker and turns on
`--strict-markers`; a misspelt marker fails collection; `make test-fast` runs
`-m "not slow" -n 3` from `backend/`; `make test-slowest` prints `--durations=30`
with no workers and no coverage; the full `test` recipe and every ci.yml pytest
line carry no `-m` filter; `verify` still depends on the same six targets in
order; at least one backend test is marked slow; rule 82 holds the 2-minute
target; and both new targets are `.PHONY`, self-documented and named in the docs.

Why: `--strict-markers` is what makes a typo fail collection instead of quietly
running the test in the fast set, and the full runs must keep the slow tests or
CI stops proving them. The timing itself is a written rule, not a timed assert.

The Makefile is read as recipe lines (`_make_recipe`); ci.yml is parsed
(`yaml.safe_load`); backend/pyproject.toml is parsed with tomllib. The misspelt
marker runs in one tiny pytest subprocess on a temp file, no services.
"""

import subprocess
import sys
import tomllib
from pathlib import Path

import yaml  # type: ignore[import-untyped]


def _repo_root() -> Path:
    """The directory holding `backend/pyproject.toml`, found by walking upwards."""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "backend" / "pyproject.toml").is_file():
            return candidate
    raise AssertionError("repo root not found: no parent holds backend/pyproject.toml")


REPO_ROOT = _repo_root()
MAKEFILE = REPO_ROOT / "Makefile"
BACKEND_DIR = REPO_ROOT / "backend"
BACKEND_PYPROJECT = BACKEND_DIR / "pyproject.toml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
RULES = REPO_ROOT / "dev" / "docs" / "rules.md"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
README = REPO_ROOT / "README.md"

VERIFY_DEPENDS = ["format", "lint", "typecheck", "test", "test-policy", "web-verify"]
"""The exact ordered dependency list `verify` must keep."""


def _pytest_ini() -> dict[str, object]:
    """The `[tool.pytest.ini_options]` table from backend/pyproject.toml."""
    parsed = tomllib.loads(BACKEND_PYPROJECT.read_text(encoding="utf-8"))
    tool = parsed.get("tool")
    assert isinstance(tool, dict), "backend/pyproject.toml has no [tool] table"
    ini = tool.get("pytest", {})
    assert isinstance(ini, dict), "backend/pyproject.toml has no [tool.pytest] table"
    options = ini.get("ini_options", {})
    assert isinstance(options, dict), "no [tool.pytest.ini_options] table"
    return options


def _make_recipe(target: str) -> list[str]:
    """The recipe commands of one Makefile target, comments dropped, continuations joined."""
    import re

    lines = MAKEFILE.read_text(encoding="utf-8").splitlines()
    header = re.compile(rf"^{re.escape(target)}\s*:(?!=)")
    starts = [index for index, line in enumerate(lines) if header.match(line)]
    assert len(starts) == 1, f"expected 1 Makefile rule for {target!r}, found {len(starts)}"
    commands: list[str] = []
    pending = ""
    for line in lines[starts[0] + 1 :]:
        if not line.startswith("\t"):
            break
        body = line.strip()
        if body.startswith("#") and not pending:
            continue
        if body.endswith("\\"):
            pending += body[:-1] + " "
            continue
        commands.append(pending + body)
        pending = ""
    return commands


def _target_header(target: str) -> str:
    """The single line declaring `target:`, used to read its `##` help text and deps."""
    import re

    lines = MAKEFILE.read_text(encoding="utf-8").splitlines()
    header = re.compile(rf"^{re.escape(target)}\s*:(?!=)")
    matches = [line for line in lines if header.match(line)]
    assert len(matches) == 1, f"expected 1 header line for {target!r}, found {len(matches)}"
    return matches[0]


def _phony_names() -> list[str]:
    """Every name on the Makefile's single `.PHONY:` line."""
    import re

    lines = MAKEFILE.read_text(encoding="utf-8").splitlines()
    phony = [line for line in lines if line.startswith(".PHONY:")]
    assert len(phony) == 1, f"expected 1 .PHONY line, found {len(phony)}"
    return re.sub(r"^\.PHONY:\s*", "", phony[0]).split()


def _ci_pytest_lines() -> list[str]:
    """Every `run` line across ci.yml that invokes pytest."""
    parsed = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    jobs = parsed.get("jobs")
    assert isinstance(jobs, dict), "ci.yml declares no jobs mapping"
    lines: list[str] = []
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps", []):
            if not isinstance(step, dict):
                continue
            for line in str(step.get("run", "")).splitlines():
                if "pytest" in line:
                    lines.append(line)
    assert lines, "ci.yml has no pytest run lines"
    return lines


def test_slow_marker_is_registered() -> None:
    """Catches a config that never registers `slow` or leaves markers lax, so a typo runs."""
    options = _pytest_ini()
    markers = options.get("markers")
    assert isinstance(markers, list), f"markers is not a list: {markers!r}"
    slow = [str(marker) for marker in markers if str(marker).startswith("slow:")]
    assert len(slow) == 1, f"expected one marker starting 'slow:', found {slow} in {markers}"
    addopts = str(options.get("addopts", ""))
    assert "--strict-markers" in addopts, f"addopts lacks --strict-markers: {addopts!r}"


def test_misspelt_marker_fails_collection(tmp_path: Path) -> None:
    """Catches lax markers: a typo'd `slwo` would collect and run in the fast set, not error."""
    test_file = tmp_path / "test_misspelt_marker_temp.py"
    test_file.write_text(
        "import pytest\n\n\n@pytest.mark.slwo\ndef test_marked() -> None:\n    assert True\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(BACKEND_PYPROJECT),
            "-p",
            "no:cacheprovider",
            str(test_file),
        ],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0, f"a misspelt marker did not fail collection:\n{combined}"
    assert "slwo" in combined, f"the error does not name the bad marker 'slwo':\n{combined}"


def test_fast_target_skips_slow_on_three_workers() -> None:
    """Catches a fast run that keeps slow tests or drops the 3-worker cap the Redis indexes need."""
    commands = _make_recipe("test-fast")
    joined = " ".join(commands)
    assert '-m "not slow"' in joined or "-m 'not slow'" in joined, (
        f'test-fast does not pass -m "not slow": {commands}'
    )
    assert "-n 3" in joined or "-n3" in joined, f"test-fast does not pass -n 3: {commands}"
    assert "cd backend" in joined or "backend &&" in joined, (
        f"test-fast does not run from backend/: {commands}"
    )


def test_slowest_target_prints_durations() -> None:
    """Catches a slowest run that hides the numbers, or bends them with workers or coverage."""
    commands = _make_recipe("test-slowest")
    joined = " ".join(commands)
    assert "--durations=30" in joined, f"test-slowest does not pass --durations=30: {commands}"
    assert "-n " not in joined and "-n3" not in joined and "--numprocesses" not in joined, (
        f"test-slowest must run serially, it carries workers: {commands}"
    )
    assert "--cov" not in joined, f"test-slowest must run without coverage: {commands}"


def test_full_runs_keep_slow_tests() -> None:
    """Catches a full run narrowed to `-m "not slow"`, which would stop CI proving slow tests."""
    test_commands = _make_recipe("test")
    for command in test_commands:
        assert " -m " not in f" {command} ", f"`make test` carries a -m filter: {command!r}"
    for line in _ci_pytest_lines():
        assert " -m " not in f" {line} ", f"a ci.yml pytest line carries a -m filter: {line!r}"


def test_verify_is_unchanged() -> None:
    """Catches a plan that reorders or drops a `verify` dependency while adding the new targets."""
    header = _target_header("verify")
    after_colon = header.split(":", 1)[1]
    deps = after_colon.split("##", 1)[0].split()
    assert deps == VERIFY_DEPENDS, f"verify deps changed: {deps} != {VERIFY_DEPENDS}"


def test_some_backend_tests_are_marked_slow() -> None:
    """Catches a plan that adds the marker but marks no test, so the fast set drops nothing."""
    marked = [
        path
        for path in (BACKEND_DIR / "tests").rglob("test_*.py")
        if "pytest.mark.slow" in path.read_text(encoding="utf-8")
    ]
    assert marked, "no file under backend/tests carries pytest.mark.slow"


def test_rules_hold_the_fast_set_target() -> None:
    """Catches a missing rule 82, so the 2-minute fast-set target is nowhere written down."""
    lines = RULES.read_text(encoding="utf-8").splitlines()
    starts = [index for index, line in enumerate(lines) if line.lstrip().startswith("82.")]
    assert len(starts) == 1, f"expected one rule numbered 82, found {len(starts)} at {starts}"
    body_lines = [lines[starts[0]]]
    for line in lines[starts[0] + 1 :]:
        if line.strip() and not line.startswith((" ", "\t")):
            break
        body_lines.append(line)
    body = "\n".join(body_lines)
    assert "make test-fast" in body, f"rule 82 does not name `make test-fast`:\n{body}"
    assert "2 minutes" in body or "2-minute" in body, f"rule 82 does not name 2 minutes:\n{body}"


def test_new_targets_are_phony_and_documented() -> None:
    """Catches a new target left off .PHONY, with no help text, or missing from the docs."""
    phony = _phony_names()
    claude_text = CLAUDE_MD.read_text(encoding="utf-8")
    readme_text = README.read_text(encoding="utf-8")
    for target in ("test-fast", "test-slowest"):
        assert target in phony, f"{target} is not in .PHONY: {phony}"
        header = _target_header(target)
        assert "##" in header, f"{target} has no ## help text: {header!r}"
        assert target in claude_text, f"{target} is not named in CLAUDE.md"
        assert target in readme_text, f"{target} is not named in README.md"
