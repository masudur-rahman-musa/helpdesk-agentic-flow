"""The argument pre-checks of `scripts/build_images.py`.

What is pinned: the exact set of four accepted `SERVICE` values, the tag pattern
and both of its length boundaries (128 accepted, 129 and the empty string
refused), the character class that keeps a tag from becoming a registry path,
and the ordering guarantee that both checks run before any `docker` process is
spawned.

Why the checks are worth a suite of their own: both values are interpolated into
a `docker build` command line, so an unchecked tag is an argument the build
tool reads as something else (a `/` turns `local` into a registry path and the
push target changes silently). a project rule wants the typed pre-check before
any effect runs, and a project rule wants the pattern applied with
`re.fullmatch` so a trailing newline cannot ride along behind a valid tag.

Pure: no Postgres, Redis, Qdrant or Docker daemon. The only seam is
`subprocess.run`, monkeypatched in the one case that asserts nothing is spawned.

The cases are red at HEAD because `scripts/build_images.py` does not exist yet,
so the import of `ACCEPTED_SERVICES`, `TAG_PATTERN`, `TAG_MAX_LENGTH`,
`BuildArgumentError`, `validate_build_arguments` and `build_image` fails at
collection.
"""

import pytest

from scripts.build_images import (
    ACCEPTED_SERVICES,
    TAG_MAX_LENGTH,
    TAG_PATTERN,
    BuildArgumentError,
    build_image,
    validate_build_arguments,
)

EXPECTED_SERVICES: frozenset[str] = frozenset({"agent-api", "mcp-server", "scheduler", "web"})
"""The four literals criterion 17 names, asserted as an EXACT set: an extra
accepted name is a build target nothing else in this repo knows how to make."""

EXPECTED_TAG_MAX_LENGTH = 128
"""The inclusive upper bound of the tag pattern, pinned as a value per standing
rule 2 and asserted from both sides by the two boundary cases below."""

VALID_TAG = "local"
"""The tag `make images` uses, held constant wherever a case varies the other
argument, so a failure names one thing."""

VALID_SERVICE = "agent-api"
"""The same, in the other direction."""

REFUSED_TAG_CHARACTERS: tuple[tuple[str, str], ...] = (
    ("slash", "helpdesk/local"),
    ("space", "local build"),
    ("trailing_newline", "local\n"),
    ("crlf", "local\r\n"),
    ("colon", "local:1"),
)
"""The rejection table a project rule asks for. The slash is the one that
matters most: `docker build -t helpdesk/local` names a different repository and
a later push goes somewhere nobody chose. The newline and CRLF rows are what
separate `re.fullmatch` from a `re.match` whose `$` is happy to stop before a
final newline."""


def test_the_accepted_service_names_are_exactly_the_four_planned() -> None:
    """Catches the accepted set drifting to whatever the Dockerfile happened to
    support, in either direction: a fifth name builds a target no Dockerfile
    has, and a missing one refuses a build the Makefile still asks for."""
    assert set(ACCEPTED_SERVICES) == set(EXPECTED_SERVICES)
    assert TAG_MAX_LENGTH == EXPECTED_TAG_MAX_LENGTH


def test_an_unknown_service_name_is_refused() -> None:
    """Catches a script that passes any string through to `docker build
    --build-arg SERVICE=...`, where the failure arrives minutes later as an
    unreadable build error instead of one typed refusal naming the four values
    that would have worked."""
    with pytest.raises(BuildArgumentError) as caught:
        validate_build_arguments("agent_api", VALID_TAG)

    message = str(caught.value)
    for service in sorted(EXPECTED_SERVICES):
        assert service in message, f"the refusal does not name {service}"


@pytest.mark.parametrize("service", sorted(EXPECTED_SERVICES))
def test_each_accepted_service_name_is_accepted(service: str) -> None:
    """Catches a guard written against a narrower set than the one it advertises
    (the three Python services, with `web` refused), which would make
    `make images` fail on the one image built from a different Dockerfile."""
    validate_build_arguments(service, VALID_TAG)


def test_a_tag_of_one_hundred_and_twenty_eight_characters_is_accepted() -> None:
    """Catches an off-by-one on the inclusive upper bound: a `{1,127}` pattern,
    or a `len(tag) < 128` comparison, refuses the longest tag the contract
    promises and the refusal reads as a bug in the caller."""
    tag = "t" * EXPECTED_TAG_MAX_LENGTH

    assert TAG_PATTERN.fullmatch(tag) is not None
    validate_build_arguments(VALID_SERVICE, tag)


def test_a_tag_of_one_hundred_and_twenty_nine_characters_is_refused() -> None:
    """Catches an unbounded pattern (`+` rather than `{1,128}`), the first
    length that must fail; a cap that is documented and not enforced is not a
    cap."""
    tag = "t" * (EXPECTED_TAG_MAX_LENGTH + 1)

    assert TAG_PATTERN.fullmatch(tag) is None
    with pytest.raises(BuildArgumentError):
        validate_build_arguments(VALID_SERVICE, tag)


def test_an_empty_tag_is_refused() -> None:
    """Catches a `{0,128}` pattern or a missing emptiness check, the inclusive
    lower boundary: `docker build -t helpdesk-agent-api:` is not the image
    anyone asked for."""
    assert TAG_PATTERN.fullmatch("") is None
    with pytest.raises(BuildArgumentError):
        validate_build_arguments(VALID_SERVICE, "")


@pytest.mark.parametrize(
    "tag",
    [tag for _, tag in REFUSED_TAG_CHARACTERS],
    ids=[name for name, _ in REFUSED_TAG_CHARACTERS],
)
def test_a_tag_carrying_a_slash_is_refused(tag: str) -> None:
    """Catches a pattern applied with `re.match` or a Pydantic-style `pattern=`,
    both of which match a PREFIX: `helpdesk/local` passes, and the tag has
    quietly become a registry path. The newline and CRLF rows are the same
    defect seen from the end of the string, where `$` stops before a final
    newline and `re.fullmatch` does not."""
    assert TAG_PATTERN.fullmatch(tag) is None
    with pytest.raises(BuildArgumentError):
        validate_build_arguments(VALID_SERVICE, tag)


def test_the_checks_run_before_any_docker_process_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Catches the checks living after the build call, or only inside an
    argument parser the build entry point does not go through: a refused tag
    would then have already spawned `docker build`, tagged an image and left it
    on the machine before the refusal was read. The spy replaces
    `scripts.build_images.subprocess.run`, the module's only process seam, so
    the case fails for the ordering and not for the refusal, which the cases
    above already pin."""
    calls: list[object] = []

    def _spy(*args: object, **kwargs: object) -> object:
        calls.append(args)
        raise AssertionError(f"a docker process was started with {args!r}")

    monkeypatch.setattr("scripts.build_images.subprocess.run", _spy)

    with pytest.raises(BuildArgumentError):
        build_image("agent_api", VALID_TAG)
    with pytest.raises(BuildArgumentError):
        build_image(VALID_SERVICE, "helpdesk/local")

    assert calls == []
