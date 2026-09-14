"""The security scanning baseline is configured as planned.

What is pinned: the gitleaks config and its three fake-credential exceptions, the
CI `security` job and the four scanners it runs, the weekly image scan over the
three images `docker-compose.yml` pins, the Dependabot ecosystems, the pinning of
every GitHub Action, the STRIDE table, and the presence of a real root
`Dockerfile` carrying no placeholder.

Why the check is static rather than runtime: none of these files is read by the
application at all. They are read by GitHub Actions, by Dependabot and by the
scanners themselves, so the only place their shape can be proven inside this
repo is by parsing them. A green CI run proves the job that exists ran; it
cannot prove the job that should exist was not deleted.

Every assertion is made against a parsed structure (`tomllib`, `yaml.safe_load`)
and never against raw text or key order, so reformatting a workflow cannot
redden a case. The cases are red at HEAD because none of the four config files
exists yet, `ci.yml` has only a `check` job, and `docs/threat-model.md` is still
the skeleton stub.
"""

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]


def _repo_root() -> Path:
    """The directory holding the root `pyproject.toml`, found by walking upwards.

    Walked rather than indexed off `parents[3]` so that moving this file cannot
    silently point every case at a directory with no config files in it, which
    would turn "the file is missing" into a different failure than the one the
    case reports.
    """
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file() and (candidate / "services").is_dir():
            return candidate
    raise AssertionError("repo root not found: no parent holds pyproject.toml and services/")


REPO_ROOT = _repo_root()
"""Every file below is resolved from here."""

GITLEAKS_CONFIG = REPO_ROOT / ".gitleaks.toml"
TRIVYIGNORE = REPO_ROOT / ".trivyignore"
DEPENDABOT = REPO_ROOT / ".github" / "dependabot.yml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
IMAGE_SCAN_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "image-scan.yml"
COMPOSE = REPO_ROOT / "docker-compose.yml"
THREAT_MODEL = REPO_ROOT / "docs" / "threat-model.md"
DOCKERFILE = REPO_ROOT / "Dockerfile"
"""The eight paths this module reads. `DOCKERFILE` is read only to prove the
real one EXISTS and names no placeholder, by
`test_the_root_dockerfile_exists_and_declares_the_three_services` (an earlier change
§8); its `ARG SERVICE` guard is pinned in full by
`test_container_build.py::test_the_root_dockerfile_declares_the_three_service_values`."""

FAKE_CREDENTIAL_PATHS: frozenset[str] = frozenset(
    {".env.example", "docs/demo-accounts.md", "scripts/seed.py"}
)
"""The only three paths allowed to carry a credential-shaped string, because each
one is deliberately fake: a placeholder file, a documented demo password, and
hard-rule-8 seed data. Asserted as an EXACT set by
`test_gitleaks_allowlists_exactly_the_three_fake_credential_paths`, so a fourth
exception has to be argued rather than added."""

SCANNERS: tuple[str, ...] = ("gitleaks", "pip-audit", "pnpm audit", "trivy")
"""The four scanners the `security` job runs, counted as four by
`test_the_security_job_runs_the_four_planned_scanners`: secrets over history,
Python advisories, npm advisories, and container configuration."""

PINNED_IMAGES: frozenset[str] = frozenset(
    {"postgres:16.6-alpine", "redis:7.4-alpine", "qdrant/qdrant:v1.12.4"}
)
"""The exact images `docker-compose.yml` pins today, read back off the compose
file and matched against the weekly scan by
`test_the_image_scan_covers_exactly_the_three_pinned_compose_images`."""

DEPENDABOT_ECOSYSTEMS: frozenset[str] = frozenset({"uv", "npm", "github-actions"})
"""The three ecosystems this repo has a manifest for: the uv lockfile, `web/`'s
package.json, and the workflow `uses:` refs whose pinning decision G relies on.
Asserted exactly by `test_dependabot_declares_exactly_the_three_planned_ecosystems`."""

ACTION_REF = re.compile(r"[\w.-]+/[\w.-]+(/[\w.-]+)*@[\w.\-+]+")
"""A `uses:` value pinned to a released tag: owner/repo[/path]@ref.

Applied with `re.fullmatch` per a project rule, so a trailing `@master` smuggled
onto the end of an otherwise valid string cannot pass. Feeds
`test_every_action_is_pinned_to_a_tag`."""

FLOATING_REFS: frozenset[str] = frozenset({"master", "main"})
"""The two refs decision G forbids: a branch ref re-points under us, so a
compromised action would be pulled in with no diff here."""

THREAT_ROWS: tuple[tuple[str, str], ...] = (
    ("prompt injection via policy content", "policy"),
    ("prompt injection via ticket bodies", "ticket"),
    ("confused deputy via JWT forwarding", "jwt"),
    ("replay of a mutating tool call", "replay"),
    ("secrets exposure in repo or history", "secret"),
    ("authorization bypass across tenants", "tenant"),
    ("data leakage through logs or telemetry", "telemetry"),
    ("the CSP's unsafe-inline on script-src", "unsafe-inline"),
)
"""The eight threats the STRIDE table owes, as (case id, substring searched for in
the scenario cell, lowercased). The last one is owed by row 27a decision B, which
accepted `'unsafe-inline'` on `script-src` and left the threat open. Feeds
`test_the_threat_model_carries_a_row_for_every_named_threat`."""

MITIGATION_PLACEHOLDERS: frozenset[str] = frozenset({"adapt", "tbd", "", "-"})
"""A mitigation cell holding one of these answers nothing, so the row is a stub
rather than a threat that was answered."""


def _load_yaml(path: Path) -> dict[str, Any]:
    """Parse one YAML document, failing with the path when it is missing."""
    assert path.is_file(), f"{path.relative_to(REPO_ROOT)} does not exist"
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict), f"{path.relative_to(REPO_ROOT)} is not a YAML mapping"
    return parsed


def _triggers(document: dict[str, Any]) -> dict[str, Any]:
    """A workflow's trigger mapping, under whichever key PyYAML produced.

    PyYAML reads YAML 1.1, where the bare word `on` is the BOOLEAN true, so a
    workflow's `on:` block arrives under the key `True` and not `"on"`. Checked
    against the installed PyYAML rather than the GitHub docs.
    """
    mapping: dict[Any, Any] = document
    for key in (True, "on"):
        value = mapping.get(key)
        if isinstance(value, dict):
            return value
    raise AssertionError(f"no trigger mapping found, keys are {sorted(map(str, document))}")


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    """The steps of one job, asserted non-empty so no case reads an empty list."""
    steps = job.get("steps")
    assert isinstance(steps, list) and steps, "the job declares no steps"
    return [step for step in steps if isinstance(step, dict)]


def _step_text(step: dict[str, Any]) -> str:
    """One step flattened to searchable text: its `run`, its `uses` and its `with`.

    Both forms are covered on purpose, because a scanner may be invoked as a
    shell command or as an action with its arguments under `with:`.
    """
    parts: list[str] = [str(step.get("run", "")), str(step.get("uses", ""))]
    arguments = step.get("with")
    if isinstance(arguments, dict):
        parts.extend(f"{key}: {value}" for key, value in arguments.items())
    return "\n".join(parts)


def _security_job() -> dict[str, Any]:
    """The `security` job of `ci.yml`."""
    jobs = _load_yaml(CI_WORKFLOW).get("jobs")
    assert isinstance(jobs, dict), "ci.yml declares no jobs mapping"
    job = jobs.get("security")
    assert isinstance(job, dict), f"ci.yml has no security job, jobs are {sorted(jobs)}"
    return job


def _step_running(job: dict[str, Any], marker: str) -> dict[str, Any]:
    """The one step of `job` whose text names `marker`, asserted to be exactly one."""
    matching = [step for step in _steps(job) if marker in _step_text(step)]
    assert len(matching) == 1, f"expected 1 step naming {marker!r}, found {len(matching)}"
    return matching[0]


def _uses_refs(document: dict[str, Any]) -> list[str]:
    """Every `uses:` string in every job of one workflow document."""
    jobs = document.get("jobs")
    assert isinstance(jobs, dict), "workflow declares no jobs mapping"
    refs: list[str] = []
    for job in jobs.values():
        if isinstance(job, dict):
            refs.extend(str(step["uses"]) for step in _steps(job) if "uses" in step)
    return refs


def _table_rows(markdown: str) -> list[list[str]]:
    """Every markdown table row of a document, as stripped cells, header and
    separator rows dropped."""
    rows: list[list[str]] = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(set(cell) <= {"-", ":"} and cell for cell in cells):
            continue
        rows.append(cells)
    return rows


def test_gitleaks_config_extends_the_default_rules() -> None:
    """Catches a config that replaces the default rule set with our own handful,
    which would stop detecting every secret shape we did not think of."""
    assert GITLEAKS_CONFIG.is_file(), ".gitleaks.toml does not exist"
    config = tomllib.loads(GITLEAKS_CONFIG.read_text(encoding="utf-8"))

    assert config["extend"]["useDefault"] is True


def test_gitleaks_allowlists_exactly_the_three_fake_credential_paths() -> None:
    """Catches a fourth path being allowlisted, which is how a real secret gets
    scanned-but-excused; equally catches a listed path that no longer needs it."""
    assert GITLEAKS_CONFIG.is_file(), ".gitleaks.toml does not exist"
    config = tomllib.loads(GITLEAKS_CONFIG.read_text(encoding="utf-8"))

    allowlists: list[Any] = []
    if isinstance(config.get("allowlist"), dict):
        allowlists.append(config["allowlist"])
    if isinstance(config.get("allowlists"), list):
        allowlists.extend(config["allowlists"])
    assert allowlists, ".gitleaks.toml declares no allowlist"

    paths = {str(path) for entry in allowlists for path in entry.get("paths", [])}

    assert paths == set(FAKE_CREDENTIAL_PATHS)


def test_ci_declares_a_security_job_that_needs_no_other_job() -> None:
    """Catches a `needs: check` that would chain the one job able to go green today
    behind `make verify`, which CI cannot run without a compose stack."""
    job = _security_job()

    assert "needs" not in job, f"the security job waits on {job['needs']}"


def test_the_security_job_runs_the_four_planned_scanners() -> None:
    """Catches a scanner quietly dropped from the job, and equally a fifth
    audit or scan step added without being planned."""
    job = _security_job()
    steps = _steps(job)
    texts = [_step_text(step) for step in steps]

    for scanner in SCANNERS:
        assert any(scanner in text for text in texts), f"no step runs {scanner}"

    scanner_steps = [text for text in texts if any(name in text for name in SCANNERS)]
    assert len(scanner_steps) == len(SCANNERS) == 4

    unplanned = [
        text
        for text in texts
        if text not in scanner_steps and ("audit" in text.lower() or "scan" in text.lower())
    ]
    assert unplanned == []


def test_gitleaks_reads_the_full_history() -> None:
    """Catches the default shallow checkout: `fetch-depth: 1` scans one commit, so
    a secret committed and reverted last month is never found."""
    job = _security_job()
    checkout = _step_running(job, "actions/checkout")
    arguments = checkout.get("with")
    assert isinstance(arguments, dict), "the checkout step passes no `with:` block"

    depth = arguments.get("fetch-depth")
    assert isinstance(depth, int) and not isinstance(depth, bool), f"fetch-depth is {depth!r}"
    assert depth == 0


def test_pip_audit_is_strict_and_carries_no_ignores_today() -> None:
    """Catches a pip-audit that reports without failing, and catches an advisory
    silently excused by an `--ignore-vuln` flag nobody argued for."""
    run = str(_step_running(_security_job(), "pip-audit").get("run", ""))

    assert "--strict" in run
    assert "--ignore-vuln" not in run


def test_pnpm_audit_gates_at_high() -> None:
    """Catches a gate loosened to critical-only, or left at the default level where
    a wall of low advisories makes the step meaningless."""
    run = str(_step_running(_security_job(), "pnpm audit").get("run", ""))

    assert "--audit-level high" in run


def test_the_trivy_config_scan_gates_at_high_and_critical() -> None:
    """Catches a config scan that reports and exits 0, or one that reads only the
    main compose file and misses the override example."""
    text = _step_text(_step_running(_security_job(), "trivy"))

    assert "--severity HIGH,CRITICAL" in text
    assert "--exit-code 1" in text
    assert "docker-compose.yml" in text
    assert "docker-compose.override.example.yml" in text


def test_the_trivyignore_file_exists_and_is_empty_of_entries() -> None:
    """Catches a CVE excused at the moment the gate is introduced, which is how a
    gate ships already-defeated."""
    assert TRIVYIGNORE.is_file(), ".trivyignore does not exist"

    entries = [
        line
        for line in TRIVYIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    assert entries == []


def test_the_image_scan_runs_weekly_and_on_demand() -> None:
    """Catches an image scan wired to `pull_request`, which would redden a PR that
    changed no code the day an upstream CVE lands (decision A)."""
    triggers = _triggers(_load_yaml(IMAGE_SCAN_WORKFLOW))

    assert "schedule" in triggers
    assert any("cron" in entry for entry in triggers["schedule"])
    assert "workflow_dispatch" in triggers
    assert "pull_request" not in triggers


def test_the_image_scan_covers_exactly_the_three_pinned_compose_images() -> None:
    """Catches compose gaining a fourth service the weekly scan never looks at, and
    catches a version bump in compose that the scan file did not follow."""
    services = _load_yaml(COMPOSE).get("services")
    assert isinstance(services, dict), "docker-compose.yml declares no services"
    images = {str(service["image"]) for service in services.values() if "image" in service}

    assert images == set(PINNED_IMAGES)

    scan = IMAGE_SCAN_WORKFLOW.read_text(encoding="utf-8")
    for image in sorted(PINNED_IMAGES):
        assert image in scan, f"image-scan.yml does not name {image}"


def test_the_image_scan_ignores_unfixed() -> None:
    """Catches a scan that fails on CVEs with no upstream fix, which leaves the
    weekly job permanently red and therefore ignored."""
    document = _load_yaml(IMAGE_SCAN_WORKFLOW)
    jobs = document.get("jobs")
    assert isinstance(jobs, dict), "image-scan.yml declares no jobs mapping"

    texts = [
        _step_text(step)
        for job in jobs.values()
        if isinstance(job, dict)
        for step in _steps(job)
        if "trivy" in _step_text(step)
    ]
    assert texts, "image-scan.yml runs no trivy step"

    for text in texts:
        assert "--ignore-unfixed" in text
        assert "--severity HIGH,CRITICAL" in text


def test_dependabot_declares_exactly_the_three_planned_ecosystems() -> None:
    """Catches a manifest left unwatched (or a fourth ecosystem added blind), an
    npm entry pointed at the repo root where there is no package.json, and
    ungrouped updates that open one pull request per bumped package."""
    updates = _load_yaml(DEPENDABOT).get("updates")
    assert isinstance(updates, list) and updates, "dependabot.yml declares no updates"

    ecosystems = {str(entry["package-ecosystem"]) for entry in updates}
    assert ecosystems == set(DEPENDABOT_ECOSYSTEMS)
    assert len(updates) == len(DEPENDABOT_ECOSYSTEMS) == 3

    for entry in updates:
        name = entry["package-ecosystem"]
        assert entry["schedule"]["interval"] == "weekly", f"{name} is not weekly"
        groups = entry.get("groups")
        assert isinstance(groups, dict) and groups, f"{name} groups nothing"
        update_types = {
            str(kind) for group in groups.values() for kind in group.get("update-types", [])
        }
        assert {"minor", "patch"} <= update_types, f"{name} groups {sorted(update_types)}"
        if name == "npm":
            assert entry["directory"] == "/web"


def test_every_action_is_pinned_to_a_tag() -> None:
    """Catches a `uses: owner/action@master`, a ref that re-points under us so a
    compromised action reaches CI with no diff in this repo (decision G)."""
    refs = _uses_refs(_load_yaml(CI_WORKFLOW)) + _uses_refs(_load_yaml(IMAGE_SCAN_WORKFLOW))

    assert len(refs) >= 2, f"only {len(refs)} actions found across both workflows"
    for ref in refs:
        assert ACTION_REF.fullmatch(ref), f"{ref} is not owner/repo@ref"
        assert ref.rsplit("@", 1)[1] not in FLOATING_REFS, f"{ref} is pinned to a branch"


@pytest.mark.parametrize(
    "phrase", [phrase for _, phrase in THREAT_ROWS], ids=[name for name, _ in THREAT_ROWS]
)
def test_the_threat_model_carries_a_row_for_every_named_threat(phrase: str) -> None:
    """Catches a STRIDE table that skips one of the eight threats this system
    actually has, and catches a row present but answered with a placeholder
    instead of a mitigation that exists in the tree."""
    assert THREAT_MODEL.is_file(), "docs/threat-model.md does not exist"
    rows = _table_rows(THREAT_MODEL.read_text(encoding="utf-8"))
    assert len(rows) >= 9, f"only {len(rows)} table rows found (8 threats plus a header)"

    matching = [row for row in rows if len(row) >= 3 and phrase in row[2].lower()]

    assert matching, f"no scenario cell names {phrase!r}"
    for row in matching:
        assert len(row) == 5, f"row has {len(row)} cells, expected 5: {row}"
        assert all(cell for cell in row), f"row has an empty cell: {row}"
        assert row[3].lower() not in MITIGATION_PLACEHOLDERS, f"mitigation is a stub: {row}"


def test_the_threat_model_has_no_adapt_placeholders() -> None:
    """Catches the skeleton stub being kept and merely extended: `ADAPT` is the
    marker the skeleton leaves where a real answer is owed."""
    assert THREAT_MODEL.is_file(), "docs/threat-model.md does not exist"

    assert "ADAPT" not in THREAT_MODEL.read_text(encoding="utf-8")


def test_the_root_dockerfile_exists_and_declares_the_three_services() -> None:
    """Catches the skeleton stub drifting back in under the real file's name: an
    `ADAPT:` placeholder is an instruction nobody carried out, and a root
    Dockerfile that names none of the three services builds an image whose
    entrypoint is whatever the base image had. Flipped by an earlier change §8: an earlier change
    deleted an unbuildable skeleton and left this row to write the real one."""
    assert DOCKERFILE.is_file(), "the root Dockerfile does not exist"
    text = DOCKERFILE.read_text(encoding="utf-8")

    for service in ("agent-api", "mcp-server", "scheduler"):
        assert service in text, f"the root Dockerfile never names {service}"
    assert "ADAPT:" not in text, "the root Dockerfile still carries an ADAPT placeholder"
