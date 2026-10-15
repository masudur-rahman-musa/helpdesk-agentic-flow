"""The Qdrant client floor and the local compose server sit on the same minor.

Why static file reads: the client prints its version mismatch warning at runtime
against a live server, which this repo does not run in the fast suite. The place
the match can be proven here is by parsing the two files that set the two
versions: `docker-compose.yml`'s qdrant `image:` tag and `backend/pyproject.toml`'s
`qdrant-client` floor. A green RAG test proves search ran against whatever server
happened to be up; it cannot prove the pinned server and the client floor agree.

Every assertion reads a parsed structure (`yaml.safe_load`, `tomllib`) or a plain
text search, never key order, so reformatting a file cannot redden a case. The
cases are red at HEAD because compose still pins `v1.12.4` and the floor is still
`>=1.12`.
"""

import re
import tomllib
from pathlib import Path

import yaml  # type: ignore[import-untyped]


def _repo_root() -> Path:
    """The directory holding `backend/pyproject.toml`, found by walking upwards.

    Walked rather than indexed off `parents[N]` so moving this file cannot
    silently point every case at a directory with no config files, which would
    turn "the file is missing" into a different failure than the one the case
    reports.
    """
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "backend" / "pyproject.toml").is_file():
            return candidate
    raise AssertionError("repo root not found: no parent holds backend/pyproject.toml")


REPO_ROOT = _repo_root()
"""Every file below is resolved from here."""

COMPOSE = REPO_ROOT / "docker-compose.yml"
PYPROJECT = REPO_ROOT / "backend" / "pyproject.toml"
IMAGE_SCAN_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "image-scan.yml"
DEPLOY_DOC = REPO_ROOT / "dev" / "ops" / "deploy.md"
SECURITY_TEST = REPO_ROOT / "infra" / "tests" / "test_security_config.py"

EXPECTED_SERVER_IMAGE = "qdrant/qdrant:v1.19.1"
"""The exact tag compose's qdrant service must pin: the client is locked at 1.19.0,
so the server matches upward to v1.19.1."""

EXPECTED_MINOR = 19
"""The minor both sides must share. Pinned as an exact int, not a floor, so a
server on v1.18 or a client floor of >=1.12 is a miss rather than a pass."""

QDRANT_IMAGE = re.compile(r"^qdrant/qdrant:v(\d+)\.(\d+)\.(\d+)$")
"""The compose qdrant tag `qdrant/qdrant:vX.Y.Z`, anchored so a trailing suffix
cannot be read as a bare patch by a partial match."""

QDRANT_CLIENT_FLOOR = re.compile(r"^qdrant-client>=(\d+)\.(\d+)$")
"""`pyproject.toml`'s `qdrant-client>=X.Y` entry, anchored so an entry carrying an
upper bound or an extra marker is not read as a bare floor by a partial match."""

NO_LONGER_PINNED = "1.12.4"
"""The old qdrant patch. No file this plan touches may still carry it."""

FILES_WITH_NO_OLD_PIN: tuple[Path, ...] = (
    COMPOSE,
    IMAGE_SCAN_WORKFLOW,
    DEPLOY_DOC,
    SECURITY_TEST,
)
"""The four files the old pin lived in; each must drop it in the same change."""


def _qdrant_server_image() -> str:
    """The qdrant service's `image:` tag from compose, asserted to be exactly one."""
    assert COMPOSE.is_file(), "docker-compose.yml does not exist"
    parsed = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict), "docker-compose.yml is not a YAML mapping"

    services = parsed.get("services")
    assert isinstance(services, dict), "docker-compose.yml declares no services"
    qdrant = services.get("qdrant")
    assert isinstance(qdrant, dict), (
        f"compose has no qdrant service, services are {sorted(services)}"
    )

    image = qdrant.get("image")
    assert isinstance(image, str) and image, f"the qdrant service names no image: {image!r}"
    return image


def _qdrant_client_floor_minor() -> int:
    """The minor of `pyproject.toml`'s `qdrant-client>=X.Y` floor, asserted exactly one."""
    assert PYPROJECT.is_file(), "backend/pyproject.toml does not exist"
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8")).get("project")
    assert isinstance(project, dict), "pyproject.toml declares no [project] table"

    dependencies = project.get("dependencies")
    assert isinstance(dependencies, list) and dependencies, (
        "pyproject.toml declares no dependencies"
    )

    minors = [
        int(match.group(2))
        for match in (QDRANT_CLIENT_FLOOR.match(str(entry).strip()) for entry in dependencies)
        if match is not None
    ]
    assert len(minors) == 1, f"expected 1 `qdrant-client>=` entry, found {len(minors)}: {minors}"
    return minors[0]


def _server_minor(image: str) -> int:
    """The minor read off a `qdrant/qdrant:vX.Y.Z` tag."""
    match = QDRANT_IMAGE.match(image)
    assert match is not None, f"qdrant image {image!r} is not qdrant/qdrant:vX.Y.Z"
    return int(match.group(2))


def test_compose_pins_a_1_19_server() -> None:
    """Catches a compose file left on the old server tag (or any tag but v1.19.1),
    which is the mismatch this plan closes: the client resolves to 1.19.0 while the
    server stays behind."""
    assert _qdrant_server_image() == EXPECTED_SERVER_IMAGE


def test_client_floor_and_server_share_the_minor() -> None:
    """Catches the two sides drifting apart: a server bumped without raising the
    client floor, or a floor raised without bumping the server. Asserts both land on
    minor 19 exactly, not merely that they are equal, so leaving both on the old 12
    would not pass."""
    server_minor = _server_minor(_qdrant_server_image())
    floor_minor = _qdrant_client_floor_minor()

    assert server_minor == floor_minor, (
        f"compose qdrant server is minor {server_minor}, client floor is minor {floor_minor}"
    )
    assert server_minor == EXPECTED_MINOR, (
        f"both on minor {server_minor}, expected {EXPECTED_MINOR}"
    )
    assert floor_minor == EXPECTED_MINOR, (
        f"both on minor {floor_minor}, expected {EXPECTED_MINOR}"
    )


def test_no_v1_12_4_pin_remains() -> None:
    """Catches the old patch being left behind in any one of the four files it lived
    in, so compose, the image scan, the accepted-red deploy note and the security
    test's pinned-image set all move together. A single stray `1.12.4` reddens this."""
    for path in FILES_WITH_NO_OLD_PIN:
        assert path.is_file(), f"{path.relative_to(REPO_ROOT)} does not exist"
        text = path.read_text(encoding="utf-8")
        assert NO_LONGER_PINNED not in text, (
            f"{path.relative_to(REPO_ROOT)} still carries {NO_LONGER_PINNED!r}"
        )
