"""The hash chain itself. Pure: no I/O, no clock, no models, no services.

Every audit event stores the hash of its predecessor, so altering one row
invalidates every row after it and verification can name the first row that
does not recompute (the design).

`seq` is assigned by the database at insert time, after the hash has been
computed, so it is deliberately excluded from the hashed fields (an earlier change
decision B). It orders the verify walk and nothing else: gaps left by
rolled-back inserts are normal and do not break a chain.

Known limitation: a chain links each event to its predecessor, so removing
events from the END leaves a shorter chain that still verifies. Editing or
removing anything else is caught, but truncation is not, and no claim to
the contrary should be made from this module. Detecting it needs an anchor
this row does not have — a signed head published outside the database, or a
periodic external witness of (tenant, last seq, last hash).
"""

import hashlib
from collections.abc import Mapping, Sequence

from pydantic import BaseModel

from helpdesk.core.canonical import canonical_json

__all__ = [
    "GENESIS_HASH",
    "VerifyResult",
    "canonical_json",
    "event_hash",
    "hashed_fields",
    "verify_chain",
]

GENESIS_HASH = "0" * 64
"""The `prev_hash` of a tenant's first event."""

_UNHASHED_KEYS = frozenset({"seq", "prev_hash", "hash"})


class VerifyResult(BaseModel):
    """Outcome of walking one tenant's chain."""

    ok: bool
    broken_at: int | None = None
    """`seq` of the first event that does not recompute, else None."""


def event_hash(prev_hash: str, event_fields: Mapping[str, object]) -> str:
    """sha256(prev_hash || canonical_json(fields)) as 64 lowercase hex chars."""
    return hashlib.sha256(prev_hash.encode() + canonical_json(event_fields)).hexdigest()


def hashed_fields(event: Mapping[str, object]) -> dict[str, object]:
    """The part of a stored event that the hash covers."""
    return {key: value for key, value in event.items() if key not in _UNHASHED_KEYS}


def verify_chain(events: Sequence[Mapping[str, object]]) -> VerifyResult:
    """Recompute every link in `seq` order and report the first mismatch.

    A missing row shows up as the break at its successor, whose `prev_hash`
    then points at a hash no surviving row carries.
    """
    expected_prev = GENESIS_HASH
    for event in sorted(events, key=lambda item: int(str(item["seq"]))):
        stored_prev = str(event["prev_hash"])
        recomputed = event_hash(stored_prev, hashed_fields(event))
        if stored_prev != expected_prev or recomputed != str(event["hash"]):
            return VerifyResult(ok=False, broken_at=int(str(event["seq"])))
        expected_prev = recomputed
    return VerifyResult(ok=True, broken_at=None)
