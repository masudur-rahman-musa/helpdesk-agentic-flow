"""The container build surface of an earlier change, parsed statically (no image is built).

What is pinned: each of the three per-app Dockerfiles' non-root `USER` and its
direct uvicorn entrypoint, the web `Dockerfile`'s non-root `USER` and its
standalone source, the `.dockerignore` exclusions, the compose `app` profile's
four build-only services, the three backing services staying profile-free, the
read-only policy-corpus mount into mcp-server, and the image-scan job's coverage
of the four built images.

The restructure split the one `ARG SERVICE`/case Dockerfile into three, one per
app (`backend/Dockerfile.<app>`), built from the `backend/` context. Each copies
only `helpdesk/__init__.py`, `helpdesk/shared` and its own app subpackage, and
its entrypoint is a direct `uvicorn helpdesk.<app>.main:app` on its own port.

Why static rather than a real build: a `docker build` of four images is minutes
and needs a daemon, which `make images` and the scan job already run. Every
claim here is a property of a checked-in file, so parsing that file is the only
place it can be proven inside pytest, and none of these cases needs Postgres,
Redis or Qdrant.

Every YAML assertion is made against a `yaml.safe_load` structure rather than
raw text, so reformatting compose or the workflow cannot redden a case. The
Dockerfiles are read as instruction lines with comments stripped, so a comment
mentioning a name it must not build cannot fail the guard either.
"""

import re
from pathlib import Path
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

BACKEND = REPO_ROOT / "backend"
"""The one Python project the restructure merged the three apps into; the build
context of all three app images and where their Dockerfiles live."""

# One entry per app: the Dockerfile, the importable app subpackage, and the port
# its entrypoint binds. The three replace the single `SERVICE`-arg Dockerfile.
APP_DOCKERFILES: tuple[tuple[str, str, str], ...] = (
    ("agent-api", "agent_api", "8000"),
    ("mcp-server", "mcp_server", "8081"),
    ("scheduler", "scheduler", "8082"),
)
"""`(app, module, port)` per Python image, the thing each per-app Dockerfile
bakes. `web` is deliberately absent: it is built from `web/Dockerfile` off
Next's standalone output and shares none of the uv install above the entrypoint
line."""

PYTHON_DOCKERFILES: tuple[Path, ...] = tuple(
    BACKEND / f"Dockerfile.{app}" for app, _module, _port in APP_DOCKERFILES
)
"""The three per-app Dockerfiles, resolved as paths for the cases that read one."""

APP_ENTRYPOINTS: dict[str, str] = {
    "agent_api": "helpdesk.agent_api.api.router:app",
    "mcp_server": "helpdesk.mcp_server.main:app",
    "scheduler": "helpdesk.scheduler.main:app",
}
"""The ASGI app each module serves. agent-api's app lives in `api/router.py`
since an earlier change; the other two keep `main`. Read by both entrypoint cases, so the
image and the watch-mode override name the same target."""

WEB_DOCKERFILE = REPO_ROOT / "web" / "Dockerfile"
DOCKERIGNORE = BACKEND / ".dockerignore"
COMPOSE = REPO_ROOT / "docker-compose.yml"
NEXT_CONFIG = REPO_ROOT / "web" / "next.config.ts"
IMAGE_SCAN_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "image-scan.yml"

PYTHON_SERVICES: tuple[str, ...] = tuple(app for app, _module, _port in APP_DOCKERFILES)
"""The three app names, each built from its own `backend/Dockerfile.<app>`. `web`
is built from `web/Dockerfile` and is not one of them."""

COMPOSE_APP_SERVICES: tuple[str, ...] = (
    "helpdesk-agent-api",
    "helpdesk-mcp-server",
    "helpdesk-scheduler",
    "helpdesk-web",
)
"""The four compose services the `app` profile adds, each with a `build:` block
and no `image:` key so that an earlier change's pinned-image set does not move."""

APP_PROFILE = "app"
"""The one profile name the four app services carry, asserted as the exact list
`["app"]` rather than a membership test, so a second profile has to be argued."""

BACKING_SERVICES: tuple[str, ...] = ("postgres", "redis", "qdrant")
"""The three services a bare `docker compose up -d` must still start, which is
only true while none of them declares a profile at all."""

BUILT_IMAGES: tuple[str, ...] = COMPOSE_APP_SERVICES
"""The four images the scan job builds and scans, named identically to the
compose services that build them."""

PRIVATE_PATHS: tuple[str, ...] = (
    "tests",
    "dev-process",
    ".env",
    "notes.md",
    "his.md",
    "pl",
    ".git",
    "docs",
    "evals",
)
"""The nine paths no image may carry: the six of criterion 6 plus version
control, the docs tree and the evaluation tree. Asserted as a required
subset, not as the whole file: the build artefacts a real image must also
exclude (`.venv`, `node_modules`, `.next`) are the implementation's business."""

NON_ROOT_FORBIDDEN: frozenset[str] = frozenset({"root", "0"})
"""The two `USER` arguments that are not a non-root uid. `0` is the numeric
spelling of the same account, which is how a "non-root" image ships as root."""

TRIVY_FLAGS: tuple[str, ...] = ("--ignore-unfixed", "--severity HIGH,CRITICAL")
"""The two flags every trivy invocation carries, matching the weekly base-image
scan an earlier change pinned."""

STANDALONE_OUTPUT = re.compile(r"""output\s*:\s*['"]standalone['"]""")
"""`output: "standalone"` in `next.config.ts`, either quote style."""

_INSTRUCTION = re.compile(r"^\s*([A-Za-z]+)\s+(.*)$")


def _instructions(path: Path) -> list[tuple[str, str]]:
    """One Dockerfile as (instruction, argument) pairs, comments and blanks dropped.

    Comments are stripped so that a line explaining why `web` is built elsewhere
    cannot fail the guard that says `web` is not a value this file accepts.
    """
    assert path.is_file(), f"{path.relative_to(REPO_ROOT)} does not exist"
    pairs: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _INSTRUCTION.match(line)
        if match is None:
            continue
        pairs.append((match.group(1).upper(), match.group(2).strip()))
    return pairs


def _instruction_text(path: Path) -> str:
    """The same file flattened to searchable text, comments already dropped."""
    return "\n".join(f"{name} {argument}" for name, argument in _instructions(path))


def _assert_non_root_user_after_the_last_copy(path: Path) -> None:
    """A `USER` naming a non-root account, placed after the last `COPY`.

    The ordering is the point: a `USER appuser` written before the copies is
    undone by every later `COPY --chown=root`, and the process still starts as
    uid 0.
    """
    pairs = _instructions(path)
    copies = [index for index, (name, _) in enumerate(pairs) if name == "COPY"]
    assert copies, f"{path.relative_to(REPO_ROOT)} copies nothing"

    users = [
        (index, argument)
        for index, (name, argument) in enumerate(pairs)
        if name == "USER" and index > copies[-1]
    ]
    assert users, f"{path.relative_to(REPO_ROOT)} declares no USER after its last COPY"

    for _, argument in users:
        account = argument.split(":", 1)[0].strip()
        assert account not in NON_ROOT_FORBIDDEN, f"USER {argument} runs as root"


def _compose_services() -> dict[str, Any]:
    """The compose file's `services` mapping."""
    assert COMPOSE.is_file(), "docker-compose.yml does not exist"
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert isinstance(document, dict), "docker-compose.yml is not a YAML mapping"
    services = document.get("services")
    assert isinstance(services, dict), "docker-compose.yml declares no services"
    return services


def _compose_service(name: str) -> dict[str, Any]:
    """One compose service, failing with the service list when it is missing."""
    services = _compose_services()
    service = services.get(name)
    assert isinstance(service, dict), f"no compose service {name}, services are {sorted(services)}"
    return service


def _workflow_step_texts() -> list[str]:
    """Every step of the image-scan workflow flattened to searchable text."""
    assert IMAGE_SCAN_WORKFLOW.is_file(), "image-scan.yml does not exist"
    document = yaml.safe_load(IMAGE_SCAN_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(document, dict), "image-scan.yml is not a YAML mapping"
    jobs = document.get("jobs")
    assert isinstance(jobs, dict), "image-scan.yml declares no jobs mapping"

    texts: list[str] = []
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            parts = [str(step.get("run", "")), str(step.get("uses", ""))]
            arguments = step.get("with")
            if isinstance(arguments, dict):
                parts.extend(f"{key}: {value}" for key, value in arguments.items())
            texts.append("\n".join(parts))
    assert texts, "image-scan.yml declares no steps"
    return texts


@pytest.mark.parametrize(
    ("app", "module", "port"),
    APP_DOCKERFILES,
    ids=[app for app, _m, _p in APP_DOCKERFILES],
)
def test_each_app_dockerfile_entrypoints_its_own_app(app: str, module: str, port: str) -> None:
    """Catches an app's Dockerfile wired to a neighbour's module or port, which
    the split makes possible: the one `ARG SERVICE`/case file is gone, so each
    `backend/Dockerfile.<app>` must run `uvicorn helpdesk.<app>.main:app` on its
    own port, carry no leftover `SERVICE` build arg, and copy only the shared
    library and its own app subpackage (never a sibling's). A file that started
    the agent API from `Dockerfile.scheduler` would fail only when a container
    runs, which is what this reads off the entrypoint line instead."""
    dockerfile = BACKEND / f"Dockerfile.{app}"
    text = _instruction_text(dockerfile)

    assert APP_ENTRYPOINTS[module] in text, (
        f"Dockerfile.{app} does not entrypoint {APP_ENTRYPOINTS[module]}"
    )
    assert f"--port {port}" in text or f'"{port}"' in text, (
        f"Dockerfile.{app} does not bind port {port}"
    )
    assert "ARG SERVICE" not in text, f"Dockerfile.{app} still declares an ARG SERVICE"

    copies = [argument for name, argument in _instructions(dockerfile) if name == "COPY"]
    app_copies = [
        c for c in copies if "helpdesk/" in c and "shared" not in c and "__init__" not in c
    ]
    sibling_modules = [m for other, m, _p in APP_DOCKERFILES if other != app]
    for argument in app_copies:
        for sibling_module in sibling_modules:
            assert f"helpdesk/{sibling_module}" not in argument, (
                f"Dockerfile.{app} copies a sibling package: {argument!r}"
            )
    assert any(f"helpdesk/{module}" in c for c in copies), (
        f"Dockerfile.{app} copies its own package nowhere, its COPYs are {copies}"
    )
    assert any("helpdesk/shared" in c for c in copies), (
        f"Dockerfile.{app} never copies helpdesk/shared, its COPYs are {copies}"
    )


@pytest.mark.parametrize("dockerfile", PYTHON_DOCKERFILES, ids=[p.name for p in PYTHON_DOCKERFILES])
def test_each_app_dockerfile_runs_as_a_non_root_user(dockerfile: Path) -> None:
    """Catches an image whose process is uid 0, where a container escape starts
    with root on the host mount; catches equally a `USER` placed before the last
    `COPY`, which a later `COPY --chown=root` silently undoes. Each of the three
    per-app Dockerfiles is its own file and gets no guarantee from the others."""
    _assert_non_root_user_after_the_last_copy(dockerfile)


def test_the_web_dockerfile_runs_as_a_non_root_user() -> None:
    """Catches the same root process in the web image, which the earlier case
    cannot see: `web/Dockerfile` is a separate file over Next's output and gets
    no guarantee from the root one."""
    _assert_non_root_user_after_the_last_copy(WEB_DOCKERFILE)


def test_the_web_image_is_built_from_the_standalone_output() -> None:
    """Catches a web image that copies the whole `node_modules` tree instead of
    Next's standalone server, and catches the reverse half: a Dockerfile copying
    `.next/standalone` from a build that was never told to emit it, which fails
    only at build time in CI."""
    config = NEXT_CONFIG.read_text(encoding="utf-8")
    assert STANDALONE_OUTPUT.search(config), 'next.config.ts declares no output: "standalone"'

    copies = [argument for name, argument in _instructions(WEB_DOCKERFILE) if name == "COPY"]
    assert any(".next/standalone" in argument for argument in copies), (
        f"web/Dockerfile copies no .next/standalone, its copies are {copies}"
    )


@pytest.mark.parametrize("path", PRIVATE_PATHS)
def test_the_dockerignore_excludes_every_private_path(path: str) -> None:
    """Catches a private path shipped inside an image: `.env` is the live
    secret, `notes.md`, `his.md` and `pl` are gitignored working files, and
    `tests`, `dev-process`, `docs` and the two evaluation trees are weight and
    disclosure with nothing to run. Catches equally a re-admitting negation
    (`!.env`) added below the exclusion, which reads as an exclusion until the
    build copies the file in anyway."""
    assert DOCKERIGNORE.is_file(), ".dockerignore does not exist"
    entries = [
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    excluded = {entry.rstrip("/") for entry in entries if not entry.startswith("!")}
    readmitted = {entry.lstrip("!").rstrip("/") for entry in entries if entry.startswith("!")}

    assert path in excluded, f".dockerignore does not exclude {path}"
    assert path not in readmitted, f".dockerignore re-admits {path} with a negation"


@pytest.mark.parametrize("name", COMPOSE_APP_SERVICES)
def test_the_app_profile_declares_the_four_services(name: str) -> None:
    """Catches an app service left out of the `app` profile, which would start
    on a bare `docker compose up -d` and race the host process on the same port;
    catches equally an `image:` key on a built service, which would add a fourth
    name to the set an earlier change's weekly scan is pinned to and redden that case for
    an image no registry holds."""
    service = _compose_service(name)

    assert service.get("profiles") == [APP_PROFILE], (
        f"{name} declares profiles {service.get('profiles')!r}, expected ['{APP_PROFILE}']"
    )
    assert "build" in service, f"{name} declares no build block"
    assert "image" not in service, f"{name} pins an image: {service.get('image')!r}"


@pytest.mark.parametrize("name", BACKING_SERVICES)
def test_the_backing_services_carry_no_profile(name: str) -> None:
    """Catches a profile added to Postgres, Redis or Qdrant while wiring the app
    profile: with one, a bare `docker compose up -d` starts nothing, and every
    host-run service and every test suite loses its database with no error
    naming the cause."""
    service = _compose_service(name)

    assert "profiles" not in service, f"{name} declares profiles {service.get('profiles')!r}"


def test_the_policy_dir_is_mounted_read_only_into_the_mcp_server() -> None:
    """Catches a writable policy mount, where a compromised tool process edits
    the corpus that decides access; catches equally the corpus baked into any
    app image, which makes the mount cosmetic and pins the corpus to the build.
    The host side is now `./backend/data/policies` after the restructure."""
    service = _compose_service("helpdesk-mcp-server")
    volumes = service.get("volumes")
    assert isinstance(volumes, list) and volumes, "helpdesk-mcp-server mounts nothing"

    mounts = [str(volume) for volume in volumes if "policies" in str(volume)]
    assert mounts, f"helpdesk-mcp-server mounts no policies path, volumes are {volumes}"
    for mount in mounts:
        assert mount.endswith(":ro"), f"the policy mount is writable: {mount}"
        assert mount.startswith("./backend/data/policies"), (
            f"the policy mount is not ./backend/data/policies: {mount}"
        )

    for dockerfile in PYTHON_DOCKERFILES:
        copies = [argument for name, argument in _instructions(dockerfile) if name == "COPY"]
        baked = [argument for argument in copies if "policies" in argument]
        assert baked == [], f"{dockerfile.name} bakes the policy corpus: {baked}"


@pytest.mark.parametrize("image", BUILT_IMAGES)
def test_the_image_scan_job_covers_the_four_built_images(image: str) -> None:
    """Catches one of the four images built and never scanned, which is the
    image nobody hears about when a HIGH lands in its base layer; catches
    equally a scan wired without `--ignore-unfixed`, which leaves the job
    permanently red, and without the severity gate, which buries the finding."""
    texts = _workflow_step_texts()

    scanning = [text for text in texts if image in text and "trivy" in text]
    assert scanning, f"image-scan.yml runs no trivy step naming {image}"

    for text in scanning:
        for flag in TRIVY_FLAGS:
            assert flag in text, f"the trivy step for {image} carries no {flag}"


# --- an earlier change: the pinned base images and the web runtime's attack surface -----

IMAGE_FLOORS: tuple[tuple[str, tuple[Path, ...], str, tuple[int, ...], str], ...] = (
    (
        "PYTHON_IMAGE",
        PYTHON_DOCKERFILES,
        "python:3.12.15-slim-bookworm",
        (3, 12, 15),
        "2026-10-12",
    ),
    (
        "UV_IMAGE",
        PYTHON_DOCKERFILES,
        "ghcr.io/astral-sh/uv:0.12.19",
        (0, 12, 19),
        "2026-09-28",
    ),
    (
        "NODE_IMAGE",
        (WEB_DOCKERFILE,),
        "node:22.23.3-bookworm-slim",
        (22, 23, 3),
        "2026-09-28",
    ),
)
"""One row per pinned base image: the ARG name, the files declaring it, the floor
tag, that tag as a version tuple, and the date the floor was measured.

The two base ARGs are now declared once in each of the three per-app
Dockerfiles, so each row names every file that pins the ARG and the floor is
checked in all of them; one image left behind on a bump reddens here. The node
base lives only in the web Dockerfile.

The date is the point of the table. A floor equal to today's pin passes forever
and proves nothing about tomorrow, so the row records WHEN the tag was checked
against trivy (2026-09-28, the run that found 44 findings in `python:3.12.8`).
Raising a floor is the deliberate act that reddens a stale pin.

Compared as an int tuple, never as a string: "3.12.8" > "3.12.14" is true in
string order and false in every sense that matters here."""

IMAGE_FLOOR_IDS: tuple[str, ...] = tuple(row[0] for row in IMAGE_FLOORS)
"""The three ARG names, used as the parametrize ids so a red names its image."""

EXACT_PATCH_COMPONENTS = 3
"""A pinned tag names major, minor AND patch. Decision A: an exact patch tag is
reproducible and its bump is a visible diff, while a floating `python:3.12`
drifts green and nobody ever sees the move."""

NPM_MODULE_PATH = "/usr/local/lib/node_modules/npm"
"""The bundled npm package the node image ships, which is where the 8 remaining
HIGH findings of decision B live (`pacote`, `sigstore`, `brace-expansion`,
`ip-address`, `picomatch`)."""

NPM_ENTRY_POINTS: tuple[str, ...] = ("/usr/local/bin/npm", "/usr/local/bin/npx")
"""The two symlinks that keep `npm` on PATH after its package tree is gone.
Deleting the tree alone leaves dangling links and `command -v npm` still
succeeds, which is what criterion 6 of section 1 measures."""

_ARG_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(\S+)$")
"""An `ARG NAME=value` argument, split into the name and the pinned reference.
Anchored, so an ARG with no default is not read as a pin of the empty string."""

_TAG_VERSION = re.compile(r":(\d+(?:\.\d+)*)")
"""The numeric run immediately after the tag separator, which reads `3.12.14`
off `python:3.12.14-slim-bookworm` and `0.12.19` off
`ghcr.io/astral-sh/uv:0.12.19`. Applied with `search` on the tag alone, never
on the whole line: a registry host may carry a port, and a suffix such as
`-slim-bookworm` must not be read as version digits."""


def _pinned_image(path: Path, arg_name: str) -> str:
    """The image reference one `ARG NAME=...` line pins, read from `path`.

    Read through `_instructions`, which drops comments, so the commented-out
    `python:3.12.8` an operator leaves above the live line cannot satisfy or
    redden any case below. Fails naming the ARGs the file does declare, so a
    renamed ARG reports the rename rather than a bare KeyError.
    """
    pins: dict[str, str] = {}
    for name, argument in _instructions(path):
        if name != "ARG":
            continue
        match = _ARG_ASSIGNMENT.match(argument)
        if match is not None:
            pins[match.group(1)] = match.group(2)

    assert arg_name in pins, (
        f"{path.relative_to(REPO_ROOT)} declares no ARG {arg_name}=..., "
        f"its pinned ARGs are {sorted(pins)}"
    )
    return pins[arg_name]


def _tag_version(reference: str) -> tuple[int, ...]:
    """The version components of an image reference's tag, as ints.

    Splits on the LAST colon so a registry host with a port keeps its port out
    of the tag, then reads the leading numeric run of that tag.
    """
    assert ":" in reference, f"{reference} pins no tag at all"
    tag = reference.rsplit(":", 1)[1]
    match = _TAG_VERSION.search(f":{tag}")
    assert match is not None, f"{reference} names no version in its tag"
    return tuple(int(part) for part in match.group(1).split("."))


def _runtime_stage_instructions(path: Path) -> list[tuple[str, str]]:
    """The instructions of `path`'s runtime stage only, comments already dropped.

    The stage is identified by its `FROM ... AS runtime` line: the slice starts
    at the instruction after that FROM and runs to the end of the file. This
    matters because `_instructions` flattens all three of `web/Dockerfile`'s
    stages into one list, and a deletion placed in `deps` or `builder` is
    discarded with that stage and never reaches the shipped image.
    """
    pairs = _instructions(path)
    starts = [
        index
        for index, (name, argument) in enumerate(pairs)
        if name == "FROM" and argument.lower().endswith(" as runtime")
    ]
    assert len(starts) == 1, (
        f"{path.relative_to(REPO_ROOT)} declares {len(starts)} `FROM ... AS runtime` stages, "
        f"its FROM lines are {[argument for name, argument in pairs if name == 'FROM']}"
    )
    return pairs[starts[0] + 1 :]


def _assert_not_older_than_the_floor(arg_name: str) -> None:
    """One floor row checked: the pinned tag parses to a tuple at or above its floor.

    Inclusive at the boundary, so the floor tag itself passes and the patch
    below it fails. Compared as int tuples, never as strings, so raising the pin
    above the floor keeps the case green and only a downgrade reddens it.
    """
    rows = [row for row in IMAGE_FLOORS if row[0] == arg_name]
    assert len(rows) == 1, f"the floor table holds {len(rows)} rows for {arg_name}"
    _, paths, floor_tag, floor, measured = rows[0]
    assert paths, f"the floor row for {arg_name} names no file"

    for path in paths:
        reference = _pinned_image(path, arg_name)
        version = _tag_version(reference)

        assert version >= floor, (
            f"{path.relative_to(REPO_ROOT)} pins {arg_name}={reference}, version {version}, "
            f"older than the floor {floor} recorded from {floor_tag} measured {measured}"
        )


def test_the_python_base_is_not_older_than_the_recorded_floor() -> None:
    """Catches the defect this plan fixes: `python:3.12.8-slim-bookworm` sat
    unbumped for nine months and surfaced as 44 CRITICAL/HIGH Debian findings in
    a trivy scan rather than as a red test. Catches equally that bump being
    reverted later. The floor is `(3, 12, 14)`, measured clean 2026-09-28, and it
    is inclusive: `3.12.14` passes, `3.12.13` fails."""
    _assert_not_older_than_the_floor("PYTHON_IMAGE")


def test_the_uv_image_is_not_older_than_the_recorded_floor() -> None:
    """Catches the second stale pin left behind by a fix that moved only the
    Python base: `ghcr.io/astral-sh/uv:0.5.11` is from the same December 2024
    freeze and predates the `revision = 3` lockfile format this repo now writes.
    The floor is `(0, 12, 19)`, measured 2026-09-28, inclusive: `0.12.19` passes,
    `0.12.18` fails."""
    _assert_not_older_than_the_floor("UV_IMAGE")


def test_the_node_base_is_not_older_than_the_recorded_floor() -> None:
    """Catches the web image being left out of a bump that moved the two base
    ARGs, which the other two cases cannot see: `web/Dockerfile` is a separate
    file and `node:22.12.0-bookworm-slim` fails the same scan job for the same
    cause. The floor is `(22, 23, 3)`, measured 2026-09-28, inclusive: `22.23.3`
    passes, `22.23.2` fails."""
    _assert_not_older_than_the_floor("NODE_IMAGE")


@pytest.mark.parametrize(
    ("arg_name", "paths", "floor_tag", "floor", "measured"),
    IMAGE_FLOORS,
    ids=IMAGE_FLOOR_IDS,
)
def test_the_pinned_tags_are_exact_patch_versions(
    arg_name: str,
    paths: tuple[Path, ...],
    floor_tag: str,
    floor: tuple[int, ...],
    measured: str,
) -> None:
    """Catches decision A being undone by a floating tag: `python:3.12-slim-bookworm`
    satisfies the floor case above forever, because a two-component tag reads as
    `(3, 12)` and any patch it silently resolves to still compares greater than
    nothing. A floating pin makes the build unreproducible and makes a base bump
    invisible in the diff, which is exactly how the nine-month staleness happened.
    Asserting three components is what forces a bump to be a line somebody wrote.
    Checked in every file that pins the ARG, so no one app image floats alone."""
    assert len(floor) == EXACT_PATCH_COMPONENTS, (
        f"the floor row for {arg_name} names {len(floor)} components, expected "
        f"{EXACT_PATCH_COMPONENTS}: {floor_tag}"
    )

    for path in paths:
        reference = _pinned_image(path, arg_name)
        version = _tag_version(reference)

        assert len(version) == EXACT_PATCH_COMPONENTS, (
            f"{path.relative_to(REPO_ROOT)} pins {arg_name}={reference}, which names "
            f"{len(version)} version components {version}, expected {EXACT_PATCH_COMPONENTS} "
            f"(major.minor.patch) as of {measured}"
        )


def test_the_web_runtime_stage_removes_npm() -> None:
    """Catches the web image shipping node's bundled npm, which is 8 HIGH findings
    of attack surface for a stage whose only command is `node server.js` and which
    never resolves a package. Catches equally the two halves of a half-removal: a
    deletion of the package tree that leaves `/usr/local/bin/npm` dangling on PATH,
    and a deletion written into the `deps` or `builder` stage, where it is thrown
    away with that stage and never reaches the shipped layer. The stage slice is
    what makes the second case visible: the assertion runs over the instructions
    after `FROM ... AS runtime` only, not over the flattened file."""
    runtime = _runtime_stage_instructions(WEB_DOCKERFILE)
    runs = [argument for name, argument in runtime if name == "RUN"]
    assert runs, "web/Dockerfile's runtime stage runs no commands at all"

    removals = "\n".join(runs)

    assert NPM_MODULE_PATH in removals, (
        f"the web runtime stage never deletes {NPM_MODULE_PATH}; its RUN commands are {runs}"
    )
    for entry_point in NPM_ENTRY_POINTS:
        assert entry_point in removals, (
            f"the web runtime stage leaves the {entry_point} entry point in place, "
            f"so `command -v npm` still succeeds; its RUN commands are {runs}"
        )


# --- an earlier change: watch mode through the compose override example -----------------

OVERRIDE_EXAMPLE = REPO_ROOT / "docker-compose.override.example.yml"
GITIGNORE = REPO_ROOT / ".gitignore"

LOCAL_OVERRIDE_LINE = "docker-compose.override.yml"
"""The exact `.gitignore` line keeping a developer's copied override out of git."""

PYTHON_RELOAD_ROWS: tuple[tuple[str, str, str], ...] = (
    ("agent-api", "agent_api", "8000"),
    ("mcp-server", "mcp_server", "8081"),
    ("scheduler", "scheduler", "8082"),
)
"""One row per Python app service: the compose service suffix, the importable
module holding `main:app`, and the port the base file publishes and probes."""

PYTHON_RELOAD_IDS: tuple[str, ...] = tuple(row[0] for row in PYTHON_RELOAD_ROWS)

HELPDESK_DIR = "/app/helpdesk"
"""Where the one merged `helpdesk` package lives in every Python image after the
restructure. The editable install's `.pth` points at `/app`, so a read-only
mount of the host source over `/app/helpdesk` is all a reloader needs, and it is
also the one `--reload-dir` each service watches."""

HELPDESK_SOURCE_MOUNT = "./backend/helpdesk:/app/helpdesk:ro"
"""The single source mount each Python override service carries: the host
`backend/helpdesk` tree, read-only, over the image's package. The restructure
replaced the old pair of mounts (a shared-lib mount plus a per-service mount)."""

VENV_DIR = "/app/.venv"
"""The image's virtualenv, which no mount may land on or under."""

WEB_SERVICE = "helpdesk-web"
WEB_DEV_IMAGE = "helpdesk-web:dev"
WEB_BUILD_TARGET = "deps"
WEB_SOURCE_MOUNT = "./web:/app"
WEB_NAMED_VOLUME_TARGETS: tuple[str, ...] = ("/app/node_modules", "/app/.next")
WEB_INSTALL = "pnpm install --frozen-lockfile"
WEB_DEV_COMMANDS: tuple[str, ...] = ("next dev", "pnpm dev")
"""Either spelling starts the dev server: `pnpm dev` runs the package script
`next dev`, so the case accepts the script or the binary."""

WEB_START_PERIOD = "60s"

ALLOWED_ENVIRONMENT: dict[str, dict[str, str]] = {WEB_SERVICE: {"WATCHPACK_POLLING": "true"}}
"""The whole environment the override example may set, as an exact mapping per
service: web's polling switch, and nothing for any other service."""


def _override_document() -> dict[str, Any]:
    """The override example parsed as a YAML mapping."""
    assert OVERRIDE_EXAMPLE.is_file(), "docker-compose.override.example.yml does not exist"
    document = yaml.safe_load(OVERRIDE_EXAMPLE.read_text(encoding="utf-8"))
    assert isinstance(document, dict), "docker-compose.override.example.yml is not a mapping"
    return document


def _override_services() -> dict[str, Any]:
    """The override example's `services` mapping."""
    services = _override_document().get("services")
    assert isinstance(services, dict), "docker-compose.override.example.yml declares no services"
    return services


def _override_service(name: str) -> dict[str, Any]:
    """One override service, failing with the service list when it is missing."""
    services = _override_services()
    service = services.get(name)
    assert isinstance(service, dict), (
        f"the override example has no service {name}, its services are {sorted(services)}"
    )
    return service


def _volume_target(volume: Any) -> str:
    """The container-side path of one compose volume, short or long syntax."""
    if isinstance(volume, dict):
        return str(volume.get("target", ""))
    parts = str(volume).split(":")
    return parts[1] if len(parts) > 1 else parts[0]


def _volume_source(volume: Any) -> str:
    """The host-side source of one compose volume, empty for an anonymous one."""
    if isinstance(volume, dict):
        return str(volume.get("source", ""))
    parts = str(volume).split(":")
    return parts[0] if len(parts) > 1 else ""


def _flag_values(arguments: list[str], flag: str) -> list[str]:
    """Every value that immediately follows `flag` in an argument list."""
    return [
        arguments[index + 1]
        for index, argument in enumerate(arguments)
        if argument == flag and index + 1 < len(arguments)
    ]


def _environment_keys(environment: Any) -> set[str]:
    """The variable names one `environment` block sets, mapping or list syntax."""
    if environment is None:
        return set()
    if isinstance(environment, dict):
        return {str(key) for key in environment}
    return {str(entry).split("=", 1)[0] for entry in environment}


@pytest.mark.parametrize(("service", "module", "port"), PYTHON_RELOAD_ROWS, ids=PYTHON_RELOAD_IDS)
def test_the_override_example_reloads_every_python_service(
    service: str, module: str, port: str
) -> None:
    """Catches a Python service left on its production entrypoint (no `--reload`),
    an entrypoint copied from agent-api that serves `helpdesk.agent_api.api.router:app`
    on 8000 inside mcp-server or scheduler, a server bound to loopback that the
    published port cannot reach, and a reload dir widened to `/app`, where the
    watcher walks the whole virtualenv, instead of the one `/app/helpdesk` tree."""
    entrypoint = _override_service(f"helpdesk-{service}").get("entrypoint")
    assert isinstance(entrypoint, list), (
        f"helpdesk-{service} declares no list-form entrypoint: {entrypoint!r}"
    )
    arguments = [str(argument) for argument in entrypoint]

    assert arguments[:2] == ["uvicorn", APP_ENTRYPOINTS[module]], arguments
    assert _flag_values(arguments, "--host") == ["0.0.0.0"], arguments
    assert _flag_values(arguments, "--port") == [port], arguments
    assert "--reload" in arguments, arguments
    assert _flag_values(arguments, "--reload-dir") == [HELPDESK_DIR], arguments


@pytest.mark.parametrize("service", PYTHON_SERVICES)
def test_the_override_example_mounts_python_source_read_only(service: str) -> None:
    """Catches a writable source mount, where the container can edit the host
    tree; catches a mount of the whole repo, which drags in what the image never
    carried; and catches a missing `helpdesk` mount, which leaves edits to the
    merged package invisible to a reloader watching an unchanged image copy.
    After the restructure every Python service carries the one same mount."""
    volumes = _override_service(f"helpdesk-{service}").get("volumes")
    assert isinstance(volumes, list), f"helpdesk-{service} mounts nothing: {volumes!r}"

    assert {str(volume) for volume in volumes} == {HELPDESK_SOURCE_MOUNT}
    assert len(volumes) == 1, volumes


def test_the_override_example_never_mounts_over_the_venv() -> None:
    """Catches the host's `.venv` (macOS wheels) mounted over the container's
    Linux virtualenv on ANY service, including web, which the per-service exact
    mount case above does not read; a mount at `/app/.venv/lib` shadows a part of
    it just as surely, so the prefix is checked too."""
    targets = [
        _volume_target(volume)
        for service in _override_services().values()
        if isinstance(service, dict)
        for volume in service.get("volumes") or []
    ]
    assert targets, "the override example mounts nothing, so this case would prove nothing"

    shadowing = [
        target
        for target in targets
        if target.rstrip("/") == VENV_DIR or target.startswith(f"{VENV_DIR}/")
    ]
    assert shadowing == [], f"mounts land on the virtualenv: {shadowing}"


def test_the_override_example_runs_web_in_dev_mode() -> None:
    """Catches web left on the production `runtime` stage (no pnpm, no source),
    a dev image without `pull_policy: build` that compose tries to pull from a
    registry holding no `helpdesk-web:dev`, a command that never installs so a
    stale `node_modules` volume breaks start after a lockfile change, and one
    that starts the dev server BEFORE the install, where the install never runs
    while the server is up."""
    web = _override_service(WEB_SERVICE)

    build = web.get("build")
    assert isinstance(build, dict), f"helpdesk-web declares no build mapping: {build!r}"
    assert build.get("target") == WEB_BUILD_TARGET, build
    assert web.get("image") == WEB_DEV_IMAGE, web.get("image")
    assert web.get("pull_policy") == "build", web.get("pull_policy")

    command = web.get("command")
    text = " ".join(str(part) for part in command) if isinstance(command, list) else str(command)
    assert WEB_INSTALL in text, f"helpdesk-web never runs {WEB_INSTALL}: {text!r}"
    starts = [text.find(spelling) for spelling in WEB_DEV_COMMANDS if spelling in text]
    assert starts, f"helpdesk-web starts no dev server: {text!r}"
    assert text.find(WEB_INSTALL) < min(starts), (
        f"the dev server starts before the install: {text!r}"
    )


def test_the_override_example_keeps_web_dependencies_in_named_volumes() -> None:
    """Catches `node_modules` or `.next` left inside the `./web` bind mount, where
    the host's macOS binaries replace the container's Linux ones; catches an
    anonymous volume, which is recreated empty on every `up`; and catches a named
    volume never declared at top level, which compose refuses to start."""
    web = _override_service(WEB_SERVICE)
    volumes = web.get("volumes")
    assert isinstance(volumes, list), f"helpdesk-web mounts nothing: {volumes!r}"
    assert WEB_SOURCE_MOUNT in [str(volume) for volume in volumes], volumes

    declared = _override_document().get("volumes")
    assert isinstance(declared, dict), f"the override example declares no volumes: {declared!r}"

    by_target = {_volume_target(volume): _volume_source(volume) for volume in volumes}
    for target in WEB_NAMED_VOLUME_TARGETS:
        source = by_target.get(target)
        assert source, f"helpdesk-web has no named volume at {target}: {volumes}"
        assert not source.startswith((".", "/", "~")), f"{target} is a bind mount from {source}"
        assert source in declared, f"volume {source} is not declared at top level: {declared}"


def test_the_override_example_gives_web_a_start_period() -> None:
    """Catches a web healthcheck with no grace for the first dev compile, which
    marks the container unhealthy before `/healthz` has ever been built; catches
    equally a `test` restated in the override, which silently diverges from the
    base file's liveness probe."""
    healthcheck = _override_service(WEB_SERVICE).get("healthcheck")
    assert isinstance(healthcheck, dict), f"helpdesk-web has no healthcheck: {healthcheck!r}"

    assert healthcheck.get("start_period") == WEB_START_PERIOD, healthcheck
    assert "test" not in healthcheck, healthcheck


def test_the_override_example_adds_no_service_and_no_profile() -> None:
    """Catches a service the base file does not define (a typo such as
    `helpdesk-api`), which compose starts as a second, half-configured container;
    catches equally a `profiles` key, which on a backing service makes a bare
    `docker compose up -d` start nothing."""
    base = set(_compose_services())
    services = _override_services()

    assert set(services) <= base, f"unknown services: {sorted(set(services) - base)}"
    with_profiles = sorted(
        name for name, body in services.items() if isinstance(body, dict) and "profiles" in body
    )
    assert with_profiles == [], f"services carrying profiles: {with_profiles}"


def test_the_override_example_sets_only_allowlisted_environment() -> None:
    """Catches the override replacing an in-network hostname (`DATABASE_URL`,
    `MCP_URL`, `AGENT_API_URL` pointed at localhost) or carrying a secret, and an
    `env_file` that loads a second credentials file; catches equally the polling
    switch written as a YAML boolean, which compose does not pass through as
    the string Next reads."""
    services = _override_services()

    keys = set().union(
        *(
            _environment_keys(body.get("environment"))
            for body in services.values()
            if isinstance(body, dict)
        )
    )
    assert keys == {"WATCHPACK_POLLING"}, sorted(keys)

    with_env_file = sorted(
        name for name, body in services.items() if isinstance(body, dict) and "env_file" in body
    )
    assert with_env_file == [], f"services carrying env_file: {with_env_file}"

    for name, environment in ALLOWED_ENVIRONMENT.items():
        assert _override_service(name).get("environment") == environment


def test_the_local_override_is_gitignored() -> None:
    """Catches a developer's copied `docker-compose.override.yml` (local ports,
    a gateway URL) showing as untracked and being committed by `git add .`."""
    lines = [line.strip() for line in GITIGNORE.read_text(encoding="utf-8").splitlines()]

    assert LOCAL_OVERRIDE_LINE in lines, f".gitignore carries no line {LOCAL_OVERRIDE_LINE}"
