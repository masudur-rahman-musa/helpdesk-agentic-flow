"""Checks for policy document names and search arguments, run before any write or search.

The length caps are also the column widths, so a check and its column cannot
drift. Patterns use `fullmatch`, so a trailing newline is refused, not a second spelling.
"""

import re

from helpdesk.shared.core.errors import ErrorCode, ToolError

SLUG_LENGTH = 64
"""Column widths for `policy_documents`."""

VERSION_LENGTH = 8

TITLE_LENGTH = 200

SOURCE_PATH_LENGTH = 256

COLLECTION_NAME_PREFIX = "policy_"

COLLECTION_NAME_LENGTH = len(COLLECTION_NAME_PREFIX) + SLUG_LENGTH

MAX_QUERY_CHARS = 1000
"""The embedder quietly cuts long input instead of refusing it, so the cap is here."""

MAX_TOP_K = 50

SLUG_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
"""One spelling per document; two spellings would be two documents."""

VERSION_PATTERN = re.compile(r"v[1-9][0-9]{0,3}")
"""The corpus folder name (`v1`), not the front-matter."""


class InvalidPolicyNameError(ToolError):
    """A document name this store will not accept, refused before anything is written."""

    code = ErrorCode.VALIDATION


class InvalidSearchArgumentError(ToolError):
    """A search query or page size refused before it reaches the model or the store."""

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
    return _checked(value, cap=SLUG_LENGTH, pattern=SLUG_PATTERN, what="document slug")


def check_version(value: str) -> str:
    return _checked(value, cap=VERSION_LENGTH, pattern=VERSION_PATTERN, what="version")


def check_title(value: str) -> str:
    return _checked(value, cap=TITLE_LENGTH, pattern=None, what="title")


def check_source_path(value: str) -> str:
    return _checked(value, cap=SOURCE_PATH_LENGTH, pattern=None, what="source path")


def check_collection_name(value: str) -> str:
    """Empty is refused: Qdrant would read it as the collections index."""
    return _checked(value, cap=COLLECTION_NAME_LENGTH, pattern=None, what="collection name")


def check_query(value: str) -> str:
    """Empty is refused: it would embed as a zero vector that matches nothing."""
    if len(value) > MAX_QUERY_CHARS:
        raise InvalidSearchArgumentError(
            f"query is longer than {MAX_QUERY_CHARS} characters: {len(value)}"
        )
    if not value:
        raise InvalidSearchArgumentError("query is empty")
    return value


def check_top_k(value: int) -> int:
    if value < 1:
        raise InvalidSearchArgumentError(f"top_k is smaller than 1: {value}")
    if value > MAX_TOP_K:
        raise InvalidSearchArgumentError(f"top_k is larger than {MAX_TOP_K}: {value}")
    return value
