"""The security scanning baseline is configured as planned.

What is pinned: the gitleaks config and its three fake-credential exceptions, the
CI `security` job and the four scanners it runs, the weekly image scan over the
three images `docker-compose.yml` pins, Dependabot staying off, the pinning of
every GitHub Action, the STRIDE table, and the presence of a real root
`Dockerfile` carrying no placeholder.

Why the check is static rather than runtime: none of these files is read by the
application at all. They are read by GitHub Actions and by the
scanners themselves, so the only place their shape can be proven inside this
repo is by parsing them. A green CI run proves the job that exists ran; it
cannot prove the job that should exist was not deleted.

Every assertion is made against a parsed structure (`tomllib`, `yaml.safe_load`)
and never against raw text or key order, so reformatting a workflow cannot
redden a case. The cases are red at HEAD because none of the four config files
exists yet, `ci.yml` has only a `check` job, and the project notes is still
the skeleton stub.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest
import tomllib
import yaml  # type: ignore[import-untyped]


def _repo_root() -> Path:
    """The directory holding the root `pyproject.toml`, found by walking upwards.

    Walked rather than indexed off `parents[3]` so that moving this file cannot
    silently point every case at a directory with no config files in it, which
    would turn "the file is missing" into a different failure than the one the
    case reports.
    """
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "backend" / "pyproject.toml").is_file():
            return candidate
    raise AssertionError("repo root not found: no parent holds backend/pyproject.toml")


REPO_ROOT = _repo_root()
"""Every file below is resolved from here."""

GITLEAKS_CONFIG = REPO_ROOT / ".gitleaks.toml"
TRIVYIGNORE = REPO_ROOT / "infra" / ".trivyignore"
DEPENDABOT = REPO_ROOT / ".github" / "dependabot.yml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
IMAGE_SCAN_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "image-scan.yml"
COMPOSE = REPO_ROOT / "docker-compose.yml"
THREAT_MODEL = REPO_ROOT / "dev" / "ops" / "threat-model.md"
APP_DOCKERFILES: tuple[Path, ...] = (
    REPO_ROOT / "backend" / "Dockerfile.agent-api",
    REPO_ROOT / "backend" / "Dockerfile.mcp-server",
    REPO_ROOT / "backend" / "Dockerfile.scheduler",
)
"""The three per-app Dockerfiles the restructure split the one `SERVICE`-arg
root Dockerfile into. Read only to prove each real file EXISTS and carries no
placeholder."""
PRE_COMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"
PYPROJECT = REPO_ROOT / "backend" / "pyproject.toml"
UV_LOCK = REPO_ROOT / "backend" / "uv.lock"
GITLEAKS_BASELINE = REPO_ROOT / ".gitleaks-baseline.json"
"""The paths this module reads. `APP_DOCKERFILES` are read only to prove the
three real per-app files EXIST and name no placeholder, by
`test_the_app_dockerfiles_exist_and_entrypoint_their_app` (an earlier change §8, carried
through the restructure split); their shape is pinned in full by
`test_container_build.py::test_each_app_dockerfile_entrypoints_its_own_app`.

The last four are an earlier change's: the pre-commit config and `pyproject.toml` whose
ruff versions must agree, `uv.lock` as the record of which ruff version this
project actually resolves, and the gitleaks baseline that records the nine
findings living only in history."""

FAKE_CREDENTIAL_PATHS: frozenset[str] = frozenset(
    {".env.example", "docs/demo-accounts.md", "backend/scripts/seed.py"}
)
"""The only three paths allowed to carry a credential-shaped string, because each
one is deliberately fake: a placeholder file, a documented demo password, and
hard-rule-8 seed data. Asserted as an EXACT set by
`test_gitleaks_allowlists_exactly_the_three_fake_credential_paths`, so a fourth
exception has to be argued rather than added."""

RUFF_PRE_COMMIT_REPO = "https://github.com/astral-sh/ruff-pre-commit"
"""The repo entry of `.pre-commit-config.yaml` whose `rev` runs ruff in the hook,
matched exactly so a renamed or forked mirror is a miss rather than a silent pass.
Read by `test_the_pre_commit_ruff_rev_matches_the_dev_dependency`."""

VERSION_NUMBER = re.compile(r"(\d+(?:\.\d+)*)")
"""The numeric part of a version string, used to read `v0.16.5` off a pre-commit
`rev` and `0.16.5` off `uv.lock` as the same three numbers. Applied with `search`
because the pre-commit form carries a leading `v` the lock form does not, and
compared as an int tuple so `0.16.5` sorts above `0.12.11` (string order does
not: "0.12" > "0.16" is false only by luck of the second digit)."""

RUFF_FLOOR_SPECIFIER = re.compile(r"^ruff\s*>=\s*(\d+(?:\.\d+)*)$")
"""`pyproject.toml`'s dev-group ruff entry, whose whole value at HEAD is
`ruff>=0.12`. Anchored so an entry carrying an upper bound or an extra marker is
not silently read as a bare floor by a partial match."""

BASELINED_FINDING_COUNT = 9
"""The exact number of findings `.gitleaks-baseline.json` excuses, every one of
them against a commit already in history (an earlier change decision B, corrected while
implementing: a rename cannot reach a committed line, so all nine are baselined
rather than two). Asserted with the not-at-HEAD property by
`test_the_baseline_records_only_the_historical_findings`, so a tenth entry has to
be argued rather than regenerated in."""

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
    assert isinstance(parsed, dict), (
        f"{path.relative_to(REPO_ROOT)} is not a YAML mapping"
    )
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
    raise AssertionError(
        f"no trigger mapping found, keys are {sorted(map(str, document))}"
    )


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
    assert len(matching) == 1, (
        f"expected 1 step naming {marker!r}, found {len(matching)}"
    )
    return matching[0]


def _version_tuple(raw: str, source: str) -> tuple[int, ...]:
    """The numeric version inside `raw` as an int tuple, failing with `source`.

    Kept local rather than reaching for `packaging`, which this project does not
    declare as a dependency; the three versions compared here are plain dotted
    numbers with no pre-release or epoch part.
    """
    found = VERSION_NUMBER.search(raw)
    assert found is not None, f"{source} carries no version number: {raw!r}"
    return tuple(int(part) for part in found.group(1).split("."))


def _pre_commit_ruff_rev() -> str:
    """The `rev` of the ruff-pre-commit repo entry, asserted to be exactly one."""
    repos = _load_yaml(PRE_COMMIT_CONFIG).get("repos")
    assert isinstance(repos, list) and repos, (
        ".pre-commit-config.yaml declares no repos"
    )

    matching = [
        entry
        for entry in repos
        if isinstance(entry, dict) and str(entry.get("repo")) == RUFF_PRE_COMMIT_REPO
    ]
    assert len(matching) == 1, (
        f"expected 1 {RUFF_PRE_COMMIT_REPO} entry, found {len(matching)}: "
        f"repos are {[entry.get('repo') for entry in repos if isinstance(entry, dict)]}"
    )
    return str(matching[0]["rev"])


def _locked_ruff_version() -> str:
    """The ruff version `uv.lock` resolves, asserted to be a single locked entry.

    The lockfile rather than `importlib.metadata` so the case reads a file this
    repo commits: the answer cannot change with whichever virtualenv happens to
    be active when the suite runs.
    """
    packages = tomllib.loads(UV_LOCK.read_text(encoding="utf-8")).get("package")
    assert isinstance(packages, list) and packages, "uv.lock declares no packages"

    versions = [
        str(entry["version"])
        for entry in packages
        if isinstance(entry, dict) and entry.get("name") == "ruff"
    ]
    assert len(versions) == 1, (
        f"uv.lock locks {len(versions)} ruff versions: {versions}"
    )
    return versions[0]


def _ruff_dev_floor() -> str:
    """The lower bound of `pyproject.toml`'s dev-group `ruff` specifier."""
    groups = tomllib.loads(PYPROJECT.read_text(encoding="utf-8")).get(
        "dependency-groups"
    )
    assert isinstance(groups, dict), "pyproject.toml declares no dependency-groups"
    dev = groups.get("dev")
    assert isinstance(dev, list) and dev, (
        "pyproject.toml declares no dev dependency group"
    )

    floors = [
        match.group(1)
        for match in (RUFF_FLOOR_SPECIFIER.match(str(entry).strip()) for entry in dev)
        if match is not None
    ]
    assert len(floors) == 1, (
        f"expected 1 `ruff>=` dev entry, found {len(floors)}: {floors}"
    )
    return floors[0]


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


def test_the_pre_commit_ruff_rev_matches_the_dev_dependency() -> None:
    """Catches the drift that made CI red at HEAD: a pre-commit rev of `v0.12.11`
    while the project resolves ruff `0.16.5`, so `ruff check` locally and `ruff
    check` in the hook are two different linters and an I001 CI reports cannot be
    reproduced. Asserts the rev IS the locked version, not merely above the
    `>=0.12` floor, which `v0.12.11` already satisfies."""
    rev = _pre_commit_ruff_rev()
    locked = _locked_ruff_version()
    floor = _ruff_dev_floor()

    assert _version_tuple(rev, ".pre-commit-config.yaml rev") == _version_tuple(
        locked, "uv.lock ruff version"
    ), f"pre-commit pins ruff {rev}, uv.lock resolves {locked}"

    assert _version_tuple(locked, "uv.lock ruff version") >= _version_tuple(
        floor, "pyproject.toml ruff floor"
    ), f"uv.lock resolves ruff {locked}, below the pyproject floor {floor}"


def test_the_gitleaks_step_passes_a_baseline_path() -> None:
    """Catches the flag being dropped, which sends the `security` job back to all
    nine findings that made it red. Every one of them lives only in history, across
    seven commits, so no rename can reach any of them and without a baseline the job
    fails forever.

    Also asserts `--redact` is ABSENT, which is load-bearing rather than cosmetic:
    redaction rewrites `Match` and `Secret` to "REDACTED", and gitleaks compares
    both when deciding whether a finding is baselined, so re-adding the flag makes
    every entry stop matching and reddens the job again."""
    run = str(_step_running(_security_job(), "gitleaks").get("run", ""))

    assert "--baseline-path" in run, (
        f"the gitleaks step passes no --baseline-path: {run!r}"
    )

    assert "--redact" not in run, (
        "the gitleaks step passes --redact, which rewrites Match and Secret to "
        f"REDACTED so no baseline entry can match: {run!r}"
    )

    flag = re.search(r"--baseline-path[=\s]+(\S+)", run)
    assert flag is not None, f"--baseline-path names no file: {run!r}"

    named = flag.group(1).strip("\"'\\")
    mount = re.search(r'-v\s+"?\$PWD:(/\S+?)"?\s', run)
    assert mount is not None, f"the gitleaks step mounts no $PWD: {run!r}"

    container_root = mount.group(1).rstrip("/")
    assert named.startswith(f"{container_root}/"), (
        f"--baseline-path names {named}, which is not under the mount {container_root}"
    )

    baseline = REPO_ROOT / named[len(container_root) + 1 :]
    assert baseline.is_file(), f"{baseline.relative_to(REPO_ROOT)} does not exist"


def test_the_gitleaks_hook_and_the_ci_job_pass_the_same_baseline() -> None:
    """Catches the divergence that made the baseline unlandable: gitleaks skips its
    own baseline FILE only when `--baseline-path` names it, so a hook without the
    flag reads the baseline's nine entries as fresh leaks and blocks every commit.
    The two invocations are the only two in the repo, and they have to agree on
    which file is the baseline."""
    config = _load_yaml(PRE_COMMIT_CONFIG)
    repos = config.get("repos")
    assert isinstance(repos, list), ".pre-commit-config.yaml declares no repos list"

    hooks = [
        hook
        for repo in repos
        if isinstance(repo, dict)
        for hook in repo.get("hooks", [])
        if isinstance(hook, dict) and hook.get("id") == "gitleaks"
    ]
    assert len(hooks) == 1, f"expected exactly one gitleaks hook, found {len(hooks)}"

    args = [str(arg) for arg in hooks[0].get("args", [])]
    hook_flag = [arg for arg in args if arg.startswith("--baseline-path")]
    assert hook_flag, f"the gitleaks hook passes no --baseline-path: {args}"

    hook_named = hook_flag[0].split("=", 1)[1].strip("\"'")
    assert (REPO_ROOT / hook_named).is_file(), (
        f"the hook's baseline {hook_named} does not exist"
    )

    run = str(_step_running(_security_job(), "gitleaks").get("run", ""))
    job_flag = re.search(r"--baseline-path[=\s]+(\S+)", run)
    assert job_flag is not None, f"the gitleaks step names no baseline: {run!r}"

    job_named = job_flag.group(1).strip("\"'\\").rsplit("/", 1)[-1]
    assert job_named == hook_named.rsplit("/", 1)[-1], (
        f"the hook baselines {hook_named} while the job baselines {job_named}"
    )


def test_the_baseline_records_only_the_historical_findings() -> None:
    """Catches a baseline regenerated wholesale from a dirty run, which is the one
    way this file turns into a blanket excuse.

    The property asserted is that every baselined finding is really HISTORICAL: the
    LINE gitleaks matched no longer reads that way at HEAD. A comparison against
    HEAD's SHA cannot express this, because entries are written by scanning commits
    that already exist and HEAD advances past them on the next commit, so such a
    check is vacuous forever. Comparing the line is not: baseline a finding in live
    code and this case fails while the SHA check passes.

    The line rather than the bare secret, because the fix for the seven test
    constants was renaming the IDENTIFIER while deliberately keeping the value, so
    the value is still in the tree and only the matched line is gone. `.env.example`
    is exempt: its placeholder is unchanged and its whole path is allowlisted.

    Each entry must also name a commit, because gitleaks compares `Commit` when
    matching a baseline entry, and that is what stops an entry covering a later
    secret in the same file and line."""
    assert GITLEAKS_BASELINE.is_file(), ".gitleaks-baseline.json does not exist"
    entries = json.loads(GITLEAKS_BASELINE.read_text(encoding="utf-8"))
    assert isinstance(entries, list), ".gitleaks-baseline.json is not a JSON array"

    assert len(entries) == BASELINED_FINDING_COUNT, (
        f"baseline records {len(entries)} findings, not {BASELINED_FINDING_COUNT}"
    )

    for entry in entries:
        path = str(entry["File"])
        assert str(entry["Commit"]), f"baseline entry for {path} names no commit"

        matched = str(entry["Match"]).strip()
        assert matched, f"baseline entry for {path} records no match to compare"

        tracked = REPO_ROOT / path
        if not tracked.is_file() or path in FAKE_CREDENTIAL_PATHS:
            continue

        lines = tracked.read_text(encoding="utf-8", errors="replace").splitlines()
        assert matched not in (line.strip() for line in lines), (
            f"baseline excuses a line still present in {path}; a finding in "
            "current code is fixed, never baselined"
        )


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
        if text not in scanner_steps
        and ("audit" in text.lower() or "scan" in text.lower())
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
    assert isinstance(depth, int) and not isinstance(depth, bool), (
        f"fetch-depth is {depth!r}"
    )
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
    images = {
        str(service["image"]) for service in services.values() if "image" in service
    }

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


def test_dependabot_is_turned_off() -> None:
    """2026-10-12: the owner turned automated upgrade pull requests off for this
    project. Upgrades are made by hand on `dev`; the `pip-audit`, `pnpm audit` and
    Trivy steps in CI still flag a known hole. Catches the config coming back and
    reopening a weekly stream of pull requests nobody asked for."""
    assert not DEPENDABOT.exists(), f"{DEPENDABOT} exists; Dependabot is turned off"


def test_every_action_is_pinned_to_a_tag() -> None:
    """Catches a `uses: owner/action@master`, a ref that re-points under us so a
    compromised action reaches CI with no diff in this repo (decision G)."""
    refs = _uses_refs(_load_yaml(CI_WORKFLOW)) + _uses_refs(
        _load_yaml(IMAGE_SCAN_WORKFLOW)
    )

    assert len(refs) >= 2, f"only {len(refs)} actions found across both workflows"
    for ref in refs:
        assert ACTION_REF.fullmatch(ref), f"{ref} is not owner/repo@ref"
        assert ref.rsplit("@", 1)[1] not in FLOATING_REFS, (
            f"{ref} is pinned to a branch"
        )


@pytest.mark.parametrize(
    "phrase",
    [phrase for _, phrase in THREAT_ROWS],
    ids=[name for name, _ in THREAT_ROWS],
)
def test_the_threat_model_carries_a_row_for_every_named_threat(phrase: str) -> None:
    """Catches a STRIDE table that skips one of the eight threats this system
    actually has, and catches a row present but answered with a placeholder
    instead of a mitigation that exists in the tree."""
    assert THREAT_MODEL.is_file(), "dev/ops/threat-model.md does not exist"
    rows = _table_rows(THREAT_MODEL.read_text(encoding="utf-8"))
    assert len(rows) >= 9, (
        f"only {len(rows)} table rows found (8 threats plus a header)"
    )

    matching = [row for row in rows if len(row) >= 3 and phrase in row[2].lower()]

    assert matching, f"no scenario cell names {phrase!r}"
    for row in matching:
        assert len(row) == 5, f"row has {len(row)} cells, expected 5: {row}"
        assert all(cell for cell in row), f"row has an empty cell: {row}"
        assert row[3].lower() not in MITIGATION_PLACEHOLDERS, (
            f"mitigation is a stub: {row}"
        )


def test_the_threat_model_has_no_adapt_placeholders() -> None:
    """Catches the skeleton stub being kept and merely extended: `ADAPT` is the
    marker the skeleton leaves where a real answer is owed."""
    assert THREAT_MODEL.is_file(), "dev/ops/threat-model.md does not exist"

    assert "ADAPT" not in THREAT_MODEL.read_text(encoding="utf-8")


def test_the_app_dockerfiles_exist_and_entrypoint_their_app() -> None:
    """Catches the skeleton stub drifting back in under a real file's name: an
    `ADAPT:` placeholder is an instruction nobody carried out, and a Dockerfile
    that never names its own app builds an image whose entrypoint is whatever the
    base image had. The restructure split the one root Dockerfile into three, so
    each `backend/Dockerfile.<app>` must exist and entrypoint its own app."""
    for dockerfile, module in zip(
        APP_DOCKERFILES, ("agent_api", "mcp_server", "scheduler"), strict=True
    ):
        assert dockerfile.is_file(), f"{dockerfile.name} does not exist"
        text = dockerfile.read_text(encoding="utf-8")
        assert f"helpdesk.{module}.main:app" in text, (
            f"{dockerfile.name} never entrypoints helpdesk.{module}.main:app"
        )
        assert "ADAPT:" not in text, (
            f"{dockerfile.name} still carries an ADAPT placeholder"
        )
