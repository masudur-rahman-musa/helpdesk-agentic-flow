"""The typed pre-checks for everything the policy corpus is addressed by.

Two groups. The document names — slug, version, title, source path,
collection name — are what a corpus file is registered and stored under. The
two search arguments below them, `check_query` and `check_top_k`,
are what a RETRIEVAL supplies; they live here because both groups are the
same thing, a client-supplied value checked before any effect runs, and
splitting them would give the package two places that bound a string.

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

COLLECTION_NAME_PREFIX = "policy_"
"""What every per-tenant Qdrant collection is named `policy_{tenant_slug}` by."""

COLLECTION_NAME_LENGTH = len(COLLECTION_NAME_PREFIX) + SLUG_LENGTH
"""The cap on a Qdrant collection name.

Derived, not chosen: a collection name is the prefix plus a tenant slug, so a
smaller cap would refuse the longest tenant `check_slug` accepts and a larger
one would admit a name no valid tenant can produce. Unlike the four widths
above it guards no column — Qdrant takes the name as a URL path segment and
sizes it itself — which is exactly why a project rule wants a pre-check here.
`check_slug` is the narrower of the two gates. Pinned by
`test_rag_naming.py::test_the_collection_name_cap_is_exactly_the_prefix_plus_the_slug_cap`
and its accepted-at-71 / refused-at-72 pair.
"""

MAX_QUERY_CHARS = 1000
"""The cap on a retrieval query.

Bounds a string that reaches TWO services with no backstop of their own: the
dense model truncates its input at 512 tokens rather than refusing it, so an
unbounded query is silently embedded from its first half, and Qdrant sizes
nothing about a query vector's origin. a project rule; the boundary pair is
pinned by
`test_rag_retrieval.py::test_check_query_accepts_a_query_of_exactly_the_cap`
and `::test_check_query_refuses_an_empty_or_oversized_query`.
"""

MAX_TOP_K = 50
"""The cap on a retrieval page size.

`top_k` becomes a Qdrant `limit` and, above it, the number of chunks an
answer's prompt is built from. Pinned exactly by
`test_rag_retrieval.py::test_the_query_and_page_caps_are_exactly_the_planned_values`,
with the inclusive boundary at 1 and 50 by
`::test_check_top_k_accepts_the_inclusive_boundary`.
"""

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


class InvalidSearchArgumentError(ToolError):
    """A query or a page size this retrieval will not accept.

    Separate from `InvalidPolicyNameError` because nothing here names a
    document: these are the two values a SEARCH supplies, checked before the
    query is embedded or either arm is issued, so an oversized value reaches
    neither the model nor the store. Pinned by
    `test_rag_retrieval.py::test_a_refused_argument_reaches_neither_the_store_nor_the_embedder`,
    which asserts zero calls on both stand-ins rather than merely that this
    was raised.
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


def check_collection_name(value: str) -> str:
    """The accepted Qdrant collection name, or `InvalidPolicyNameError`.

    No pattern: the name is built from a slug `check_slug` has already
    accepted, so the only thing left to bound is the length of the whole. The
    empty string is refused here rather than left to the store, where it
    addresses the collections index instead of a collection; pinned by
    `test_rag_naming.py::test_an_empty_collection_name_is_refused`.
    """
    return _checked(value, cap=COLLECTION_NAME_LENGTH, pattern=None, what="collection name")


def check_query(value: str) -> str:
    """The accepted retrieval query, or `InvalidSearchArgumentError`.

    No pattern: a question is prose in whatever language the user asks in,
    and the only things this store promises about it are that it is not empty
    and that it fits `MAX_QUERY_CHARS`. The empty string is refused here
    rather than left to the embedder, where it produces a zero vector whose
    cosine against every chunk is 0.0 (`embedding.cosine`) — a page the
    refusal threshold would then read as meaningless similarity rather than
    as no query at all.
    """
    if len(value) > MAX_QUERY_CHARS:
        raise InvalidSearchArgumentError(
            f"query is longer than {MAX_QUERY_CHARS} characters: {len(value)}"
        )
    if not value:
        raise InvalidSearchArgumentError("query is empty")
    return value


def check_top_k(value: int) -> int:
    """The accepted page size, or `InvalidSearchArgumentError`.

    Inclusive at both ends: 1 is the smallest useful page and `MAX_TOP_K` the
    largest this store may be asked for. Zero and negative values are refused
    here rather than passed on as a Qdrant `limit`, which interprets them
    itself.
    """
    if value < 1:
        raise InvalidSearchArgumentError(f"top_k is smaller than 1: {value}")
    if value > MAX_TOP_K:
        raise InvalidSearchArgumentError(f"top_k is larger than {MAX_TOP_K}: {value}")
    return value
