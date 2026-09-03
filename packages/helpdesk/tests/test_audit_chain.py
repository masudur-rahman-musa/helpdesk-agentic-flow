"""An earlier change section 6: the pure hash-chain core, written before the implementation.

Expected implementation surface (`helpdesk/audit/chain.py`, pure: no I/O, no
clock, no imports from models or services):

``GENESIS_HASH: str``
    ``"0" * 64`` — the ``prev_hash`` of a tenant's first event (the design).

``canonical_json(mapping: Mapping[str, object]) -> bytes``
    Deterministic JSON: keys sorted at every level, compact separators
    (``,`` and ``:``), ``uuid.UUID`` and ``datetime`` rendered as strings.
    Key insertion order of the input never changes the output.

``event_hash(prev_hash: str, event_fields: Mapping[str, object]) -> str``
    ``sha256(prev_hash || canonical_json(event_fields)).hexdigest()`` — 64
    lowercase hex chars (the design).

``verify_chain(events: Sequence[Mapping[str, object]]) -> VerifyResult``
    Walks the events in ``seq`` order (the input order is not trusted) and
    recomputes each link. An event mapping carries the DB-assigned ``seq``,
    the stored ``prev_hash`` and ``hash``, plus the hashed fields; those
    three keys are excluded from the hash input (an earlier change decision B: ``seq``
    is assigned by the database after the hash is computed, so it can only
    order the walk). ``VerifyResult`` has ``ok: bool`` and
    ``broken_at: int | None`` — the ``seq`` of the FIRST event whose stored
    hash or link does not recompute, or ``None`` when the chain is intact.
"""

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from helpdesk.audit.chain import GENESIS_HASH, canonical_json, event_hash, verify_chain

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
    """seq only orders the walk (decision B); gaps from rolled-back inserts are fine."""
    events = _chain(3)
    for event, seq in zip(events, (10, 20, 31), strict=True):
        event["seq"] = seq
    result = verify_chain(events)
    assert result.ok is True
    assert result.broken_at is None
