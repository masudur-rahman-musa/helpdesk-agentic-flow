"""Every ruff run uses the one backend config, and CI never writes a `.env`.

What is pinned: both pre-commit ruff hooks, CI's format check and the Makefile's
`format` and `lint` recipes pass `--config backend/pyproject.toml`; the Makefile
recipes cover `infra/tests`; ruff is clean over the whole repo under that config;
and no step of `ci.yml` writes `backend/.env`.

Why: ruff finds its config only inside `backend/`. A run from the repo root
without `--config` gets ruff's defaults for every file outside it, so CI's lint
job and the local hooks disagree about `infra/tests`.

The config files are read parsed (`yaml.safe_load`) where they are YAML. The
Makefile is read as recipe lines, with backslash continuations joined.
"""

import re
import shlex
import subprocess
import sys
from itertools import pairwise
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]


def _repo_root() -> Path:
    """The directory holding `backend/pyproject.toml`, found by walking upwards."""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "backend" / "pyproject.toml").is_file():
            return candidate
    raise AssertionError("repo root not found: no parent holds backend/pyproject.toml")


REPO_ROOT = _repo_root()
PRE_COMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
MAKEFILE = REPO_ROOT / "Makefile"

BACKEND_CONFIG = "backend/pyproject.toml"
"""The one ruff config every run must name, as a path relative to the repo root."""

RUFF_PRE_COMMIT_REPO = "https://github.com/astral-sh/ruff-pre-commit"
RUFF_HOOK_IDS: tuple[str, ...] = ("ruff-format", "ruff-check")

BACKEND_ENV_WRITE = re.compile(
    r"(?:\b(?:cp|mv|install|tee)\b[^\n;&|]*?\s|>>?\s*)(?:\./)?backend/\.env(?![\w.])"
)
"""A shell line that writes `backend/.env`: a `cp`, `mv`, `install` or `tee`
naming it, or a `>`/`>>` redirect into it. The negative lookahead keeps
`backend/.env.demo` and `backend/.env.example` from matching."""

DOT_ENV_WRITE = re.compile(
    r"(?:\b(?:cp|mv|install|tee)\b[^\n;&|]*?\s|>>?\s*)(?:\./)?\.env(?![\w.])"
)
"""The same write, for a step whose `working-directory` is `backend`."""


def _load_yaml(path: Path) -> dict[str, Any]:
    """Parse one YAML document, failing with the path when it is missing."""
    assert path.is_file(), f"{path.relative_to(REPO_ROOT)} does not exist"
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict), f"{path.relative_to(REPO_ROOT)} is not a YAML mapping"
    return parsed


def _names_backend_config(tokens: list[str]) -> bool:
    """True when `tokens` hold `--config backend/pyproject.toml` or its `=` form."""
    if f"--config={BACKEND_CONFIG}" in tokens:
        return True
    return any(
        token == "--config" and following == BACKEND_CONFIG for token, following in pairwise(tokens)
    )


def _ci_jobs() -> dict[str, Any]:
    """The `jobs` mapping of `ci.yml`."""
    jobs = _load_yaml(CI_WORKFLOW).get("jobs")
    assert isinstance(jobs, dict), "ci.yml declares no jobs mapping"
    return jobs


def _make_recipe(target: str) -> list[str]:
    """The recipe commands of one Makefile target, comments dropped, continuations joined."""
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


def _run_ruff(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the project's ruff (the one in this interpreter's environment) from the root."""
    return subprocess.run(
        [sys.executable, "-m", "ruff", *arguments],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_both_pre_commit_ruff_hooks_pass_the_backend_config() -> None:
    """Catches a hook left on ruff's defaults, or only one of the two hooks given the config."""
    repos = _load_yaml(PRE_COMMIT_CONFIG).get("repos")
    assert isinstance(repos, list), ".pre-commit-config.yaml declares no repos list"
    ruff_repos = [
        repo
        for repo in repos
        if isinstance(repo, dict) and repo.get("repo") == RUFF_PRE_COMMIT_REPO
    ]
    assert len(ruff_repos) == 1, f"expected 1 ruff-pre-commit repo, found {len(ruff_repos)}"
    hooks = {
        str(hook.get("id")): hook
        for hook in ruff_repos[0].get("hooks", [])
        if isinstance(hook, dict)
    }
    for hook_id in RUFF_HOOK_IDS:
        assert hook_id in hooks, f"the {hook_id} hook is missing, hooks are {sorted(hooks)}"
        arguments = hooks[hook_id].get("args", [])
        assert isinstance(arguments, list), f"the {hook_id} hook's args is not a list"
        assert _names_backend_config([str(argument) for argument in arguments]), (
            f"the {hook_id} hook does not pass --config {BACKEND_CONFIG}: args are {arguments}"
        )


def test_the_ci_format_check_passes_the_backend_config() -> None:
    """Catches CI's `ruff format --check .` still running with ruff's defaults outside backend/."""
    lint = _ci_jobs().get("lint")
    assert isinstance(lint, dict), "ci.yml has no lint job"
    steps = [step for step in lint.get("steps", []) if isinstance(step, dict)]
    format_lines = [
        line
        for step in steps
        for line in str(step.get("run", "")).splitlines()
        if "ruff format" in line and "--check" in line
    ]
    assert len(format_lines) == 1, (
        f"expected 1 `ruff format --check` line in the lint job, found {len(format_lines)}"
    )
    assert _names_backend_config(shlex.split(format_lines[0])), (
        f"the CI format check does not pass --config {BACKEND_CONFIG}: {format_lines[0]!r}"
    )


def test_make_format_and_lint_cover_infra_tests() -> None:
    """Catches `make format` or `make lint` that skip infra/tests or drop the backend config."""
    for target in ("format", "lint"):
        ruff_commands = [command for command in _make_recipe(target) if " ruff " in command]
        assert len(ruff_commands) == 1, (
            f"expected 1 ruff command in `make {target}`, found {ruff_commands}"
        )
        tokens = shlex.split(ruff_commands[0])
        assert "infra/tests" in tokens, f"`make {target}` does not cover infra/tests: {tokens}"
        assert _names_backend_config(tokens), (
            f"`make {target}` does not pass --config {BACKEND_CONFIG}: {tokens}"
        )


def test_ruff_is_clean_over_the_whole_repo_with_the_backend_config() -> None:
    """Catches infra/tests (or any file outside backend/) left unformatted or failing lint."""
    lint = _run_ruff("check", "--config", BACKEND_CONFIG, ".")
    form = _run_ruff("format", "--check", "--config", BACKEND_CONFIG, ".")
    assert lint.returncode == 0, f"ruff check is not clean:\n{lint.stdout}{lint.stderr}"
    assert form.returncode == 0, f"ruff format --check is not clean:\n{form.stdout}{form.stderr}"


def test_no_ci_step_writes_a_backend_env() -> None:
    """Catches the tests job still copying backend/.env.demo to backend/.env before compose."""
    writers: list[str] = []
    for job_name, job in _ci_jobs().items():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps", []):
            if not isinstance(step, dict):
                continue
            run = str(step.get("run", ""))
            in_backend = str(step.get("working-directory", "")).rstrip("/") in {
                "backend",
                "./backend",
            }
            for line in run.splitlines():
                if BACKEND_ENV_WRITE.search(line) or (in_backend and DOT_ENV_WRITE.search(line)):
                    writers.append(f"{job_name}: {line.strip()}")
    assert writers == [], f"ci.yml writes backend/.env: {writers}"
