"""Deterministic JSON encoding for everything that hashes data.

The audit chain and the idempotency fingerprint both use it. Two copies could
drift, and a drift makes a good chain read as tampered or a replay as a conflict.
"""

import json
import uuid
from collections.abc import Mapping
from datetime import datetime


def _render(value: object) -> str:
    """Stringify datetimes (ISO 8601, offset kept) and UUIDs."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    raise TypeError(f"value of type {type(value).__name__} is not canonicalisable")


def canonical_json(mapping: Mapping[str, object]) -> bytes:
    """Serialise with sorted keys and no whitespace.

    Writer and verifier must agree byte for byte, so both come through here.
    """
    return json.dumps(
        mapping, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_render
    ).encode()
