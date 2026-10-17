"""The pure hash-chain core, written before the implementation.

Expected implementation surface (`helpdesk/audit/chain.py`, pure: no I/O, no
clock, no imports from models or services):

``GENESIS_HASH: str``
    ``"0" * 64`` — the ``prev_hash`` of a tenant's first event.

``canonical_json(mapping: Mapping[str, object]) -> bytes``
    Deterministic JSON: keys sorted at every level, compact separators
    (``,`` and ``:``), ``uuid.UUID`` and ``datetime`` rendered as strings.
    Key insertion order of the input never changes the output.

``event_hash(prev_hash: str, event_fields: Mapping[str, object]) -> str``
    ``sha256(prev_hash || canonical_json(event_fields)).hexdigest()`` — 64
    lowercase hex chars.

``verify_chain(events: Sequence[Mapping[str, object]]) -> VerifyResult``
    Walks the events in ``seq`` order (the input order is not trusted) and
    recomputes each link. An event mapping carries the DB-assigned ``seq``,
    the stored ``prev_hash`` and ``hash``, plus the hashed fields; those
    three keys are excluded from the hash input (``seq``
    is assigned by the database after the hash is computed, so it can only
    order the walk). ``VerifyResult`` has ``ok: bool`` and
    ``broken_at: int | None`` — the ``seq`` of the FIRST event whose stored
    hash or link does not recompute, or ``None`` when the chain is intact.

The ANCHOR cases sit at the bottom of this file; every
case above it is older and is left exactly as it was. Surface the anchor
adds to the same pure module:

``class ChainAnchor(BaseModel)``
    ``audit_seq: int`` (>= 1) and ``audit_hash: str`` (``re.fullmatch`` of
    ``[0-9a-f]{64}``), both refused with a typed error. The validation itself
    is pinned in ``test_audit_anchor.py``; this file only builds valid ones.

``verify_chain(events, anchor: ChainAnchor | None = None) -> VerifyResult``
    ``VerifyResult`` gains ``truncated_at: int | None`` and
    ``anchored_seq: int | None``. Precedence is one
    verdict per case and the RECOMPUTE wins: a walk that breaks answers
    ``broken_at`` with ``truncated_at=None``, whatever the anchor says. With
    no anchor the verdict is byte-for-byte today's, which is why
    ``test_truncating_the_tail_is_not_detected_by_the_walk_alone`` above
    stays green unchanged: it is about the walk alone.
"""

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from helpdesk.shared.audit.chain import (
    GENESIS_HASH,
    ChainAnchor,
    VerifyResult,
    canonical_json,
    event_hash,
    verify_chain,
)

# --- fixed, obviously fake sample values --------------------------------------

SAMPLE_UUID = uuid.UUID("11111111-2222-3333-4444-555555555555")
SAMPLE_TIME = datetime(2026, 3, 1, 9, 30, tzinfo=UTC)


def _fields(event_type: str = "auth.login", **overrides: Any) -> dict[str, Any]:
    """One event's hashed fields (everything except seq / prev_hash / hash)."""
    fields: dict[str, Any] = {
        "occurred_at": SAMPLE_TIME,
        "actor_kind": "user",
        "actor_ref": str(SAMPLE_UUID),
        "session_id": str(SAMPLE_UUID),
        "trace_id": "trace-fake-1",
        "event_type": event_type,
        "subject_type": "user",
        "subject_id": str(SAMPLE_UUID),
        "payload": {"fake": True},
    }
    fields.update(overrides)
    return fields


def _linked(seq: int, prev_hash: str, **overrides: Any) -> dict[str, Any]:
    """A well-formed stored event: hashed fields + seq + prev_hash + correct hash."""
    fields = _fields(**overrides)
    return {**fields, "seq": seq, "prev_hash": prev_hash, "hash": event_hash(prev_hash, fields)}


def _chain(length: int) -> list[dict[str, Any]]:
    """`length` correctly linked events starting from genesis, seq 1..length."""
    events: list[dict[str, Any]] = []
    prev = GENESIS_HASH
    for i in range(1, length + 1):
        event = _linked(i, prev, trace_id=f"trace-fake-{i}")
        events.append(event)
        prev = str(event["hash"])
    return events


# --- genesis ------------------------------------------------------------------


def test_genesis_hash_is_sixty_four_zeroes() -> None:
    assert GENESIS_HASH == "0" * 64


# --- canonical_json -----------------------------------------------------------


def test_canonical_json_returns_bytes() -> None:
    assert isinstance(canonical_json({"a": 1}), bytes)


def test_canonical_json_sorts_keys_and_uses_compact_separators() -> None:
    assert canonical_json({"b": 1, "a": 2}) == b'{"a":2,"b":1}'


def test_canonical_json_is_key_order_independent() -> None:
    forward = {"a": 1, "b": {"x": 1, "y": 2}, "c": [1, 2]}
    reversed_insertion = {"c": [1, 2], "b": {"y": 2, "x": 1}, "a": 1}
    assert canonical_json(forward) == canonical_json(reversed_insertion)


def test_canonical_json_sorts_nested_objects_too() -> None:
    assert canonical_json({"outer": {"b": 1, "a": 2}}) == b'{"outer":{"a":2,"b":1}}'


def test_canonical_json_is_deterministic_across_calls() -> None:
    payload = _fields()
    assert canonical_json(payload) == canonical_json(payload)


@pytest.mark.parametrize(
    ("mapping", "expected"),
    [
        ({"id": SAMPLE_UUID}, f'{{"id":"{SAMPLE_UUID}"}}'.encode()),
        ({"at": SAMPLE_TIME}, f'{{"at":"{SAMPLE_TIME.isoformat()}"}}'.encode()),
        ({"nested": {"id": SAMPLE_UUID}}, f'{{"nested":{{"id":"{SAMPLE_UUID}"}}}}'.encode()),
        ({"none": None}, b'{"none":null}'),
        ({"nums": [1, 2]}, b'{"nums":[1,2]}'),
        ({"flag": True}, b'{"flag":true}'),
    ],
)
def test_canonical_json_renders_values(mapping: Mapping[str, object], expected: bytes) -> None:
    assert canonical_json(mapping) == expected


def test_canonical_json_refuses_a_type_it_has_no_stable_rendering_for() -> None:
    """Silently accepting one would let the same event hash two ways."""
    with pytest.raises(TypeError):
        canonical_json({"unsupported": object()})


def test_canonical_json_round_trips_non_ascii_text() -> None:
    """Encoding choice is free; the decoded value must survive unchanged."""
    encoded = canonical_json({"note": "grüße-fäke"})
    assert json.loads(encoded.decode()) == {"note": "grüße-fäke"}


# --- event_hash ---------------------------------------------------------------


def test_event_hash_matches_the_spec_formula() -> None:
    fields = _fields()
    expected = hashlib.sha256(GENESIS_HASH.encode() + canonical_json(fields)).hexdigest()
    assert event_hash(GENESIS_HASH, fields) == expected


def test_event_hash_is_lowercase_hex_of_length_64() -> None:
    digest = event_hash(GENESIS_HASH, _fields())
    assert len(digest) == 64
    assert digest == digest.lower()
    assert all(char in "0123456789abcdef" for char in digest)


def test_event_hash_ignores_field_insertion_order() -> None:
    fields = _fields()
    shuffled = dict(reversed(list(fields.items())))
    assert event_hash(GENESIS_HASH, shuffled) == event_hash(GENESIS_HASH, fields)


def test_event_hash_changes_with_prev_hash() -> None:
    fields = _fields()
    other_prev = "1" * 64
    assert event_hash(other_prev, fields) != event_hash(GENESIS_HASH, fields)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event_type", "auth.failed"),
        ("actor_kind", "system"),
        ("actor_ref", str(uuid.UUID(int=7))),
        ("payload", {"fake": False}),
        ("occurred_at", datetime(2026, 3, 1, 9, 30, 1, tzinfo=UTC)),
    ],
)
def test_event_hash_changes_with_any_field(field: str, value: object) -> None:
    baseline = event_hash(GENESIS_HASH, _fields())
    changed = _fields()
    changed[field] = value
    assert event_hash(GENESIS_HASH, changed) != baseline


# --- verify_chain -------------------------------------------------------------


def test_empty_chain_verifies_ok() -> None:
    result = verify_chain([])
    assert result.ok is True
    assert result.broken_at is None


@pytest.mark.parametrize("length", [1, 2, 5])
def test_intact_chain_verifies_ok(length: int) -> None:
    result = verify_chain(_chain(length))
    assert result.ok is True, f"chain of {length} should verify"
    assert result.broken_at is None


def test_verify_walks_in_seq_order_not_input_order() -> None:
    events = _chain(4)
    shuffled: Sequence[Mapping[str, object]] = [events[2], events[0], events[3], events[1]]
    result = verify_chain(shuffled)
    assert result.ok is True
    assert result.broken_at is None


def test_first_event_must_start_from_genesis() -> None:
    events = _chain(3)
    events[0] = _linked(1, "f" * 64, trace_id="trace-fake-1")
    result = verify_chain(events)
    assert result.ok is False
    assert result.broken_at == 1


def test_tampered_payload_is_reported_at_that_event_seq() -> None:
    events = _chain(4)
    events[2]["payload"] = {"fake": "tampered"}  # hash no longer recomputes
    result = verify_chain(events)
    assert result.ok is False
    assert result.broken_at == 3


def test_broken_link_is_reported_at_the_event_whose_prev_hash_is_wrong() -> None:
    events = _chain(4)
    events[1]["prev_hash"] = "a" * 64
    result = verify_chain(events)
    assert result.ok is False
    assert result.broken_at == 2


def test_only_the_first_broken_link_is_reported() -> None:
    """Tampering event 2 also invalidates 3 and 4; verify names 2, the earliest."""
    events = _chain(4)
    events[1]["payload"] = {"fake": "tampered"}
    events[3]["payload"] = {"fake": "also-tampered"}
    result = verify_chain(events)
    assert result.ok is False
    assert result.broken_at == 2


def test_a_deleted_middle_event_breaks_the_chain() -> None:
    """Rows removed behind the trigger must not verify as an intact chain."""
    events = _chain(4)
    del events[1]
    result = verify_chain(events)
    assert result.ok is False
    assert result.broken_at == 3


def test_stored_hash_replaced_by_a_plausible_digest_still_fails() -> None:
    """Recomputing the whole tail is the attack; the first altered link is named."""
    events = _chain(3)
    events[1]["hash"] = "b" * 64
    result = verify_chain(events)
    assert result.ok is False
    assert result.broken_at == 2


def test_truncating_the_tail_is_not_detected_by_the_walk_alone() -> None:
    """A known limit, asserted so nobody reads more into `ok` than it means.

    Removing the newest events leaves a shorter chain that still links, so
    verification cannot see it. Catching truncation needs an anchor outside
    the table (a witnessed head), which this row does not have.
    """
    result = verify_chain(_chain(5)[:2])
    assert result.ok is True
    assert result.broken_at is None


def test_non_contiguous_seq_values_are_accepted_when_links_hold() -> None:
    """seq only orders the walk; gaps from rolled-back inserts are fine."""
    events = _chain(3)
    for event, seq in zip(events, (10, 20, 31), strict=True):
        event["seq"] = seq
    result = verify_chain(events)
    assert result.ok is True
    assert result.broken_at is None


# --- verify_chain against an anchor -------------------------------


def _hashed(event: Mapping[str, Any]) -> dict[str, Any]:
    """One stored event's hashed fields: everything except seq, prev_hash, hash.

    Written out here rather than imported from the module under test, so a
    re-chaining case cannot inherit the same mistake from the code it judges.
    """
    return {key: value for key, value in event.items() if key not in {"seq", "prev_hash", "hash"}}


def _tampered_chain(length: int, seq: int) -> list[dict[str, Any]]:
    """`length` linked events with the row at `seq` edited and its hash left alone."""
    events = _chain(length)
    events[seq - 1]["payload"] = {"fake": "tampered"}
    return events


def _anchor_at(events: list[dict[str, Any]], index: int) -> ChainAnchor:
    """The anchor a verified sweep would have written for `events[index]`.

    Read off the SAME list the case verifies, so the
    anchored hash is the one that list really carries and a case that expects
    a MATCH cannot pass because of a typo in a literal digest.
    """
    return ChainAnchor(
        audit_seq=int(str(events[index]["seq"])), audit_hash=str(events[index]["hash"])
    )


def test_an_anchor_naming_a_surviving_seq_with_its_hash_verifies_ok() -> None:
    """Catches an implementation that treats the presence of an anchor as
    suspicion in itself — one that reports `truncated_at` or `ok=False`
    whenever an anchor is passed — and, through `anchored_seq`, one that
    verifies against the anchor and then forgets to say it did. A reader
    cannot tell "verified against a head" from "never anchored" without that
    field, and every intact tenant would look unanchored for ever.
    """
    events = _chain(5)

    result = verify_chain(events, anchor=_anchor_at(events, 2))

    assert result.ok is True
    assert result.broken_at is None
    assert result.truncated_at is None
    assert result.anchored_seq == 3


def test_a_tail_removed_past_the_anchor_is_reported_as_truncated() -> None:
    """The whole point of the anchor. Catches the implementation this module has
    shipped until now: the walk alone recomputes perfectly over a shortened
    chain, so without the anchor being consulted at all this answers `ok=True`
    — which is exactly what the untouched
    `test_truncating_the_tail_is_not_detected_by_the_walk_alone` above asserts
    for the no-anchor call. The two cases differ in one argument, so this one
    fails if and only if the anchor branch is missing.

    `broken_at` is asserted None as well, to catch reporting truncation
    through the old field: "broken at event 5" naming a row that
    no longer exists is a false sentence, and a web panel reading `broken_at`
    would point an auditor at the wrong row.
    """
    events = _chain(5)
    anchor = _anchor_at(events, 4)

    result = verify_chain(events[:2], anchor=anchor)

    assert result.ok is False
    assert result.truncated_at == 5
    assert result.broken_at is None


def test_a_rechained_log_is_caught_by_the_anchored_hash() -> None:
    """The second gap, the one a hash chain cannot see at all: an attacker who
    edits a row and re-chains every link from genesis leaves a log that
    recomputes cleanly end to end.

    Catches an implementation whose anchor branch only asks whether the
    anchored SEQ still exists (the truncation test) and never compares the
    stored hash: seq 3 is present here, the walk is flawless, and only the
    remembered digest disagrees. Also catches one that reports this through
    `truncated_at` — nothing was truncated, a row was rewritten, and the two
    facts send an auditor to different places.
    """
    original = _chain(5)
    anchor = _anchor_at(original, 2)
    rechained = _chain(5)
    rechained[1]["payload"] = {"fake": "rewritten"}
    prev = str(rechained[0]["hash"])
    for event in rechained[1:]:
        event["prev_hash"] = prev
        event["hash"] = event_hash(prev, _hashed(event))
        prev = str(event["hash"])

    assert verify_chain(rechained).ok is True, "premise: the rewritten log recomputes cleanly"
    assert str(rechained[2]["hash"]) != anchor.audit_hash, "premise: the anchored row moved"

    result = verify_chain(rechained, anchor=anchor)

    assert result.ok is False
    assert result.broken_at == 3
    assert result.truncated_at is None


def test_a_broken_link_wins_over_a_truncated_tail() -> None:
    """The precedence criterion: one verdict per case, and the recompute
    wins.

    Catches an implementation that checks the anchor FIRST and returns, which
    would answer `truncated_at=5` here and hide the tampered row at seq 2
    entirely — the log has been edited AND shortened, and the earlier, more
    specific fact is the one an auditor has to be given. Both fields are
    asserted, so an implementation reporting both at once fails too.
    """
    events = _chain(5)
    anchor = _anchor_at(events, 4)
    surviving = events[:3]
    surviving[1]["payload"] = {"fake": "tampered"}

    result = verify_chain(surviving, anchor=anchor)

    assert result.ok is False
    assert result.broken_at == 2
    assert result.truncated_at is None


def test_an_empty_list_with_an_anchor_is_truncated() -> None:
    """The boundary row of the truncation decision: nothing at all survives.

    Catches an implementation that short-circuits on an empty list before it
    reads the anchor — the `for` loop has nothing to walk, so `ok=True` falls
    straight out and a tenant whose whole log was deleted reads as healthy,
    which is the loudest failure this change exists to prevent.
    """
    events = _chain(3)
    anchor = _anchor_at(events, 2)

    result = verify_chain([], anchor=anchor)

    assert result.ok is False
    assert result.truncated_at == 3
    assert result.broken_at is None


def test_an_empty_list_with_no_anchor_is_ok_and_never_anchored() -> None:
    """The other half of the same boundary, and the one a false positive would
    break: a tenant that has never emitted an event and has never been
    anchored is healthy, not truncated.

    Catches an implementation that invents an anchor (a zero, a genesis row)
    when none was passed, which would make every new tenant's first scrape
    report a tampered log. `anchored_seq is None` is the fact that separates
    this from the case above.
    """
    result = verify_chain([], anchor=None)

    assert result.ok is True
    assert result.broken_at is None
    assert result.truncated_at is None
    assert result.anchored_seq is None


@pytest.mark.parametrize(
    ("label", "events", "expected"),
    [
        ("intact", _chain(3), VerifyResult(ok=True, broken_at=None)),
        ("tampered", _tampered_chain(3, 2), VerifyResult(ok=False, broken_at=2)),
        ("truncated", _chain(5)[:2], VerifyResult(ok=True, broken_at=None)),
    ],
)
def test_verify_chain_without_an_anchor_returns_todays_verdict(
    label: str, events: list[dict[str, Any]], expected: VerifyResult
) -> None:
    """With no anchor the answer is exactly today's.

    The comparison is against a whole `VerifyResult` built from the two OLD
    fields, so both new fields must default to None. Catches an anchor
    argument made mandatory (every existing caller — the route, the gauge, the
    MCP path — would raise), and catches an implementation that fills
    `truncated_at` or `anchored_seq` from somewhere other than an anchor it
    was given, which would put the `truncated` row above at `ok=False` with no
    head to justify it.
    """
    assert verify_chain(events) == expected, label
