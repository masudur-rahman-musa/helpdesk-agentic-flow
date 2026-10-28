"""Build one service image, with both arguments checked before docker starts.

Why a script rather than four lines of Makefile: both values are interpolated
into a `docker build` command line, so an unchecked one is an argument the
build tool reads as something else. A tag carrying a `/` turns `local` into a
registry path and a later push goes somewhere nobody chose; a service name
outside the accepted set reaches `--build-arg SERVICE=` and fails minutes later
as an unreadable build error instead of one refusal naming the four values that
would have worked.

the typed pre-check runs before any effect, and the pattern is applied with
`re.fullmatch` so a trailing newline
cannot ride along behind an otherwise valid tag. Both are pinned by
`backend/tests/shared/test_build_images_args.py`; the ordering specifically
by `test_the_checks_run_before_any_docker_process_starts`, which patches this
module's `subprocess.run` and asserts nothing was spawned.

The three Python services each build from their own `backend/Dockerfile.<service>`
with `backend/` as the context, copying only that app plus `shared/`; there is
no longer one root Dockerfile with a `SERVICE` build arg.

`web` is accepted here and built from a different file: it has no uv install to
share with the three Python services, so `docker build web` is its own context
with its own Dockerfile.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
"""The repo root, so this script works from any working directory."""

BACKEND_ROOT = REPO_ROOT / "backend"
"""The Python project root: the build context for the three service images and
where their `Dockerfile.<service>` files live."""

ACCEPTED_SERVICES: frozenset[str] = frozenset({"agent-api", "mcp-server", "scheduler", "web"})
"""The only four images this repository knows how to build (criterion 17).

An EXACT set rather than a floor: a fifth name would be a build target no
Dockerfile has, and a missing one refuses a build the Makefile still asks for.
Pinned by `test_the_accepted_service_names_are_exactly_the_four_planned`."""

WEB_SERVICE = "web"
"""The one accepted name built from `web/Dockerfile` rather than a
`backend/Dockerfile.<service>`."""

TAG_MAX_LENGTH = 128
"""The inclusive upper bound on a tag, pinned as a value and
asserted from both sides by the two boundary cases in the test module."""

TAG_PATTERN: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
"""The characters a tag may carry, applied with `fullmatch` and never `match`.

The literal 128 is the same number as `TAG_MAX_LENGTH`, which the test module
pins together so the two cannot drift. The class excludes `/` and `:` because
either turns a tag into something docker reads as a repository or a second tag,
and excludes whitespace because `re.match`'s `$` would stop before a trailing
newline and let it through."""

IMAGE_PREFIX = "helpdesk-"
"""Every image is named `helpdesk-<service>:<tag>`, the names docker-compose.yml
builds and .github/workflows/image-scan.yml scans."""


class BuildArgumentError(ValueError):
    """A service name or a tag that must never reach a `docker build`.

    A `ValueError` subclass rather than a bare one so a caller can catch this
    without catching every other conversion failure in the process."""


def validate_build_arguments(service: str, tag: str) -> None:
    """Refuse an unknown service or a malformed tag, before any effect runs.

    Raises `BuildArgumentError`. The refusal for an unknown service names all
    four accepted values, because the message is the whole user interface at
    the moment someone has typed `agent_api` for `agent-api`.
    """
    if service not in ACCEPTED_SERVICES:
        accepted = ", ".join(sorted(ACCEPTED_SERVICES))
        raise BuildArgumentError(f"unknown service {service!r}: expected one of {accepted}")

    if TAG_PATTERN.fullmatch(tag) is None:
        raise BuildArgumentError(
            f"invalid tag {tag!r}: 1 to {TAG_MAX_LENGTH} characters from A-Z a-z 0-9 and _ . - only"
        )


def _build_command(service: str, tag: str) -> list[str]:
    """The argument vector for one image, never a shell string.

    A list means the values are passed to `docker` as separate argv entries, so
    even a tag that got past the pattern could not be read as a second flag.
    """
    image = f"{IMAGE_PREFIX}{service}:{tag}"
    if service == WEB_SERVICE:
        return ["docker", "build", "-t", image, str(REPO_ROOT / "web")]
    return [
        "docker",
        "build",
        "-f",
        str(BACKEND_ROOT / f"Dockerfile.{service}"),
        "-t",
        image,
        str(BACKEND_ROOT),
    ]


def build_image(service: str, tag: str) -> None:
    """Validate, then build. The order is the contract, not an implementation
    detail: a refused tag must not have already tagged an image and left it on
    the machine. `subprocess.run` is reached through the module so the test can
    replace it and assert nothing was spawned."""
    validate_build_arguments(service, tag)
    subprocess.run(_build_command(service, tag), check=True)


def main(argv: list[str] | None = None) -> int:
    """`uv run python infra/scripts/build_images.py --service agent-api --tag local`.

    No service argument means all four, which is what `make images` runs.
    """
    parser = argparse.ArgumentParser(description="Build this repository's service images.")
    parser.add_argument(
        "--service",
        action="append",
        help="a service to build; repeatable, defaults to all four",
    )
    parser.add_argument("--tag", default="local", help="the image tag (default: local)")
    arguments = parser.parse_args(argv)

    services = arguments.service or sorted(ACCEPTED_SERVICES)
    try:
        # Every argument is checked before the FIRST build, so a typo in the
        # fourth name does not leave three images built and one refusal.
        for service in services:
            validate_build_arguments(service, arguments.tag)
        for service in services:
            build_image(service, arguments.tag)
    except BuildArgumentError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as error:
        print(f"error: docker build failed with status {error.returncode}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
