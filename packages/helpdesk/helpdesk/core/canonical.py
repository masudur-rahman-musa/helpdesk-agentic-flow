"""Deterministic JSON encoding, shared by everything that hashes a request.

The audit chain hashes events with it and the idempotency fingerprint
hashes tool arguments with it. Two copies of this encoder would be free to
drift, and a drift means either a chain that reads as tampered or a replay
that reads as a conflict, so there is exactly one.

Moved verbatim out of `audit/chain.py`, which re-exports it.
"""

import json
import uuid
from collections.abc import Mapping
from datetime import datetime


def _render(value: object) -> str:
    """Stringify the value types the JSON encoder cannot handle itself.

    Datetimes use ISO 8601 rather than `str()` so the rendering is the one
    the rest of the system reads and writes, offset included.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    raise TypeError(f"value of type {type(value).__name__} is not canonicalisable")


def canonical_json(mapping: Mapping[str, object]) -> bytes:
    """Serialise deterministically: keys sorted at every level, no whitespace.

    Writer and verifier must agree byte for byte, so this is the only place
    either of them turns a mapping into bytes.
    """
    return json.dumps(
        mapping, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_render
    ).encode()
