"""One line of JSON per record, with identity redacted on the way out.

Redaction lives in the FORMATTER, not in a `logging.Filter` (an earlier change
decision B). A filter is attached per handler, so a handler added by a later
plan would log unredacted and no test would fail; a formatter is what renders
the line, and the pure `redact` under it is table-testable over plain dicts
with no logging machinery in the way.

A forbidden key renders `REDACTED` in place rather than being dropped
(decision C): a missing key is indistinguishable from a field that was never
set, which is exactly the question an operator asks when a line looks thin.

Nothing here raises into the caller. A value with no JSON form renders its
`repr`, a structure deeper than `MAX_REDACT_DEPTH` renders `TRUNCATED` (which
is also what terminates a self-referential one), and a `json.dumps` that fails
anyway still emits a parseable line. An exception thrown by the machinery that
reports exceptions takes the caller down with it, so the unhappy paths are
pinned: `test_obs_logging.py::test_a_self_referential_structure_emits_one_valid_line`,
`::test_a_non_serialisable_value_falls_back_to_its_repr`.
"""

import json
import logging
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from helpdesk.obs.context import current_context

FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {"email", "display_name", "password", "user_id", "actor_ref"}
)
"""The field names the design keeps out of telemetry, at any position.

This set is the inverse of an allowlist, and is exactly as good as the names
in it: a future field carrying a person under a sixth spelling passes through
with no test failing. It lives here alone so a later
plan widens it in one place. Pinned by
`test_obs_logging.py::test_every_forbidden_key_is_redacted_at_every_position`,
which asserts the set itself and every member at three depths."""

REDACTED = "[redacted]"
"""What a forbidden key's value renders as (decision C)."""

TRUNCATED = "[truncated]"
"""What a value deeper than `MAX_REDACT_DEPTH` renders as."""

ELISION_MARKER = "…"
"""Appended to a string cut at `MAX_LOG_VALUE_CHARS`, so a reader can tell a
cut value from one that happened to be exactly that long."""

MAX_REDACT_DEPTH = 6
"""How deep the walk goes before a value renders `TRUNCATED`.

Depth 0 is the value handed to `redact`, so a leaf under six mappings still
renders and one deeper does not. This is also what makes a self-referential
structure terminate. Pinned inclusively from both sides by
`test_obs_logging.py::test_nesting_of_exactly_the_cap_is_rendered_and_one_deeper_is_truncated`."""

MAX_LOG_VALUE_CHARS = 2000
"""How long one rendered string may be before it is cut.

A log line is written per record and read by a line-oriented collector; one
caller logging a whole document would otherwise push every other line of that
request out of a size-capped buffer. Pinned inclusively from both sides by
`test_obs_logging.py::test_a_value_of_exactly_the_cap_is_kept_and_one_more_is_cut`."""

_RESERVED_RECORD_FIELDS: frozenset[str] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)
"""`LogRecord`'s own attributes, so everything else on it is the caller's `extra`."""


def redact(value: object, depth: int = 0) -> object:
    """One value rendered safe for a log line: pure, and never raising.

    Four rules, in order: too deep is `TRUNCATED`, a forbidden key's value is
    `REDACTED`, an over-long string is cut with `ELISION_MARKER`, and anything
    with no JSON form becomes its `repr`. Mappings and sequences are walked;
    the depth cap is what makes a cycle terminate rather than recurse.

    No I/O and no logging call of its own — this runs inside a formatter, so a
    log line emitted from here would recurse through the handler that called
    it.
    """
    if depth > MAX_REDACT_DEPTH:
        return TRUNCATED
    if isinstance(value, Mapping):
        return {
            str(key): (REDACTED if str(key) in FORBIDDEN_KEYS else redact(item, depth + 1))
            for key, item in value.items()
        }
    if isinstance(value, str):
        return _cut(value)
    if isinstance(value, bytes | bytearray):
        return _cut(value.decode("utf-8", errors="replace"))
    if isinstance(value, Sequence | set | frozenset):
        return [redact(item, depth + 1) for item in value]
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        # NaN and the infinities have no JSON form: `json.dumps` renders them
        # as bare words no strict parser accepts, and `allow_nan=False` would
        # raise inside the handler instead.
        if value != value or value in (float("inf"), float("-inf")):
            return repr(value)
        return value
    return _cut(repr(value))


def _cut(text: str) -> str:
    """`text`, cut to `MAX_LOG_VALUE_CHARS` with the marker if it is longer."""
    if len(text) <= MAX_LOG_VALUE_CHARS:
        return text
    return text[:MAX_LOG_VALUE_CHARS] + ELISION_MARKER


class JsonFormatter(logging.Formatter):
    """One record, one line, every value through `redact`.

    The bound `TelemetryContext` is read at FORMAT time, not at construction:
    a formatter that captured it once would stamp the first request's tenant
    on every later line of a long-lived process, pinned by
    `test_obs_context.py::test_a_bound_context_reaches_the_emitted_line`.

    The caller's `extra` is written before nothing and after the context, so a
    caller cannot overwrite `timestamp`, `level`, `logger` or `message`; the
    context fields it can shadow are its own call's anyway, and every key it
    supplies passes the same redaction as the rest.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(current_context().model_dump())
        payload.update(_extras(record))
        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info is not None:
            payload["stack"] = self.formatStack(record.stack_info)
        return _dumps(redact(payload))


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    """Whatever the caller passed as `extra`, and nothing `logging` owns."""
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _RESERVED_RECORD_FIELDS and not key.startswith("_")
    }


def _dumps(payload: object) -> str:
    """The line, or a minimal parseable one if serialising it fails.

    `redact` renders every value into a JSON-able form, so the fallback is
    unreachable by construction — which is exactly why it is here: the one
    value that proves the construction wrong must still leave a line rather
    than an exception raised inside a `logging` call.
    """
    try:
        return json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:  # pragma: no cover - see the docstring
        return json.dumps(
            {"level": "ERROR", "message": f"log record was not serialisable: {exc!r}"}
        )


def configure_logging(level: int = logging.INFO) -> None:
    """Install exactly one JSON handler on the root logger. Idempotent.

    Both services call this at startup and one test session imports both, so a
    second call must not double every line — a duplicated line is a duplicated
    fact for anyone counting them. Any handler this function installed before
    is removed first, which is what makes a re-call a replacement rather than
    an addition; handlers installed by anything else (pytest's capture, a
    host's own) are left alone. Pinned by
    `test_obs_logging.py::test_configure_logging_twice_leaves_one_handler`.
    """
    root = logging.getLogger()
    for existing in list(root.handlers):
        if isinstance(existing.formatter, JsonFormatter):
            root.removeHandler(existing)
            existing.close()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)
