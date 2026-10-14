"""The hash chain itself. Pure: no I/O, no clock, no models, no services.

Every audit event stores the hash of its predecessor, so altering one row
invalidates every row after it and verification can name the first row that
does not recompute (the design).

`seq` is assigned by the database at insert time, after the hash has been
computed, so it is deliberately excluded from the hashed fields (an earlier change
decision B). It orders the verify walk and nothing else: gaps left by
rolled-back inserts are normal and do not break a chain.

The walk ALONE cannot see two things, and both are why `verify_chain` takes
an anchor. Removing events from the END leaves a shorter chain
that still recomputes perfectly, and a log re-chained wholesale from genesis
recomputes too — an attacker who edits a row and relinks every event after it
leaves nothing for a walk to catch. What catches both is a head remembered
outside the walk: an anchor of (last seq, last hash), written only after a
verify that already agreed with the anchor before it, which
`audit.anchor.anchor_tenant_chain` is. With no anchor passed, this module
still makes no claim about either failure, which is what
`test_audit_chain.py::test_truncating_the_tail_is_not_detected_by_the_walk_alone`
records.
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

The narrower of the two gates the value passes — the column is `String(64)`,
which admits 64 of anything — so the inclusive boundary is written against
this one, by
`test_audit_anchor.py::test_an_anchor_hash_of_exactly_sixty_four_lowercase_hex_characters_is_accepted`
and `::test_an_anchor_hash_outside_the_pattern_is_refused_with_the_typed_error`.
Uppercase is excluded because `event_hash` never emits it: an uppercase
anchor could never equal a stored hash, so every later verify would read the
log as rewritten."""

MIN_ANCHOR_SEQ = 1
"""The lowest `audit_seq` an anchor may name: the first value the identity
hands out. A floor of 0 would let a tenant with no events be anchored at a
row that can never exist, and that tenant would then read as truncated for
ever. Pinned by
`test_audit_anchor.py::test_an_anchor_seq_below_one_is_refused_with_the_typed_error`."""

_ANCHOR_HASH_RE = re.compile(ANCHOR_HASH_PATTERN)


class InvalidChainAnchorError(ToolError):
    """An anchor whose seq or hash could not have come from a real chain.

    `VALIDATION`, the the design code for an input that has to be fixed rather
    than retried. Raised before any INSERT is built; pinned
    by the boundary and rejection cases in `test_audit_anchor.py`.
    """

    code = ErrorCode.VALIDATION


class ChainAnchor(BaseModel):
    """A tenant's chain head as it was last verified: one seq, one hash.

    Data, not a row: the pure verdict below takes it as an argument, which is
    what keeps this module free of I/O while still answering the two
    questions the walk cannot.
    """

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
        # `re.fullmatch`, never `re.match` or a Pydantic `pattern=` (standing
        # rule 4): both anchor at the start only, so a 64-character digest
        # with a newline glued on would be accepted, and no operator reading
        # a shipped log line could tell it from the real one.
        if _ANCHOR_HASH_RE.fullmatch(value) is None:
            raise InvalidChainAnchorError(
                f"an anchored hash must match {ANCHOR_HASH_PATTERN}: {value!r}"
            )
        return value


class VerifyResult(BaseModel):
    """Outcome of walking one tenant's chain, against its anchor where there is one.

    Exactly one of `broken_at` and `truncated_at` is ever set, and they are
    two different facts: a row was rewritten HERE, versus everything after
    here is gone. Reporting the second through the first would name the last
    GOOD event as broken, which sends an auditor to the wrong row (an earlier change
    decision E).
    """

    ok: bool
    broken_at: int | None = None
    """`seq` of the first event that does not recompute, else None."""
    truncated_at: int | None = None
    """The anchored `seq` no surviving event carries, else None."""
    anchored_seq: int | None = None
    """The `seq` this verdict was checked against, None when never anchored.

    "Verified against a head" and "never anchored" are different facts, and
    without this field a reader cannot tell them apart; pinned by
    `test_audit_chain.py::test_an_empty_list_with_no_anchor_is_ok_and_never_anchored`."""


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

    A missing row shows up as the break at its successor, whose `prev_hash`
    then points at a hash no surviving row carries.

    The walk runs FIRST and its verdict wins: a chain that is both edited and
    shortened answers `broken_at`, because the earlier and more specific fact
    is the one an auditor needs. Pinned by
    `test_audit_chain.py::test_a_broken_link_wins_over_a_truncated_tail`.

    With no anchor the answer is exactly what it was before an earlier change, which
    is what lets every caller that never learned about anchors keep calling.
    With one, two further failures become visible: the anchored seq no longer
    present (truncation), and present under a different hash (a log re-chained
    from genesis).
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
