"""The JSON log formatter and the redaction under it.

Written before the implementation exists. Surface under test:

``helpdesk.obs.logging``
    ``FORBIDDEN_KEYS: frozenset[str]``
    ``REDACTED``, ``TRUNCATED``, ``ELISION_MARKER``: ``str``
    ``MAX_REDACT_DEPTH``, ``MAX_LOG_VALUE_CHARS``: ``int``
    ``def redact(value: object, depth: int = 0) -> object`` — pure
    ``class JsonFormatter(logging.Formatter)`` — one line of JSON per record,
    carrying ``timestamp``, ``level``, ``logger``, ``message``, the bound
    telemetry context and the record's ``extra``, every value through
    ``redact``
    ``def configure_logging(level: int = logging.INFO) -> None`` — idempotent

Every assertion that names a constant asserts its VALUE, so
widening a bound without widening the criterion fails here rather than
shipping silently.

No backing service: these are a pure function and a formatter, driven through
a handler this module attaches to a throwaway logger.
"""

import io
import json
import logging
import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from helpdesk.shared.core.observability.logging import (
    ELISION_MARKER,
    FORBIDDEN_KEYS,
    MAX_LOG_VALUE_CHARS,
    MAX_REDACT_DEPTH,
    REDACTED,
    TRUNCATED,
    JsonFormatter,
    configure_logging,
    redact,
)

# An obviously fake value planted under every forbidden key: it must never
# reach the line, so its presence anywhere in the raw text is the failure.
SECRET = "must-never-appear-in-a-log-line"


def _emit(message: str, extra: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    """Format one record through `JsonFormatter`; return the raw text and its parse.

    A throwaway logger with `propagate = False`, so the record cannot reach
    the root handlers pytest and `configure_logging` install and the captured
    text is exactly this one record.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger(f"test.obs.logging.{uuid.uuid4().hex}")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        logger.info(message, extra=extra or {})
    finally:
        logger.removeHandler(handler)
        handler.close()
    raw = stream.getvalue()
    parsed: dict[str, Any] = json.loads(raw)
    return raw, parsed


def _at(document: Any, path: tuple[Any, ...]) -> Any:
    """The value at `path` inside a parsed line (dict keys and list indices)."""
    current: Any = document
    for step in path:
        current = current[step]
    return current


def _nested(levels: int, leaf: str) -> Any:
    """`levels` dicts wrapped around `leaf`, each under the key `k`.

    Counting convention: `_nested(1, "x") == {"k": "x"}`, so `leaf` sits at
    nesting level `levels` and is reached by walking `levels` keys.
    """
    current: Any = leaf
    for _ in range(levels):
        current = {"k": current}
    return current


@pytest.fixture
def _root_handlers() -> Iterator[None]:
    """Restore the root logger exactly as it was; `configure_logging` edits it."""
    root = logging.getLogger()
    saved = list(root.handlers)
    level = root.level
    try:
        yield
    finally:
        root.handlers[:] = saved
        root.setLevel(level)


def test_a_record_renders_as_one_line_of_valid_json() -> None:
    """Catches a formatter that pretty-prints, or that lets a multi-line
    message split one record across two log lines — either of which breaks
    every line-oriented log reader downstream and makes half a record parse
    as a whole one."""
    raw, line = _emit("first line\nsecond line")

    assert raw.count("\n") == 1
    assert raw.endswith("\n")
    assert line["message"] == "first line\nsecond line"
    assert line["level"] == "INFO"
    assert "timestamp" in line
    assert "logger" in line


FORBIDDEN_POSITIONS: tuple[tuple[str, str, tuple[Any, ...]], ...] = (
    # label, the extra key the value is planted under, the path to it in the line
    ("top-level-extra", "", ()),
    ("nested-dict", "payload", ("payload",)),
    ("dict-inside-a-list", "items", ("items", 0)),
)

REDACTION_CASES = [
    (f"{position}-{key}", position, container, path, key)
    for position, container, path in FORBIDDEN_POSITIONS
    # Sorted so the ids are stable; the set itself is asserted in the test.
    for key in sorted(FORBIDDEN_KEYS)
]


@pytest.mark.parametrize(
    ("label", "position", "container", "path", "key"),
    REDACTION_CASES,
    ids=[case[0] for case in REDACTION_CASES],
)
def test_every_forbidden_key_is_redacted_at_every_position(
    label: str, position: str, container: str, path: tuple[Any, ...], key: str
) -> None:
    """Catches a formatter that only redacts top-level keys, so an identity
    tucked one dict or one list element deeper — the shape every payload,
    claims map and tool argument dump actually has — reaches the log."""
    assert REDACTED == "[redacted]"
    assert (
        frozenset({"email", "display_name", "password", "user_id", "actor_ref"}) == FORBIDDEN_KEYS
    )

    if container == "":
        extra: dict[str, Any] = {key: SECRET}
    elif container == "payload":
        extra = {"payload": {key: SECRET}}
    else:
        extra = {"items": [{key: SECRET}]}

    raw, line = _emit("a record carrying an identity", extra)

    # The key stays, its value does not.
    assert _at(line, path)[key] == REDACTED, label
    assert SECRET not in raw, label


def test_a_value_of_exactly_the_cap_is_kept_and_one_more_is_cut() -> None:
    """Catches an off-by-one that truncates a value that fits: the boundary is
    inclusive at MAX_LOG_VALUE_CHARS, and one character past it is cut to
    exactly the cap with the elision marker appended."""
    assert MAX_LOG_VALUE_CHARS == 2000
    assert ELISION_MARKER == "…"

    fits = "a" * MAX_LOG_VALUE_CHARS
    one_over = "a" * (MAX_LOG_VALUE_CHARS + 1)

    assert redact(fits) == fits
    assert redact(one_over) == fits + ELISION_MARKER

    _, line = _emit("a long value", {"note": one_over})
    assert line["note"] == fits + ELISION_MARKER


def test_nesting_of_exactly_the_cap_is_rendered_and_one_deeper_is_truncated() -> None:
    """Catches a recursion cap that discards a level that was within bounds.

    Counting convention: `redact` is called with `depth=0` for the top-level
    value, so a leaf wrapped in MAX_REDACT_DEPTH dicts is reached by walking
    that many keys and renders as itself; one dict deeper renders TRUNCATED.
    """
    assert MAX_REDACT_DEPTH == 6
    assert TRUNCATED == "[truncated]"

    at_the_cap = redact(_nested(MAX_REDACT_DEPTH, "leaf"))
    one_deeper = redact(_nested(MAX_REDACT_DEPTH + 1, "leaf"))

    assert _at(at_the_cap, ("k",) * MAX_REDACT_DEPTH) == "leaf"
    assert _at(one_deeper, ("k",) * (MAX_REDACT_DEPTH + 1)) == TRUNCATED


def test_a_self_referential_structure_emits_one_valid_line() -> None:
    """Catches unbounded recursion, which is a RecursionError raised inside a
    log call — an exception thrown by the machinery that exists to report
    exceptions, taking the caller down with it."""
    loop: dict[str, Any] = {"name": "cycle"}
    loop["self"] = loop

    raw, line = _emit("a cyclic payload", {"payload": loop})

    assert raw.count("\n") == 1
    assert line["payload"]["name"] == "cycle"


class _NoJsonForm:
    """A value with no JSON form, whose repr is recognisable in the line."""

    def __repr__(self) -> str:
        return "<NoJsonForm object, tests only>"


def test_a_non_serialisable_value_falls_back_to_its_repr() -> None:
    """Catches a formatter that raises TypeError on an object json cannot
    encode and loses the whole line, which is exactly what happens the first
    time a caller logs a model, a UUID or an exception in `extra`."""
    value = _NoJsonForm()

    assert redact(value) == repr(value)

    raw, line = _emit("an unserialisable value", {"payload": value})

    assert raw.count("\n") == 1
    assert line["payload"] == repr(value)


def test_configure_logging_twice_leaves_one_handler(
    _root_handlers: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Catches an `addHandler` with no idempotence, which doubles every line
    in any process that configures twice — both services are imported by one
    test session, and a duplicated audit-relevant line is a duplicated fact.

    The count is of handlers carrying `JsonFormatter`, not of every root
    handler: pytest's own logging plugin attaches handlers to the root logger
    for the duration of a test, so an absolute count would assert on pytest
    rather than on the implementation.
    """
    configure_logging()
    configure_logging()

    root = logging.getLogger()
    installed = [h for h in root.handlers if isinstance(h.formatter, JsonFormatter)]

    assert len(installed) == 1
    assert isinstance(installed[0], logging.StreamHandler)

    marker = "one-line-per-record-marker"
    logging.getLogger("test.obs.logging.idempotence").info(marker)

    captured = capsys.readouterr()
    assert (captured.err + captured.out).count(marker) == 1
