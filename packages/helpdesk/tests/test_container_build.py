"""The container build surface of an earlier change, parsed statically (no image is built).

What is pinned: the root `Dockerfile`'s `ARG SERVICE` guard and its non-root
`USER`, the web `Dockerfile`'s non-root `USER` and its standalone source, the
`.dockerignore` exclusions, the compose `app` profile's four build-only
services, the three backing services staying profile-free, the read-only
`policies/` mount into mcp-server, and the image-scan job's coverage of the four
built images.

Why static rather than a real build: a `docker build` of four images is minutes
and needs a daemon, which `make images` and the scan job already run. Every
claim here is a property of a checked-in file, so parsing that file is the only
place it can be proven inside pytest, and none of these cases needs Postgres,
Redis or Qdrant.

Every YAML assertion is made against a `yaml.safe_load` structure rather than
raw text, so reformatting compose or the workflow cannot redden a case. The
Dockerfiles are read as instruction lines with comments stripped, so a comment
mentioning a name it must not build cannot fail the guard either.

The cases are red at HEAD because `Dockerfile`, `web/Dockerfile` and the four
`helpdesk-*` compose services do not exist yet, `next.config.ts` declares no
`output`, and `image-scan.yml` names only the three pinned base images.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]


def _repo_root() -> Path:
    """The directory holding the root `pyproject.toml`, found by walking upwards."""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file() and (candidate / "services").is_dir():
            return candidate
    raise AssertionError("repo root not found: no parent holds pyproject.toml and services/")


REPO_ROOT = _repo_root()
"""Every path below is resolved from here."""

DOCKERFILE = REPO_ROOT / "Dockerfile"
WEB_DOCKERFILE = REPO_ROOT / "web" / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
COMPOSE = REPO_ROOT / "docker-compose.yml"
NEXT_CONFIG = REPO_ROOT / "web" / "next.config.ts"
IMAGE_SCAN_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "image-scan.yml"

PYTHON_SERVICES: tuple[str, ...] = ("agent-api", "mcp-server", "scheduler")
"""The three values the root Dockerfile's `ARG SERVICE` accepts, in full. `web`
is deliberately absent: it is built from `web/Dockerfile` off Next's standalone
output and shares none of the uv install above the entrypoint line."""

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
    ".claude",
    ".git",
    "docs",
    "project-evals",
    "project-obs",
)
"""The eleven paths no image may carry: the seven of criterion 6 plus version
control, the docs tree and the two evaluation trees. Asserted as a required
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


def test_the_root_dockerfile_declares_the_three_service_values() -> None:
    """Catches a Dockerfile that takes `ARG SERVICE` and never checks it, so a
    typo builds an image whose entrypoint is empty and which fails only when a
    container is started; equally catches `web` being folded into this file,
    where the uv install has no Next build to run."""
    text = _instruction_text(DOCKERFILE)

    assert "ARG SERVICE" in text, "the root Dockerfile declares no ARG SERVICE"
    for service in PYTHON_SERVICES:
        assert service in text, f"the root Dockerfile never names {service}"
    assert re.search(r"\bweb\b", text) is None, "the root Dockerfile names web as a service"


def test_the_root_dockerfile_runs_as_a_non_root_user() -> None:
    """Catches an image whose process is uid 0, where a container escape starts
    with root on the host mount; catches equally a `USER` placed before the last
    `COPY`, which a later `COPY --chown=root` silently undoes."""
    _assert_non_root_user_after_the_last_copy(DOCKERFILE)


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
    the corpus that decides access; catches equally `policies/` baked into the
    image, which makes the mount cosmetic and pins the corpus to the build."""
    service = _compose_service("helpdesk-mcp-server")
    volumes = service.get("volumes")
    assert isinstance(volumes, list) and volumes, "helpdesk-mcp-server mounts nothing"

    mounts = [str(volume) for volume in volumes if "policies" in str(volume)]
    assert mounts, f"helpdesk-mcp-server mounts no policies path, volumes are {volumes}"
    for mount in mounts:
        assert mount.endswith(":ro"), f"the policy mount is writable: {mount}"

    copies = [argument for name, argument in _instructions(DOCKERFILE) if name == "COPY"]
    assert [argument for argument in copies if "policies" in argument] == []


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
