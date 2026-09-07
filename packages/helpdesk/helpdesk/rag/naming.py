"""The typed pre-checks for everything a policy document is named by.

Pure: `re` and the error codes, no I/O and no models. The four length
constants are also the `policy_documents` column widths — `models/policy.py`
imports them rather than repeating the numbers — so a check and the column it
guards cannot drift apart. Pinned by
`test_rag_naming.py::test_the_four_length_caps_are_exactly_the_spec_values`
and `test_policy_models.py::test_the_document_column_widths_are_the_naming_constants`.

Every check runs `re.fullmatch` where it has a pattern, so
a trailing newline, CRLF or space is a refusal rather than a second spelling
of one slug; pinned by
`test_rag_naming.py::test_a_slug_with_a_trailing_character_is_refused` and
its version twin. Checks run before any write, so an oversized or malformed
name is a typed `VALIDATION` refusal instead of a truncation error raised
half-way through a registering transaction.
"""

import re

from helpdesk.errors import ErrorCode, ToolError

SLUG_LENGTH = 64
"""The `policy_documents.slug` column width."""

VERSION_LENGTH = 8
"""The `policy_documents.version` column width.

Wider than any version `VERSION_PATTERN` admits (`v9999`, five characters).
The column is sized to the design rather than to the pattern, so decision G can
be widened later without a migration; the pattern is the narrower gate, and
`test_rag_naming.py::test_an_eight_character_version_is_refused_by_the_pattern`
pins that it, not the cap, is what refuses a full-width candidate.
"""

TITLE_LENGTH = 200
"""The `policy_documents.title` column width."""

SOURCE_PATH_LENGTH = 256
"""The `policy_documents.source_path` column width."""

SLUG_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
"""A lowercase-hyphen slug: no uppercase, no underscore, no leading, trailing
or doubled hyphen. `(tenant, slug)` is the key `is_current` is flipped over,
so two spellings of one document would be two documents."""

VERSION_PATTERN = re.compile(r"v[1-9][0-9]{0,3}")
"""Decision G: the corpus directory name (`v1`), not the front-matter."""


class InvalidPolicyNameError(ToolError):
    """A slug, version, title or source path this store will not accept.

    Raised before any effect runs, so a malformed name never half-lands: the
    alternative is a document row written under a name the ingest walker
    cannot produce again, which presents as a version that re-ingests for
    ever because its sha256 is never found.
    """

    code = ErrorCode.VALIDATION


def _checked(value: str, *, cap: int, pattern: re.Pattern[str] | None, what: str) -> str:
    if len(value) > cap:
        raise InvalidPolicyNameError(f"{what} is longer than {cap} characters: {len(value)}")
    if not value:
        raise InvalidPolicyNameError(f"{what} is empty")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise InvalidPolicyNameError(f"{what} does not match {pattern.pattern!r}")
    return value


def check_slug(value: str) -> str:
    """The accepted document slug, or `InvalidPolicyNameError`."""
    return _checked(value, cap=SLUG_LENGTH, pattern=SLUG_PATTERN, what="document slug")


def check_version(value: str) -> str:
    """The accepted version directory name, or `InvalidPolicyNameError`."""
    return _checked(value, cap=VERSION_LENGTH, pattern=VERSION_PATTERN, what="version")


def check_title(value: str) -> str:
    """The accepted document title, or `InvalidPolicyNameError`.

    No pattern: a title is prose read off the document's first heading, and
    the only thing this store promises about it is that it fits the column.
    """
    return _checked(value, cap=TITLE_LENGTH, pattern=None, what="title")


def check_source_path(value: str) -> str:
    """The accepted source path, or `InvalidPolicyNameError`."""
    return _checked(value, cap=SOURCE_PATH_LENGTH, pattern=None, what="source path")
