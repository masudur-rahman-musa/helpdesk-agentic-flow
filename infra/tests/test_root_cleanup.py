"""The repo root keeps only what a tool must find there.

What is pinned: the three app services read `backend/.env.demo` then
`backend/.env`, both optional; every `${...}` in `docker-compose.yml` that
names a key of `backend/.env.example` carries a default, one value per name,
so `docker compose config` passes with no env file anywhere; `check_root.py`
refuses a root `.env` even when `.gitignore` hides it from git; `.editorconfig`
is gone; and the config scan in `make security` names the moved
`infra/.trivyignore`.

An earlier change: `backend/.env.demo` is gitignored and untracked, so it is never in a
CI checkout and no case here reads it. The CI `tests` job sets the URLs the
app needs itself, pointed at the ports and Postgres defaults compose
publishes, and makes `JWT_SECRET` fresh on every run with `openssl rand`.

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
import urllib.parse
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
ENV_EXAMPLE = REPO_ROOT / "backend" / ".env.example"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
MAKEFILE = REPO_ROOT / "Makefile"
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


def _env_example_names() -> set[str]:
    """The names `backend/.env.example` lists; its values are placeholders, never read."""
    names: set[str] = set()
    for raw in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        names.add(line.partition("=")[0].strip())
    return names


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


def test_compose_defaults_are_set_and_consistent() -> None:
    """Catches a `${...:?}` left in, a bare `${NAME}` of a setting, or two defaults for one name.

    A bare lookup of a `.env.example` name is empty once no env file exists, and
    two different defaults for one name (the Postgres password in the server and
    in the app DSN) make a stack that starts but cannot log in.
    """
    names = _env_example_names() | set(POSTGRES_NAMES)
    for name in POSTGRES_NAMES:
        assert name in _env_example_names(), f"premise: {name} has a line in backend/.env.example"

    required: list[str] = []
    bare: list[str] = []
    defaults: dict[str, set[str]] = {}
    for text in _strings(_compose()):
        for match in _BRACED.finditer(text):
            name, op, value = match.group(1), match.group(2), match.group(3)
            if op in (":?", "?"):
                required.append(match.group(0))
            elif name not in names:
                continue
            elif op is None or op in (":+", "+"):
                bare.append(match.group(0))
            else:
                defaults.setdefault(name, set()).add(value)
        for match in _UNBRACED.finditer(text):
            if match.group(1) in names:
                bare.append(match.group(0))

    assert required == [], f"required lookups remain: {required}"
    assert bare == [], f"lookups with no default for a .env.example name: {bare}"
    split = {name: values for name, values in defaults.items() if len(values) != 1}
    assert split == {}, f"one name, more than one default: {split}"
    assert set(POSTGRES_NAMES) <= set(defaults), (
        f"the Postgres values carry no default: {set(POSTGRES_NAMES) - set(defaults)}"
    )


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker is not on PATH")
def test_compose_config_without_any_env(tmp_path: Path) -> None:
    """Catches a compose file that needs any env file (root `.env`, `.env.demo`, `.env`) to load.

    Nothing is copied beside the compose file, as in a fresh CI checkout.
    """
    shutil.copy(COMPOSE, tmp_path / "docker-compose.yml")
    (tmp_path / "backend").mkdir()
    (tmp_path / "web").mkdir()
    assert not (tmp_path / ".env").exists()
    assert not (tmp_path / "backend" / ".env.demo").exists()
    assert not (tmp_path / "backend" / ".env").exists()

    names = _env_example_names()
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in names
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


def _security_recipe() -> list[str]:
    """The `make security` recipe commands, backslash continuations joined into one each."""
    lines = MAKEFILE.read_text(encoding="utf-8").splitlines()
    header = re.compile(r"^security\s*:(?![:=])")
    starts = [index for index, line in enumerate(lines) if header.match(line)]
    assert len(starts) == 1, f"expected 1 Makefile rule for 'security', found {len(starts)}"
    commands: list[str] = []
    pending = ""
    for line in lines[starts[0] + 1 :]:
        if not line.startswith("\t"):
            break
        pending += line.strip()
        if pending.endswith("\\"):
            pending = pending[:-1] + " "
            continue
        commands.append(pending)
        pending = ""
    return commands


def test_ci_scan_names_the_moved_ignore_file() -> None:
    """Catches the scan passing the old root `.trivyignore`, or a move that copied or lost the file.

    An earlier change: the config scan moved from `ci.yml` into `make security`,
    so the `--ignorefile` paths are read from that target's recipe.
    """
    found = [m for run in _security_recipe() for m in re.findall(r"--ignorefile\s+(\S+)", run)]
    assert found, "premise: make security passes --ignorefile at least once"
    assert set(found) == {"infra/.trivyignore"}, found
    assert (REPO_ROOT / "infra" / ".trivyignore").is_file()
    assert not (REPO_ROOT / ".trivyignore").exists()


# --- an earlier change: backend/.env.demo out of git, CI sets its own values ---


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _ci() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(CI.read_text(encoding="utf-8"))
    return data


def _tests_job() -> dict[str, Any]:
    job: dict[str, Any] = _ci()["jobs"]["tests"]
    return job


def _tests_job_env() -> dict[str, str]:
    env = _tests_job().get("env") or {}
    return {str(k): str(v) for k, v in env.items()}


def _published_host_ports(service: str) -> set[int]:
    """The host side of every `ports:` entry of one compose service."""
    out: set[int] = set()
    for entry in _compose()["services"][service].get("ports", []):
        if isinstance(entry, dict):
            out.add(int(entry["published"]))
        else:
            parts = str(entry).split(":")
            out.add(int(parts[-2] if len(parts) >= 2 else parts[-1]))
    return out


def _postgres_default(name: str) -> str:
    """The `${NAME:-default}` default the compose postgres service gives one name."""
    raw = str(_compose()["services"]["postgres"]["environment"][name])
    match = _BRACED.fullmatch(raw)
    assert match is not None and match.group(1) == name, f"premise: {name} is ${{{name}:-...}}"
    assert match.group(2) in (":-", "-"), f"premise: {name} carries a default"
    return match.group(3)


def test_env_demo_is_gitignored() -> None:
    """Catches a `.gitignore` that still carries the `!backend/.env.demo` exception."""
    result = _git("check-ignore", "-q", "--no-index", "backend/.env.demo")
    assert result.returncode == 0, result.stdout + result.stderr


def test_env_demo_is_not_tracked() -> None:
    """Catches a change that ignores the file but never runs `git rm --cached` on it."""
    result = _git("ls-files", "--", "backend/.env.demo")
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    ("name", "scheme", "service", "port"),
    [
        ("DATABASE_URL", "postgresql+asyncpg", "postgres", 5432),
        ("REDIS_URL", "redis", "redis", 6379),
        ("QDRANT_URL", "http", "qdrant", 6333),
    ],
)
def test_ci_tests_job_points_at_the_compose_ports(
    name: str, scheme: str, service: str, port: int
) -> None:
    """Catches a CI URL that is missing, on the wrong port, or on a port compose never publishes."""
    env = _tests_job_env()
    assert name in env, f"the tests job env sets no {name}: {sorted(env)}"
    url = urllib.parse.urlsplit(env[name])
    assert url.scheme == scheme, env[name]
    assert url.hostname == "localhost", env[name]
    assert url.port == port, env[name]
    assert port in _published_host_ports(service), (
        f"compose {service} does not publish {port}: {_published_host_ports(service)}"
    )


@pytest.mark.parametrize(
    ("part", "compose_name"),
    [("username", "POSTGRES_USER"), ("password", "POSTGRES_PASSWORD"), ("database", "POSTGRES_DB")],
)
def test_ci_database_url_matches_the_compose_postgres_defaults(
    part: str, compose_name: str
) -> None:
    """Catches a CI DSN whose user, password or database differs from what compose creates."""
    env = _tests_job_env()
    assert "DATABASE_URL" in env, f"the tests job env sets no DATABASE_URL: {sorted(env)}"
    url = urllib.parse.urlsplit(env["DATABASE_URL"])
    got = {
        "username": urllib.parse.unquote(url.username or ""),
        "password": urllib.parse.unquote(url.password or ""),
        "database": url.path.removeprefix("/"),
    }[part]
    assert got == _postgres_default(compose_name), env["DATABASE_URL"]


_JWT_LINE = re.compile(r"JWT_SECRET=\$\(openssl rand -hex 32\).*>>\s*\"?\$GITHUB_ENV\"?")


def _keys(node: object) -> list[str]:
    """Every mapping key in a parsed YAML tree."""
    if isinstance(node, dict):
        return [str(k) for k in node] + [k for v in node.values() for k in _keys(v)]
    if isinstance(node, list):
        return [k for item in node for k in _keys(item)]
    return []


def test_ci_jwt_secret_is_random_per_run() -> None:
    """Catches a literal `JWT_SECRET` in `ci.yml`, a shorter secret, or one set after the suite.

    The step must come before the pytest slice and carry no `if:`, so every shard gets it.
    """
    assert "JWT_SECRET" not in _keys(_ci()), "ci.yml sets JWT_SECRET as a literal value"

    steps: list[dict[str, Any]] = _tests_job()["steps"]
    writers = [
        i
        for i, step in enumerate(steps)
        if isinstance(step.get("run"), str) and _JWT_LINE.search(step["run"])
    ]
    suite = [
        i
        for i, step in enumerate(steps)
        if isinstance(step.get("run"), str) and "test_shard.py" in step["run"]
    ]
    assert suite, "premise: the tests job runs the pytest slice"
    assert len(writers) == 1, f"steps writing JWT_SECRET from openssl rand -hex 32: {writers}"
    assert writers[0] < suite[0], "JWT_SECRET is written after the suite runs"
    assert "if" not in steps[writers[0]], "JWT_SECRET is written on some shards only"


def test_ci_writes_no_env_demo() -> None:
    """Catches a CI step that copies or writes a `.env.demo` instead of setting values itself."""
    found = [
        f"{job_name}: {text}"
        for job_name, job in _ci()["jobs"].items()
        for step in job.get("steps", [])
        for text in _strings(step.get("run")) + _strings(step.get("with"))
        if ".env.demo" in text
    ]
    assert found == [], found
