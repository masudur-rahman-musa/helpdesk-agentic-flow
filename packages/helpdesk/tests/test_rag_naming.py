"""An earlier change §6: the four typed pre-checks in `helpdesk/rag/naming.py`.

Expected implementation surface (the implementer must provide exactly this):

``helpdesk.rag.naming``
    ``SLUG_LENGTH = 64`` — the `policy_documents.slug` column width.
    ``VERSION_LENGTH = 8`` — the `policy_documents.version` column width.
    ``TITLE_LENGTH = 200`` — the `policy_documents.title` column width.
    ``SOURCE_PATH_LENGTH = 256`` — the `policy_documents.source_path` width.
    ``SLUG_PATTERN: re.Pattern[str]`` — a lowercase-hyphen slug.
    ``VERSION_PATTERN: re.Pattern[str]`` — ``r"v[1-9][0-9]{0,3}"`` (decision G).
    ``InvalidPolicyNameError(ToolError)`` — ``code = ErrorCode.VALIDATION``.
    ``check_slug(value: str) -> str``
    ``check_version(value: str) -> str``
    ``check_title(value: str) -> str``
    ``check_source_path(value: str) -> str``
    Each returns the value it accepted (so a caller can use the result) and
    raises ``InvalidPolicyNameError`` otherwise. Every check runs
    ``re.fullmatch`` where it has a pattern and compares the
    length against the SAME constant the column width is declared with, so
    the check cannot drift from the column.

Length caps and the version pattern are pinned to their exact values here: a band would admit a wrong number, and a docstring is not
a record.

The version cap is deliberately NOT boundary-tested as "accepted at 8":
`VERSION_PATTERN` admits at most five characters (`v9999`), so no
8-character version can ever be accepted and an "accepted at 8" row would be
unsatisfiable by the shape this plan fixes. Pinned
instead: the constant's exact value, the longest pattern-valid version being
accepted, and an 8-character candidate being refused — by the pattern rather
than by the cap, which that case's docstring says out loud.

Pure: no DB, no fixtures, no I/O.
"""

from collections.abc import Callable

import pytest
from helpdesk.errors import ErrorCode
from helpdesk.rag.naming import (
    SLUG_LENGTH,
    SLUG_PATTERN,
    SOURCE_PATH_LENGTH,
    TITLE_LENGTH,
    VERSION_LENGTH,
    VERSION_PATTERN,
    InvalidPolicyNameError,
    check_slug,
    check_source_path,
    check_title,
    check_version,
)

Check = Callable[[str], str]


def _slug_of_length(length: int) -> str:
    """A lowercase-hyphen slug of exactly `length` characters."""
    value = ("access-control-policy-" * (length // 22 + 1))[:length]
    return value.rstrip("-").ljust(length, "x")


def _title_of_length(length: int) -> str:
    return ("Fake Policy Title " * (length // 18 + 1))[:length]


def _path_of_length(length: int) -> str:
    return ("policies/nordlicht/v1/" + "a" * length)[:length]


def test_the_four_length_caps_are_exactly_the_spec_values() -> None:
    """Catches an implementation that sizes a check or a column to any number
    other than the design's 64/8/200/256, including one that widens the check
    without widening the column it guards."""
    assert SLUG_LENGTH == 64
    assert VERSION_LENGTH == 8
    assert TITLE_LENGTH == 200
    assert SOURCE_PATH_LENGTH == 256


def test_the_version_pattern_is_exactly_decision_g() -> None:
    """Catches a version pattern that admits `v0`, `v01` or a five-digit
    version — any pattern other than decision G's `v[1-9][0-9]{0,3}`, which
    is what the corpus directory names and the Qdrant payload filter agree
    on."""
    assert VERSION_PATTERN.pattern == r"v[1-9][0-9]{0,3}"


def test_the_slug_pattern_admits_only_lowercase_hyphen_slugs() -> None:
    """Catches a slug pattern that accepts uppercase, underscores, spaces or
    a leading/trailing hyphen — the shapes that would make two spellings of
    one document slug both storable and the (tenant, slug) key ambiguous."""
    assert SLUG_PATTERN.fullmatch("access-control-policy") is not None
    for rejected in ("Access-Control", "access_control", "access control", "-access", "access-"):
        assert SLUG_PATTERN.fullmatch(rejected) is None, rejected


# --- inclusive boundaries -----------------------------------------------------


@pytest.mark.parametrize(
    ("check", "value", "cap"),
    [
        (check_slug, _slug_of_length(64), SLUG_LENGTH),
        (check_title, _title_of_length(200), TITLE_LENGTH),
        (check_source_path, _path_of_length(256), SOURCE_PATH_LENGTH),
    ],
    ids=["slug-64", "title-200", "source_path-256"],
)
def test_a_value_at_exactly_the_cap_is_accepted(check: Check, value: str, cap: int) -> None:
    """Catches an exclusive comparison (`len(value) < CAP` instead of `<=`),
    which would refuse the longest value the column can actually store."""
    assert len(value) == cap
    assert check(value) == value


@pytest.mark.parametrize(
    ("check", "value", "cap"),
    [
        (check_slug, _slug_of_length(65), SLUG_LENGTH),
        (check_title, _title_of_length(201), TITLE_LENGTH),
        (check_source_path, _path_of_length(257), SOURCE_PATH_LENGTH),
    ],
    ids=["slug-65", "title-201", "source_path-257"],
)
def test_a_value_one_over_the_cap_is_refused(check: Check, value: str, cap: int) -> None:
    """Catches a missing length check: without it the oversized value reaches
    a sized column and fails as an asyncpg truncation error after the write
    has started, instead of as a typed VALIDATION refusal before it."""
    assert len(value) == cap + 1
    with pytest.raises(InvalidPolicyNameError) as excinfo:
        check(value)
    assert excinfo.value.code is ErrorCode.VALIDATION


def test_the_longest_pattern_valid_version_is_accepted() -> None:
    """Catches a version check that caps the digits below decision G's three
    optional ones, refusing `v9999` although both the pattern and the column
    hold it."""
    assert len("v9999") <= VERSION_LENGTH
    assert check_version("v9999") == "v9999"


def test_an_eight_character_version_is_refused_by_the_pattern() -> None:
    """Catches a version check that only measures length: `v1234567` is
    exactly VERSION_LENGTH characters, so the cap cannot refuse it and only
    the `vN` pattern can. Removing the pattern check is the single change
    that flips this case."""
    candidate = "v1234567"
    assert len(candidate) == VERSION_LENGTH
    with pytest.raises(InvalidPolicyNameError) as excinfo:
        check_version(candidate)
    assert excinfo.value.code is ErrorCode.VALIDATION


def test_a_version_over_the_cap_is_refused() -> None:
    """Catches a version check with no length cap at all, where a 9-character
    candidate would reach a `varchar(8)` column."""
    candidate = "v" + "1" * 8
    assert len(candidate) == VERSION_LENGTH + 1
    with pytest.raises(InvalidPolicyNameError) as excinfo:
        check_version(candidate)
    assert excinfo.value.code is ErrorCode.VALIDATION


# --- fullmatch rejections -----------------------------------

_TRAILERS: tuple[tuple[str, str], ...] = (
    ("newline", "\n"),
    ("crlf", "\r\n"),
    ("space", " "),
)


@pytest.mark.parametrize(("name", "trailer"), _TRAILERS, ids=[name for name, _ in _TRAILERS])
def test_a_slug_with_a_trailing_character_is_refused(name: str, trailer: str) -> None:
    """Catches a slug check using `re.match` or `re.search` (or a Pydantic
    `Field(pattern=...)`), each of which matches a prefix and so accepts
    `access-control-policy\\n` — two spellings of one document slug, one of
    which carries a control character into a Qdrant payload key."""
    with pytest.raises(InvalidPolicyNameError) as excinfo:
        check_slug("access-control-policy" + trailer)
    assert excinfo.value.code is ErrorCode.VALIDATION


@pytest.mark.parametrize(("name", "trailer"), _TRAILERS, ids=[name for name, _ in _TRAILERS])
def test_a_version_with_a_trailing_character_is_refused(name: str, trailer: str) -> None:
    """Catches a version check using `re.match`/`re.search`/`pattern=`, which
    would accept `v1\\n` as a version and let one corpus directory register
    under two version strings the payload filter cannot match."""
    with pytest.raises(InvalidPolicyNameError) as excinfo:
        check_version("v1" + trailer)
    assert excinfo.value.code is ErrorCode.VALIDATION


def test_an_empty_slug_and_an_empty_version_are_refused() -> None:
    """Catches a check that only measures an upper bound, letting the empty
    string through as a document slug or a version."""
    for check in (check_slug, check_version):
        with pytest.raises(InvalidPolicyNameError) as excinfo:
            check("")
        assert excinfo.value.code is ErrorCode.VALIDATION
