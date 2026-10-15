"""The image-publishing surface of an earlier change, parsed statically (nothing is built).

What is pinned: the four images `.github/workflows/build.yml` pushes, the GHCR
path they are pushed under, the Dockerfile and build argument behind each one,
both target architectures, the commit-SHA-only tag rule, the image job's exact
three permissions, the trigger set and the absence of `pull_request`, the
never-cancelling concurrency group, the Trivy gate over every pushed reference
and its position after the push, the SBOM and provenance attestations, that
every build actually pushes, that the emulator and the builder are set up, that
no two builds share a cache scope, the GHCR login, and the two paragraphs
the project notes owes about the published path and the console's wrong one.

Why static rather than a real push: the workflow's claims are properties of a
checked-in file, and a real multi-architecture push needs a registry, a token
and minutes of emulation. A shape error found here costs a pytest run; found at
a deploy it costs a failed rollout. What this file cannot see is a registry
permission, which only the first run on `main` discovers.

Every structural assertion is made against a `yaml.safe_load` of the workflow
rather than raw text, so reformatting it cannot redden a case. Two criteria are
worded over the text itself and are asserted that way as well: the forbidden
floating tags, scoped to the build steps, and `pull_request`, which must be
absent from the whole file including its comments.

Note on `on:`: PyYAML reads YAML 1.1, where the unquoted key `on` is the
BOOLEAN True, not the string "on". A case that looks up `document["on"]` finds
nothing and goes green or red for a reason that has nothing to do with the
triggers, so the trigger block is reached through `_trigger_block()`, which
looks up True first.

The cases are red at HEAD because `.github/workflows/build.yml` does not exist,
and because the project notes carries neither the published-images section nor
the console path-mismatch paragraph.
"""

import re
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]


def _repo_root() -> Path:
    """The directory holding the root `pyproject.toml`, found by walking upwards."""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "backend" / "pyproject.toml").is_file():
            return candidate
    raise AssertionError("repo root not found: no parent holds backend/pyproject.toml")


REPO_ROOT = _repo_root()
"""Every path below is resolved from here."""

BUILD_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build.yml"
DEPLOY_DOC = REPO_ROOT / "dev" / "ops" / "deploy.md"

REGISTRY_PREFIX = "ghcr.io/masudur-rahman-musa/ai-help-desk-governed-access/"
"""The one path prefix every pushed reference carries. It is this repository's
real remote, spelled owner-first; publishing anywhere else publishes to a
namespace we do not own."""

IMAGE_NAMES: frozenset[str] = frozenset(
    {
        "helpdesk-agent-api",
        "helpdesk-mcp-server",
        "helpdesk-scheduler",
        "helpdesk-web",
    }
)
"""The four image names, asserted as an exact set rather than a subset: a fifth
name is an image built on every push that nothing deploys and nobody scans."""

DOCKERFILE_BY_IMAGE: dict[str, str] = {
    "helpdesk-agent-api": "backend/Dockerfile.agent-api",
    "helpdesk-mcp-server": "backend/Dockerfile.mcp-server",
    "helpdesk-scheduler": "backend/Dockerfile.scheduler",
}
"""The three Python images and the per-app Dockerfile each one builds from. The
restructure split the one `SERVICE`-arg Dockerfile into three, so the image is
told apart by its file, not by a build argument. The mapping is asserted per
image, so two steps sharing one Dockerfile (both building the agent-api image)
is a failure rather than a coincidence the set check misses."""

WEB_IMAGE = "helpdesk-web"
"""The fourth image, which shares neither the Dockerfile nor the context of the
three above."""

BACKEND_CONTEXT = "backend"
"""The build context of the three Python images: the one `backend/` project the
restructure merged them into."""

WEB_DOCKERFILE = "web/Dockerfile"
"""The file the web image builds from, named relative to the repository root
even though its build context is `web`."""

WEB_CONTEXT = "web"
"""The build context of the web image: Next's standalone output lives under
`web/`, and a root context copies the whole repository into the daemon."""

PLATFORMS = "linux/amd64"
"""The exact `platforms` value on all four builds. The live node is x86_64, and an
emulated arm64 half made every run 14 to 24 minutes."""

ARCHITECTURES: tuple[str, ...] = ("linux/amd64",)
"""The architectures as separate strings, which is how the project notes has to
name them for a reader who never opens the workflow."""

SHA_TAG_SUFFIX = ":${{ github.sha }}"
"""The only tag suffix any pushed reference carries. A rollback names a SHA, so
a tag that moves makes "the image that was running" unrecoverable."""

FORBIDDEN_TAG_STRINGS: tuple[str, ...] = ("latest", ":main", "0.0.0-unbuilt")
"""The three floating or placeholder tags that may not appear inside a build
step, in structure or in its raw text. `0.0.0-unbuilt` is the local placeholder
`make images` uses and must never reach a registry."""

REQUIRED_PERMISSIONS: dict[str, str] = {
    "contents": "read",
    "packages": "write",
    "id-token": "write",
}
"""The image job's whole `permissions` block, asserted as an exact mapping. A
fourth entry is a scope the run token carries while third-party actions execute
inside the same job."""

PUSH_BRANCHES: list[str] = ["main"]
"""The only branch a push publishes from."""

PUSH_TAGS: list[str] = ["v*"]
"""The only tag pattern a push publishes from."""

FORBIDDEN_TRIGGER = "pull_request"
"""The trigger that may appear nowhere in the file, comments included: a fork's
pull request would reach the GHCR login step while the job holds
`packages: write`."""

CONCURRENCY_REF = "github.ref"
"""The expression the concurrency group is keyed on, so two pushes to different
refs never queue behind one another."""

BUILD_PUSH_ACTION = "docker/build-push-action"
"""The action every build step uses. Only buildx does multi-architecture builds
and attestations, which is why `dev/scripts/build_images.py` is not the publish
path."""

LOGIN_ACTION = "docker/login-action"
"""The action that authenticates against the registry."""

TRIVY_ACTION = "aquasecurity/trivy-action"
"""The action that scans every pushed reference."""

TRIVY_SEVERITY = "CRITICAL,HIGH"
"""The severities the scan gates on, in the action's own spelling."""

TRIVY_EXIT_CODE = "1"
"""The exit code a finding produces. Without it the scan reports and the job
still goes green."""

GHCR_REGISTRY = "ghcr.io"
"""The registry the login step targets."""

RUN_TOKEN = "${{ secrets.GITHUB_TOKEN }}"
"""The password the login step uses: the run's own scoped token, never a
personal access token pasted into a secret."""

CONSOLE_IMAGE_PATH = "developermusa/ai-helpdesk-governed-access"
"""The path the companion console's `infra/k8s/apps/kustomization.yaml` once named.
It is neither this repository's owner nor its spelling; the console has since
moved to this repository's own path, so the doc must not repeat the old one."""

PUBLISHED_IMAGES_HEADING = "Published images"
"""The heading of the section the project notes owes."""

_TRUE_SPELLINGS: frozenset[str] = frozenset({"true"})
"""A GitHub Actions input is a string to the action either way, so `true` and
`"true"` are the same value; both are accepted and nothing else is."""

QEMU_ACTION = "docker/setup-qemu-action"
"""The action that registers the emulator. Its return is the arm64 half coming
back under emulation, which is what made every run 14 to 24 minutes."""

BUILDX_ACTION = "docker/setup-buildx-action"
"""The action that provides the builder the attestations and the GHA cache need."""

_STEP_START = re.compile(r"^\s*-\s+(name|uses):")


def _raw_text() -> str:
    """The workflow file as written, comments included."""
    assert BUILD_WORKFLOW.is_file(), ".github/workflows/build.yml does not exist"
    return BUILD_WORKFLOW.read_text(encoding="utf-8")


def _workflow() -> dict[Any, Any]:
    """The workflow parsed, failing with the path when the file is missing.

    The key type is `Any` rather than `str` because YAML 1.1 turns the unquoted
    `on:` key into the boolean True, so this mapping genuinely holds a key that
    is not a string.
    """
    document = yaml.safe_load(_raw_text())
    assert isinstance(document, dict), "build.yml is not a YAML mapping"
    return document


def _trigger_block() -> dict[str, Any]:
    """The `on:` mapping, reached through the boolean key PyYAML produces.

    YAML 1.1 resolves an unquoted `on` to True, so the string key exists only if
    the author quoted it. Both are accepted; neither being present is a failure
    naming the keys that are.
    """
    document = _workflow()
    for key in (True, "on"):
        block = document.get(key)
        if isinstance(block, dict):
            return block
    raise AssertionError(
        f"build.yml declares no on: block, its keys are {sorted(map(str, document))}"
    )


def _steps() -> list[dict[str, Any]]:
    """Every step of every job, flattened."""
    jobs = _workflow().get("jobs")
    assert isinstance(jobs, dict) and jobs, "build.yml declares no jobs mapping"
    steps: list[dict[str, Any]] = []
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            if isinstance(step, dict):
                steps.append(step)
    assert steps, "build.yml declares no steps"
    return steps


def _steps_using(action: str) -> list[dict[str, Any]]:
    """Every step whose `uses` names one action, version suffix ignored."""
    return [step for step in _steps() if str(step.get("uses", "")).startswith(action)]


def _inputs(step: dict[str, Any]) -> dict[str, Any]:
    """One step's `with:` mapping."""
    arguments = step.get("with")
    assert isinstance(arguments, dict), (
        f"step {step.get('name', step.get('uses'))!r} has no with: block"
    )
    return arguments


def _build_steps() -> list[dict[str, Any]]:
    """The build-push steps, asserted to be four before any case reads them."""
    steps = _steps_using(BUILD_PUSH_ACTION)
    assert len(steps) == len(IMAGE_NAMES), (
        f"build.yml has {len(steps)} {BUILD_PUSH_ACTION} steps, expected {len(IMAGE_NAMES)}"
    )
    return steps


def _tags(step: dict[str, Any]) -> list[str]:
    """One build step's tags as a list, whether written inline or as a block."""
    value = _inputs(step).get("tags")
    assert value is not None, f"build step {step.get('name')!r} pushes no tags"
    return [line.strip() for line in str(value).splitlines() if line.strip()]


def _image_name(reference: str) -> str:
    """The image name inside a full reference, tag and registry path removed."""
    return reference.rsplit(":", 1)[0].rstrip("/").rsplit("/", 1)[-1]


def _build_step_by_image() -> dict[str, dict[str, Any]]:
    """The four build steps keyed by the image name each one pushes."""
    by_image: dict[str, dict[str, Any]] = {}
    for step in _build_steps():
        for reference in _tags(step):
            by_image[_image_name(reference)] = step
    return by_image


def _pushed_references() -> set[str]:
    """Every reference the four build steps push."""
    return {reference for step in _build_steps() for reference in _tags(step)}


def _build_step_blocks() -> list[str]:
    """The raw text of each build step, from its leading dash to the next step.

    Trailing blank and comment lines are dropped, so a comment introducing the
    NEXT step is not read as part of this one.
    """
    lines = _raw_text().splitlines()
    starts = [index for index, line in enumerate(lines) if _STEP_START.match(line)]
    assert starts, "build.yml declares no steps in its raw text"

    blocks: list[str] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        body = lines[start:end]
        while body and (not body[-1].strip() or body[-1].lstrip().startswith("#")):
            body.pop()
        block = "\n".join(body)
        if BUILD_PUSH_ACTION in block:
            blocks.append(block)
    assert blocks, f"no raw step block names {BUILD_PUSH_ACTION}"
    return blocks


def _is_true(value: Any) -> bool:
    """Whether a step input reads as true, in either the boolean or string form."""
    return str(value).strip().lower() in _TRUE_SPELLINGS


def _image_job() -> dict[str, Any]:
    """The job that holds the build steps, which is the job whose token is scoped."""
    jobs = _workflow().get("jobs")
    assert isinstance(jobs, dict) and jobs, "build.yml declares no jobs mapping"
    holding = [
        job
        for job in jobs.values()
        if isinstance(job, dict)
        and any(
            isinstance(step, dict)
            and str(step.get("uses", "")).startswith(BUILD_PUSH_ACTION)
            for step in job.get("steps") or []
        )
    ]
    assert len(holding) == 1, (
        f"{len(holding)} jobs hold {BUILD_PUSH_ACTION} steps, expected 1"
    )
    return holding[0]


def _normalised(value: Any) -> str:
    """One path input, reduced to its repository-relative posix spelling.

    `str.lstrip("./")` is a CHARACTER-set strip, so it turns `../Dockerfile`
    into `Dockerfile` and lets a path that escapes the workspace pass as the
    root one. This resolves the parts instead, so only a genuine `./` prefix
    is removed and a `..` segment survives to fail the comparison.
    """
    parts = [part for part in PurePosixPath(str(value).strip()).parts if part != "."]
    return "/".join(parts)


def _deploy_doc() -> str:
    """the project notes as text."""
    assert DEPLOY_DOC.is_file(), "dev/ops/deploy.md does not exist"
    return DEPLOY_DOC.read_text(encoding="utf-8")


def test_the_four_images_are_built() -> None:
    """Catches a fifth image name added to the workflow, which is an image built
    and pushed on every commit that no manifest deploys and no Trivy step
    scans; catches equally one of the four dropped, which deploys the previous
    commit's image with no error at the push."""
    assert {_image_name(reference) for reference in _pushed_references()} == IMAGE_NAMES


def test_the_registry_path_is_this_repo() -> None:
    """Catches a build step pushing under a path this repository does not own,
    such as the console's `developermusa/...` spelling copied across, where the
    push fails on permissions or lands in a stranger's namespace."""
    for reference in sorted(_pushed_references()):
        assert reference.startswith(REGISTRY_PREFIX), (
            f"{reference} is not published under {REGISTRY_PREFIX}"
        )


@pytest.mark.parametrize("image", sorted(DOCKERFILE_BY_IMAGE))
def test_the_three_python_images_build_from_their_own_dockerfile(image: str) -> None:
    """Catches a Python image built from a neighbour's Dockerfile, or with the
    wrong context: the entrypoint is now baked by which `Dockerfile.<app>` built
    it, so a scheduler image built from `Dockerfile.agent-api` would start the
    agent API and the defect appears only when a container runs. The three share
    the one `backend/` context and carry no `SERVICE` build argument, which the
    split removed."""
    step = _build_step_by_image().get(image)
    assert step is not None, f"no build step pushes {image}"
    arguments = _inputs(step)

    assert _normalised(arguments.get("file", "")) == DOCKERFILE_BY_IMAGE[image], (
        f"{image} builds from {arguments.get('file')!r}, expected {DOCKERFILE_BY_IMAGE[image]}"
    )
    assert _normalised(arguments.get("context", "")) == BACKEND_CONTEXT, (
        f"{image} builds with context {arguments.get('context')!r}, expected {BACKEND_CONTEXT}"
    )

    build_args = [
        line.strip() for line in str(arguments.get("build-args", "")).splitlines()
    ]
    service_args = [arg for arg in build_args if arg.startswith("SERVICE=")]
    assert service_args == [], (
        f"{image} still passes a SERVICE build-arg: {service_args}"
    )


def test_the_web_image_builds_from_its_own_context() -> None:
    """Catches the web image built from the root Dockerfile or the root context,
    which either bakes a Python entrypoint into the web image or ships the whole
    repository into the build daemon; the root-context form still produces a
    runnable image, so only this case sees it."""
    step = _build_step_by_image().get(WEB_IMAGE)
    assert step is not None, f"no build step pushes {WEB_IMAGE}"
    arguments = _inputs(step)

    assert _normalised(arguments.get("context", "")) == WEB_CONTEXT, (
        f"{WEB_IMAGE} builds with context {arguments.get('context')!r}, expected {WEB_CONTEXT}"
    )
    assert _normalised(arguments.get("file", "")) == WEB_DOCKERFILE, (
        f"{WEB_IMAGE} builds from {arguments.get('file')!r}, expected {WEB_DOCKERFILE}"
    )


@pytest.mark.parametrize("image", sorted(IMAGE_NAMES))
def test_every_image_is_built_for_the_live_nodes_architecture(image: str) -> None:
    """Catches a build step left at the runner's default platform, set to arm64,
    which the x86_64 node cannot run, or widened back to an emulated arm64 half,
    which made every run 14 to 24 minutes."""
    step = _build_step_by_image().get(image)
    assert step is not None, f"no build step pushes {image}"

    platforms = str(_inputs(step).get("platforms", "")).replace(" ", "")
    assert platforms == PLATFORMS, (
        f"{image} builds platforms {platforms!r}, expected {PLATFORMS}"
    )


def test_the_only_tag_is_the_commit_sha() -> None:
    """Catches a floating tag added beside the SHA, in structure or in the raw
    text of a build step: with `latest` or `:main` moving under it, a rollback
    can no longer name the image that was running, and a node that repulls gets
    a different build than its neighbour."""
    for step in _build_steps():
        tags = _tags(step)
        assert len(tags) == 1, (
            f"build step {step.get('name')!r} pushes {len(tags)} tags: {tags}"
        )
        assert tags[0].endswith(SHA_TAG_SUFFIX), (
            f"{tags[0]} is not tagged {SHA_TAG_SUFFIX}"
        )

    for block in _build_step_blocks():
        for forbidden in FORBIDDEN_TAG_STRINGS:
            assert forbidden not in block, (
                f"a build step names the floating tag {forbidden!r}"
            )


def test_the_permissions_are_exactly_three() -> None:
    """Catches a fourth permission on the job that runs third-party actions with
    the run token in scope: `contents: write` there lets any action in the build
    push to the repository. Asserted as an exact mapping, so a widened scope
    cannot hide beside the three required ones."""
    job = _image_job()
    permissions = job.get("permissions")

    assert permissions == REQUIRED_PERMISSIONS, (
        f"the image job declares permissions {permissions!r}, expected {REQUIRED_PERMISSIONS}"
    )


def test_the_workflow_has_no_pull_request_trigger() -> None:
    """Catches a `pull_request` trigger added to the publish workflow, where a
    fork's pull request reaches the GHCR login while the job holds
    `packages: write`. Asserted over the raw text as well, because a commented
    out trigger is one uncomment away from live."""
    assert FORBIDDEN_TRIGGER not in _trigger_block(), (
        f"on: declares {FORBIDDEN_TRIGGER}, its keys are {sorted(map(str, _trigger_block()))}"
    )
    assert FORBIDDEN_TRIGGER not in _raw_text(), (
        f"build.yml names {FORBIDDEN_TRIGGER} in its text, comments included"
    )


def test_the_triggers_are_main_tags_and_dispatch() -> None:
    """Catches a publish wired to every branch, or to no branch: `branches: ['*']`
    pushes an image per feature branch under a token with `packages: write`, and
    a missing `workflow_dispatch` leaves no way to republish a SHA by hand after
    a failed run."""
    triggers = _trigger_block()
    push = triggers.get("push")
    assert isinstance(push, dict), f"on: declares push {push!r}, expected a mapping"

    assert push.get("branches") == PUSH_BRANCHES, (
        f"on.push.branches is {push.get('branches')!r}, expected {PUSH_BRANCHES}"
    )
    assert push.get("tags") == PUSH_TAGS, (
        f"on.push.tags is {push.get('tags')!r}, expected {PUSH_TAGS}"
    )
    assert "workflow_dispatch" in triggers, (
        f"on: declares no workflow_dispatch, its keys are {sorted(map(str, triggers))}"
    )


def test_concurrency_never_cancels() -> None:
    """Catches `cancel-in-progress: true`, which kills a running publish when the
    next commit lands and leaves a half-pushed multi-architecture manifest in
    the registry; catches equally a group not keyed on the ref, under which a
    tag build and a main build cancel each other."""
    concurrency = _workflow().get("concurrency")
    assert isinstance(concurrency, dict), (
        f"build.yml declares concurrency {concurrency!r}"
    )

    assert CONCURRENCY_REF in str(concurrency.get("group", "")), (
        f"the concurrency group {concurrency.get('group')!r} is not keyed on {CONCURRENCY_REF}"
    )
    assert str(concurrency.get("cancel-in-progress")).lower() == "false", (
        f"cancel-in-progress is {concurrency.get('cancel-in-progress')!r}, expected false"
    )


def test_every_image_is_trivy_gated() -> None:
    """Catches an image pushed and never scanned, which is the image nobody hears
    about when a CRITICAL lands in its base layer; catches equally a scan wired
    without `exit-code: '1'`, which reports the finding and lets the job go
    green, and without `ignore-unfixed`, which leaves it permanently red."""
    steps = _steps_using(TRIVY_ACTION)
    assert len(steps) == len(IMAGE_NAMES), (
        f"build.yml has {len(steps)} {TRIVY_ACTION} steps, expected {len(IMAGE_NAMES)}"
    )

    scanned = {str(_inputs(step).get("image-ref", "")).strip() for step in steps}
    assert scanned == _pushed_references(), (
        f"the scanned references {sorted(scanned)} are not the pushed ones "
        f"{sorted(_pushed_references())}"
    )

    for step in steps:
        arguments = _inputs(step)
        reference = arguments.get("image-ref")
        assert arguments.get("severity") == TRIVY_SEVERITY, (
            f"the scan of {reference} gates on {arguments.get('severity')!r}, "
            f"expected {TRIVY_SEVERITY}"
        )
        assert _is_true(arguments.get("ignore-unfixed")), (
            f"the scan of {reference} sets ignore-unfixed {arguments.get('ignore-unfixed')!r}"
        )
        assert str(arguments.get("exit-code")).strip() == TRIVY_EXIT_CODE, (
            f"the scan of {reference} exits {arguments.get('exit-code')!r}, "
            f"expected {TRIVY_EXIT_CODE}"
        )


@pytest.mark.parametrize("image", sorted(IMAGE_NAMES))
def test_every_build_carries_an_sbom_and_provenance(image: str) -> None:
    """Catches an image published with no SBOM or no provenance attestation,
    which is the image whose contents cannot be answered for when a CVE lands
    and whose build cannot be tied to a commit. The default of both inputs is
    off, so an omitted key is the failure this case reads."""
    step = _build_step_by_image().get(image)
    assert step is not None, f"no build step pushes {image}"
    arguments = _inputs(step)

    assert _is_true(arguments.get("sbom")), (
        f"{image} sets sbom {arguments.get('sbom')!r}, expected true"
    )
    assert _is_true(arguments.get("provenance")), (
        f"{image} sets provenance {arguments.get('provenance')!r}, expected true"
    )


def test_every_image_is_actually_pushed() -> None:
    """Catches `push: false` on a build step, which is the whole plan silently
    undone: the image builds, the job goes green, the Trivy step then scans
    whatever the registry still holds from the previous commit, and the deploy
    that names this SHA finds no manifest."""
    for step in _build_steps():
        arguments = _inputs(step)
        assert _is_true(arguments.get("push")), (
            f"build step {step.get('name')!r} sets push {arguments.get('push')!r}, expected true"
        )


def test_the_builder_is_set_up_and_the_emulator_is_not() -> None:
    """Catches the buildx setup step being dropped, which loses the attestations
    and the GHA cache, and the QEMU step coming back, which is the emulated arm64
    half returning with its 14 to 24 minute runs."""
    assert _steps_using(BUILDX_ACTION), f"build.yml has no {BUILDX_ACTION} step"
    assert not _steps_using(QEMU_ACTION), f"build.yml still has a {QEMU_ACTION} step"


def test_every_build_has_its_own_cache_scope() -> None:
    """Catches a scope copied from a neighbour, under which two builds share one
    cache entry and evict each other's layers on every run. It never fails a
    build, so the only symptom is a job that quietly rebuilds from scratch
    every time."""
    scopes: list[str] = []
    for step in _build_steps():
        arguments = _inputs(step)
        for key in ("cache-from", "cache-to"):
            value = str(arguments.get(key, ""))
            assert "scope=" in value, (
                f"build step {step.get('name')!r} sets {key} {value!r}, which names no scope"
            )
        step_scopes = {
            str(arguments.get(key, "")).split("scope=", 1)[1].split(",")[0]
            for key in ("cache-from", "cache-to")
        }
        assert len(step_scopes) == 1, (
            f"build step {step.get('name')!r} reads and writes different scopes: {step_scopes}"
        )
        scopes.append(step_scopes.pop())

    assert len(set(scopes)) == len(scopes), (
        f"the four builds share cache scopes: {scopes}"
    )


def test_every_image_is_scanned_after_it_is_pushed() -> None:
    """Catches a Trivy step ordered above the build that produces its reference,
    where the scan pulls the previous commit's image under the same tag, or
    fails on a manifest that does not exist yet. Either way the gate reports on
    something other than what this run publishes."""
    steps = _steps()
    for image, build_step in _build_step_by_image().items():
        reference = _tags(build_step)[0]
        scan_step = next(
            step
            for step in steps
            if str(step.get("uses", "")).startswith(TRIVY_ACTION)
            and str(_inputs(step).get("image-ref", "")).strip() == reference
        )
        assert steps.index(scan_step) > steps.index(build_step), (
            f"{image} is scanned before it is built"
        )


def test_the_login_is_ghcr_with_the_run_token() -> None:
    """Catches a login against Docker Hub or against a long-lived personal access
    token held in a secret: the first pushes four images to the wrong registry,
    the second gives every action in the job a credential that outlives the run
    and is not scoped to this repository."""
    steps = _steps_using(LOGIN_ACTION)
    assert len(steps) == 1, (
        f"build.yml has {len(steps)} {LOGIN_ACTION} steps, expected 1"
    )

    arguments = _inputs(steps[0])
    assert arguments.get("registry") == GHCR_REGISTRY, (
        f"the login targets {arguments.get('registry')!r}, expected {GHCR_REGISTRY}"
    )
    assert str(arguments.get("password", "")).strip() == RUN_TOKEN, (
        f"the login password is {arguments.get('password')!r}, expected the run token"
    )


def test_the_deploy_doc_publishes_the_registry_path() -> None:
    """Catches the workflow landing with no operator-facing record of it: without
    this section a reader deploying by hand has no way to know the registry
    path, that a tag is a commit SHA and never `latest`, or which
    architecture is built, and reads it off the workflow or guesses."""
    document = _deploy_doc()

    assert PUBLISHED_IMAGES_HEADING in document, (
        f"dev/ops/deploy.md has no {PUBLISHED_IMAGES_HEADING!r} section"
    )
    assert REGISTRY_PREFIX in document, (
        f"dev/ops/deploy.md does not name {REGISTRY_PREFIX}"
    )
    for architecture in ARCHITECTURES:
        assert architecture in document, f"dev/ops/deploy.md does not name {architecture}"


def test_the_deploy_doc_no_longer_names_the_old_console_path() -> None:
    """Catches the resolved mismatch coming back as live advice: the console's
    kustomization now names this repository's own GHCR path (fixed there, and
    the note dropped here by an earlier change), so a reader told to expect the old path
    would chase a blocker that no longer exists."""
    document = _deploy_doc()

    assert CONSOLE_IMAGE_PATH not in document, (
        f"dev/ops/deploy.md still names the old console path {CONSOLE_IMAGE_PATH}"
    )
