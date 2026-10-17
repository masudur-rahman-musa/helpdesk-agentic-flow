"""One line of JSON per record, with identity redacted on the way out.

Redaction is in the formatter, not a filter: a filter is per handler, so a new
handler would log unredacted. A redacted key stays, marked, so it is not
mistaken for one never set. Nothing here raises into the caller.
"""

import json
import logging
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from helpdesk.shared.core.observability.context import current_context

FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {"email", "display_name", "password", "user_id", "actor_ref"}
)
"""Keys kept out of telemetry at any depth. Add any new field that names a person."""

REDACTED = "[redacted]"

TRUNCATED = "[truncated]"

ELISION_MARKER = "…"

MAX_REDACT_DEPTH = 6
"""Also what stops a structure that contains itself."""

MAX_LOG_VALUE_CHARS = 2000
"""So one huge value cannot push other lines out of a size-capped log buffer."""

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
    """One value made safe for a log line. Never raises or logs: it runs in a formatter."""
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
        # NaN and infinity have no JSON form, so render them as text.
        if value != value or value in (float("inf"), float("-inf")):
            return repr(value)
        return value
    return _cut(repr(value))


def _cut(text: str) -> str:
    if len(text) <= MAX_LOG_VALUE_CHARS:
        return text
    return text[:MAX_LOG_VALUE_CHARS] + ELISION_MARKER


class JsonFormatter(logging.Formatter):
    """One record, one line, every value through `redact`.

    The context is read per line, not once, or every line would carry the first tenant.
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
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _RESERVED_RECORD_FIELDS and not key.startswith("_")
    }


def _dumps(payload: object) -> str:
    """The line, or a minimal one if serialising fails, so logging never raises."""
    try:
        return json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:  # pragma: no cover - see the docstring
        return json.dumps(
            {"level": "ERROR", "message": f"log record was not serialisable: {exc!r}"}
        )


def configure_logging(level: int = logging.INFO) -> None:
    """Install one JSON handler on the root logger, replacing its own earlier one.

    A second call must not double every line. Other handlers are left alone.
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
