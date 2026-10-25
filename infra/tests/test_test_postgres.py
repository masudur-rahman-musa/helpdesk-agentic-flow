"""An earlier change: the compose `postgres-test` service and the CI job that starts it.

Static reads of ``docker-compose.yml`` and ``.github/workflows/ci.yml`` through
``yaml.safe_load``, so reformatting cannot redden a case. No service is needed.
"""

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml  # type: ignore[import-untyped]


def _repo_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "backend" / "pyproject.toml").is_file():
            return candidate
    raise AssertionError("repo root not found: no parent holds backend/pyproject.toml")


REPO_ROOT = _repo_root()
COMPOSE = REPO_ROOT / "docker-compose.yml"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"

DATA_DIR = "/var/lib/postgresql/data"


def _load(path: Path) -> dict[str, Any]:
    assert path.is_file(), f"{path.name} does not exist"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict), f"{path.name} is not a YAML mapping"
    return document


def _service(name: str) -> dict[str, Any]:
    services = _load(COMPOSE).get("services")
    assert isinstance(services, dict), "docker-compose.yml declares no services"
    service = services.get(name)
    assert isinstance(service, dict), f"no compose service {name}, services are {sorted(services)}"
    return service


def _tests_job() -> dict[str, Any]:
    jobs = _load(CI).get("jobs")
    assert isinstance(jobs, dict), "ci.yml declares no jobs"
    job = jobs.get("tests")
    assert isinstance(job, dict), f"ci.yml has no tests job, jobs are {sorted(jobs)}"
    return job


def test_the_test_postgres_uses_the_app_postgres_image() -> None:
    """Catches a test server on a different image or tag, which adds a fourth
    image to the pinned set the scan covers and lets the test server drift from
    the version the app runs on."""
    image = _service("postgres").get("image")
    assert image, "the app postgres service pins no image"
    assert _service("postgres-test").get("image") == image


def test_the_test_postgres_publishes_5433_on_loopback_only() -> None:
    """Catches a bare `5433:5432` (open to the whole network), a second port
    entry, or the wrong host port, any of which exposes or misplaces the test
    server."""
    assert _service("postgres-test").get("ports") == ["127.0.0.1:5433:5432"]


def test_the_test_postgres_keeps_no_data_and_runs_no_init_script() -> None:
    """Catches a named volume or bind mount (data or an init script that
    survives a restart, so the server is no longer clean on every start), and a
    tmpfs on the wrong path."""
    service = _service("postgres-test")
    assert "volumes" not in service, f"postgres-test mounts {service.get('volumes')!r}"
    tmpfs = service.get("tmpfs")
    if isinstance(tmpfs, str):
        tmpfs = [tmpfs]
    assert isinstance(tmpfs, list), f"postgres-test has no tmpfs list: {tmpfs!r}"
    assert DATA_DIR in [str(entry).split(":", 1)[0] for entry in tmpfs], tmpfs


def test_the_test_postgres_names_itself_helpdesk_test() -> None:
    """Catches a test server that never sets cluster_name, so the suite's name
    check refuses it, and one with a near-miss name such as `helpdesk-tests`."""
    command = _service("postgres-test").get("command")
    text = " ".join(str(part) for part in command) if isinstance(command, list) else str(command)
    assert re.search(r"cluster_name=helpdesk-test(?![\w-])", text), text


def test_the_test_postgres_carries_no_profile() -> None:
    """Catches a profile on the test server: a bare `docker compose up -d` would
    then skip it and the suite would find nothing on port 5433."""
    service = _service("postgres-test")
    assert "profiles" not in service, f"postgres-test declares profiles {service.get('profiles')!r}"


def test_the_test_postgres_login_matches_the_default_test_url() -> None:
    """Catches a server whose user, password or database differ from the default
    TEST_DATABASE_URL (`helpdesk` / `change-me` / `helpdesk_test`), or that reads
    them from `${...}` lookups, which would tie a throwaway server to env files."""
    environment = _service("postgres-test").get("environment")
    if isinstance(environment, list):
        environment = dict(str(entry).split("=", 1) for entry in environment)
    assert isinstance(environment, dict), f"postgres-test has no environment: {environment!r}"
    assert {k: str(v) for k, v in environment.items() if k.startswith("POSTGRES_")} == {
        "POSTGRES_USER": "helpdesk",
        "POSTGRES_PASSWORD": "change-me",
        "POSTGRES_DB": "helpdesk_test",
    }


def test_ci_starts_the_test_postgres() -> None:
    """Catches a tests job that starts only postgres, redis and qdrant, so the
    suite fails on CI with a refused connection on 5433. The match is on the whole
    word, so `postgres` alone does not count."""
    steps = _tests_job().get("steps")
    assert isinstance(steps, list)
    starting = [
        str(step.get("run", ""))
        for step in steps
        if isinstance(step, dict) and "docker compose up" in str(step.get("run", ""))
    ]
    assert starting, "the tests job has no docker compose up step"
    assert any(re.search(r"(?<![\w-])postgres-test(?![\w-])", run) for run in starting), starting


def test_ci_points_TEST_DATABASE_URL_at_5433() -> None:
    """Catches a tests job that leaves TEST_DATABASE_URL unset or aimed at 5432
    (the app server), or at another database than helpdesk_test."""
    env = _tests_job().get("env")
    assert isinstance(env, dict), f"the tests job has no env mapping: {env!r}"
    url = env.get("TEST_DATABASE_URL")
    assert isinstance(url, str), "the tests job env sets no TEST_DATABASE_URL"
    parts = urlsplit(url)
    assert (parts.hostname, parts.port, parts.path) == ("localhost", 5433, "/helpdesk_test")
