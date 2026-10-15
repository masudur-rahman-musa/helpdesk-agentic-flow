"""The repo root keeps only what a tool must find there.

What is pinned: the three app services read `backend/.env.demo` then
`backend/.env`, both optional; every `${...}` in `docker-compose.yml` that
names a key of `backend/.env.demo` carries a default equal to that line, so
`docker compose config` passes with no `.env` anywhere; `check_root.py`
refuses a root `.env` even when `.gitignore` hides it from git; `.editorconfig`
is gone; and the CI config scan names the moved `infra/.trivyignore`.

Why the checks are static: none of these files is read by the application.
They are read by compose, git hooks, CI and the dev scripts, so the only place
their shape can be proven here is by parsing them. Compose is parsed with
`yaml.safe_load`, never as raw text, so a comment naming `${POLICY_DIR}` cannot
redden a case. The one runtime call is `docker compose config`, skipped when
docker is not on PATH. No backing service is touched.

The cases are red at HEAD because compose still reads a root `.env` with
required `${...:?}` lookups, `check_root.py` only reads `git ls-files`,
`.editorconfig` exists, and `ci.yml`
passes `--ignorefile .trivyignore`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]


def _repo_root() -> Path:
    """The directory holding `backend/pyproject.toml`, found by walking upwards."""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "backend" / "pyproject.toml").is_file():
            return candidate
    raise AssertionError("repo root not found: no parent holds backend/pyproject.toml")


REPO_ROOT = _repo_root()
COMPOSE = REPO_ROOT / "docker-compose.yml"
ENV_DEMO = REPO_ROOT / "backend" / ".env.demo"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
CHECK_ROOT = REPO_ROOT / "dev" / "scripts" / "check_root.py"

APP_SERVICES = ("helpdesk-agent-api", "helpdesk-mcp-server", "helpdesk-scheduler")
"""The three services that read `env_file: [.env]` before this plan."""

EXPECTED_ENV_FILES = [("backend/.env.demo", False), ("backend/.env", False)]
"""Demo values first, then the real keys over them, both optional, in this order."""

POSTGRES_NAMES = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "APP_DB_PASSWORD")
"""The four values that were required `${...:?}` lookups before this plan."""

# `${NAME}`, `${NAME:-x}`, `${NAME-x}`, `${NAME:?x}`, `${NAME?x}`; never `$${...}`.
_BRACED = re.compile(r"(?<!\$)\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?+])([^}]*))?\}")
# `$NAME` with no braces; never `$$NAME`.
_UNBRACED = re.compile(r"(?<!\$)\$([A-Za-z_][A-Za-z0-9_]*)")


def _compose() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return data


def _env_demo() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in ENV_DEMO.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _strings(node: object) -> list[str]:
    """Every string value and mapping key in a parsed YAML tree."""
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        out: list[str] = []
        for key, value in node.items():
            out += _strings(key) + _strings(value)
        return out
    if isinstance(node, list):
        return [s for item in node for s in _strings(item)]
    return []


def _norm(path: str) -> str:
    return path[2:] if path.startswith("./") else path


def _env_files(service: dict[str, Any]) -> list[tuple[str, bool]]:
    """A service's `env_file` as (path, required) pairs, short forms expanded."""
    raw = service.get("env_file")
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    out: list[tuple[str, bool]] = []
    for item in items:
        if isinstance(item, str):
            out.append((_norm(item), True))
        else:
            out.append((_norm(str(item["path"])), bool(item.get("required", True))))
    return out


def test_app_services_read_backend_env_files() -> None:
    """Catches compose loading a root `.env`, a required env file, or `.env` before `.env.demo`.

    Exact list per service, so each of the three wrong shapes flips the case alone.
    """
    services = _compose()["services"]
    for name in APP_SERVICES:
        assert _env_files(services[name]) == EXPECTED_ENV_FILES, name
    for name, service in services.items():
        paths = [p for p, _ in _env_files(service)]
        assert ".env" not in paths, f"{name} still reads the root .env"


def test_compose_defaults_match_env_demo() -> None:
    """Catches a default drifting from `.env.demo`, a `${...:?}` left in, or a bare `${NAME}`.

    A bare lookup of a `.env.demo` key is empty once no root `.env` exists.
    """
    demo = _env_demo()
    for name in POSTGRES_NAMES:
        assert name in demo, f"premise: {name} has a line in backend/.env.demo"

    required: list[str] = []
    bare: list[str] = []
    mismatched: list[str] = []
    defaulted: set[str] = set()
    for text in _strings(_compose()):
        for match in _BRACED.finditer(text):
            name, op, value = match.group(1), match.group(2), match.group(3)
            if op in (":?", "?"):
                required.append(match.group(0))
            elif name not in demo:
                continue
            elif op is None or op in (":+", "+"):
                bare.append(match.group(0))
            else:
                defaulted.add(name)
                if value != demo[name]:
                    mismatched.append(f"{match.group(0)} vs .env.demo {name}={demo[name]}")
        for match in _UNBRACED.finditer(text):
            if match.group(1) in demo:
                bare.append(match.group(0))

    assert required == [], f"required lookups remain: {required}"
    assert bare == [], f"lookups with no default for a .env.demo key: {bare}"
    assert mismatched == [], f"defaults differ from backend/.env.demo: {mismatched}"
    assert set(POSTGRES_NAMES) <= defaulted, (
        f"the Postgres values carry no default: {set(POSTGRES_NAMES) - defaulted}"
    )


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker is not on PATH")
def test_compose_config_without_any_env(tmp_path: Path) -> None:
    """Catches a compose file that still needs a root `.env` or a `backend/.env` to load."""
    shutil.copy(COMPOSE, tmp_path / "docker-compose.yml")
    (tmp_path / "backend").mkdir()
    (tmp_path / "web").mkdir()
    shutil.copy(ENV_DEMO, tmp_path / "backend" / ".env.demo")
    assert not (tmp_path / ".env").exists()
    assert not (tmp_path / "backend" / ".env").exists()

    demo = _env_demo()
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in demo
        and not k.startswith("POSTGRES_")
        and k != "APP_DB_PASSWORD"
        and not k.startswith("COMPOSE_")
    }
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(tmp_path / "docker-compose.yml"),
            "--project-directory",
            str(tmp_path),
            "--profile",
            "app",
            "config",
            "--quiet",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _check_root(project: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project)}
    return subprocess.run(
        [sys.executable, str(CHECK_ROOT)],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_root_check_refuses_root_env(tmp_path: Path) -> None:
    """Catches a root check that reads only `git ls-files`, blind to an ignored root `.env`.

    The clean run first also catches a check that refuses every root.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, timeout=60)
    (tmp_path / ".claude").mkdir()
    shutil.copy(REPO_ROOT / ".claude" / "root-allowlist", tmp_path / ".claude" / "root-allowlist")
    (tmp_path / "README.md").write_text("fake\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(".env\n", encoding="utf-8")

    clean = _check_root(tmp_path)
    assert clean.returncode == 0, clean.stdout + clean.stderr

    (tmp_path / ".env").write_text("LLM_API_KEY=\n", encoding="utf-8")
    dirty = _check_root(tmp_path)
    assert dirty.returncode == 1, dirty.stdout + dirty.stderr
    assert ".env" in dirty.stdout


def test_no_editorconfig() -> None:
    """Catches a cleanup that leaves `.editorconfig` at the root."""
    assert not (REPO_ROOT / ".editorconfig").exists()


def test_ci_scan_names_the_moved_ignore_file() -> None:
    """Catches CI passing the old root `.trivyignore`, or a move that copied or lost the file."""
    workflow = yaml.safe_load(CI.read_text(encoding="utf-8"))
    runs = [
        step["run"]
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if isinstance(step.get("run"), str)
    ]
    found = [m for run in runs for m in re.findall(r"--ignorefile\s+(\S+)", run)]
    assert found, "premise: ci.yml passes --ignorefile at least once"
    assert set(found) == {"infra/.trivyignore"}, found
    assert (REPO_ROOT / "infra" / ".trivyignore").is_file()
    assert not (REPO_ROOT / ".trivyignore").exists()
