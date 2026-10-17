"""The hash chain itself. Pure: no I/O, no clock, no models.

Each event stores the hash of the one before it, so changing a row breaks
every row after it. `seq` is set by the database after the hash is made, so
it is not hashed; it only orders the walk, and gaps from rolled-back inserts
are normal.

The walk alone cannot see a log cut short at the end, or one re-chained from
the start. Both are caught by an anchor: the last (seq, hash) remembered
after a good check.
"""

import hashlib
import re
from collections.abc import Mapping, Sequence

from pydantic import BaseModel, field_validator

from helpdesk.shared.core.canonical import canonical_json
from helpdesk.shared.core.errors import ErrorCode, ToolError

__all__ = [
    "ANCHOR_HASH_PATTERN",
    "GENESIS_HASH",
    "MIN_ANCHOR_SEQ",
    "ChainAnchor",
    "InvalidChainAnchorError",
    "VerifyResult",
    "canonical_json",
    "event_hash",
    "hashed_fields",
    "verify_chain",
]

GENESIS_HASH = "0" * 64
"""The `prev_hash` of a tenant's first event."""

_UNHASHED_KEYS = frozenset({"seq", "prev_hash", "hash"})

ANCHOR_HASH_PATTERN = r"[0-9a-f]{64}"
"""What an anchored hash may be: exactly what `event_hash` emits.

Uppercase is refused: it could never equal a stored hash, so every later
check would read the log as rewritten."""

MIN_ANCHOR_SEQ = 1
"""The lowest `audit_seq` an anchor may name. Anchoring at 0 would point at a
row that can never exist, and the tenant would read as cut short for ever."""

_ANCHOR_HASH_RE = re.compile(ANCHOR_HASH_PATTERN)


class InvalidChainAnchorError(ToolError):
    """An anchor whose seq or hash could not have come from a real chain."""

    code = ErrorCode.VALIDATION


class ChainAnchor(BaseModel):
    """A tenant's chain head as last checked. Plain data, so this module stays free of I/O."""

    audit_seq: int
    audit_hash: str

    @field_validator("audit_seq")
    @classmethod
    def _check_seq(cls, value: int) -> int:
        if value < MIN_ANCHOR_SEQ:
            raise InvalidChainAnchorError(
                f"an anchored seq below {MIN_ANCHOR_SEQ} names no event: {value}"
            )
        return value

    @field_validator("audit_hash")
    @classmethod
    def _check_hash(cls, value: str) -> str:
        # `fullmatch`: `re.match` or a Pydantic `pattern=` would accept a
        # digest with a newline glued on.
        if _ANCHOR_HASH_RE.fullmatch(value) is None:
            raise InvalidChainAnchorError(
                f"an anchored hash must match {ANCHOR_HASH_PATTERN}: {value!r}"
            )
        return value


class VerifyResult(BaseModel):
    """Outcome of walking one tenant's chain, against its anchor where there is one.

    At most one of `broken_at` (a row changed) and `truncated_at` (the rest
    is gone) is set.
    """

    ok: bool
    broken_at: int | None = None
    """`seq` of the first event that does not recompute, else None."""
    truncated_at: int | None = None
    """The anchored `seq` no surviving event carries, else None."""
    anchored_seq: int | None = None
    """The `seq` this verdict was checked against, None when never anchored."""


def event_hash(prev_hash: str, event_fields: Mapping[str, object]) -> str:
    """sha256(prev_hash || canonical_json(fields)) as 64 lowercase hex chars."""
    return hashlib.sha256(prev_hash.encode() + canonical_json(event_fields)).hexdigest()


def hashed_fields(event: Mapping[str, object]) -> dict[str, object]:
    """The part of a stored event that the hash covers."""
    return {key: value for key, value in event.items() if key not in _UNHASHED_KEYS}


def verify_chain(
    events: Sequence[Mapping[str, object]], anchor: ChainAnchor | None = None
) -> VerifyResult:
    """Recompute every link in `seq` order, then hold the result to the anchor.

    A missing row shows up as a break at the row after it. The walk's
    verdict wins over the anchor's, since an edited row is the more exact
    fact. An anchor adds two checks: its seq is gone (cut short), or it is
    there with a different hash (re-chained).
    """
    anchored_seq = anchor.audit_seq if anchor is not None else None
    expected_prev = GENESIS_HASH
    for event in sorted(events, key=lambda item: int(str(item["seq"]))):
        stored_prev = str(event["prev_hash"])
        recomputed = event_hash(stored_prev, hashed_fields(event))
        if stored_prev != expected_prev or recomputed != str(event["hash"]):
            return VerifyResult(
                ok=False, broken_at=int(str(event["seq"])), anchored_seq=anchored_seq
            )
        expected_prev = recomputed
    if anchor is None:
        return VerifyResult(ok=True)
    anchored = _event_at(events, anchor.audit_seq)
    if anchored is None:
        return VerifyResult(ok=False, truncated_at=anchor.audit_seq, anchored_seq=anchored_seq)
    if str(anchored["hash"]) != anchor.audit_hash:
        return VerifyResult(ok=False, broken_at=anchor.audit_seq, anchored_seq=anchored_seq)
    return VerifyResult(ok=True, anchored_seq=anchored_seq)


def _event_at(events: Sequence[Mapping[str, object]], seq: int) -> Mapping[str, object] | None:
    """The event carrying `seq`, or None when no surviving row does."""
    for event in events:
        if int(str(event["seq"])) == seq:
            return event
    return None
