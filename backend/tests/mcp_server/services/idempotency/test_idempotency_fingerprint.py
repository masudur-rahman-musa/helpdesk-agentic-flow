"""The request fingerprint, written before the implementation.

Expected implementation surface:

``helpdesk.core.canonical.canonical_json(mapping: Mapping[str, object]) -> bytes``
    The deterministic encoder moved verbatim out of ``audit/chain.py`` and
    re-exported from there, so the audit API is unchanged and the
    fingerprint never imports the audit package.
    ``helpdesk.audit.chain.canonical_json`` must be the *same object*: two
    copies would be free to drift.

``helpdesk.idempotency.fingerprint.fingerprint(tool_name: str, args: Mapping[str, object]) -> str``
    ``sha256(canonical_json({"tool": tool_name, "args": args})).hexdigest()``
    — 64 lowercase hex chars. Pure: no I/O, no clock.

The fingerprint is what tells a replay from a conflict, so it must be
insensitive to argument key order (a client may serialise a dict any way)
and sensitive to everything else, including value types: ``1`` and ``"1"``
are different requests.
"""

import hashlib
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime

import pytest

from helpdesk.mcp_server.services.idempotency.fingerprint import fingerprint
from helpdesk.shared.core.canonical import canonical_json
from helpdesk.shared.audit import chain as audit_chain

TOOL = "revoke_access"
SAMPLE_UUID = uuid.UUID("11111111-2222-3333-4444-555555555555")
SAMPLE_TIME = datetime(2026, 3, 1, 9, 30, tzinfo=UTC)

ARGS: dict[str, object] = {
    "grant_id": str(SAMPLE_UUID),
    "reason": "fake reason",
    "cascade": False,
    "days": 30,
}


# --- shared bytes with the audit chain ----------------------------------------


def test_audit_chain_re_exports_the_same_canonical_json() -> None:
    """One encoder, one set of bytes: two copies could drift silently."""
    assert audit_chain.canonical_json is canonical_json


def test_fingerprint_is_sha256_of_the_canonical_bytes() -> None:
    expected = hashlib.sha256(canonical_json({"tool": TOOL, "args": ARGS})).hexdigest()
    assert fingerprint(TOOL, ARGS) == expected


def test_fingerprint_is_lowercase_hex_of_length_64() -> None:
    digest = fingerprint(TOOL, ARGS)
    assert len(digest) == 64
    assert digest == digest.lower()
    assert all(char in "0123456789abcdef" for char in digest)


def test_fingerprint_is_deterministic_across_calls() -> None:
    assert fingerprint(TOOL, ARGS) == fingerprint(TOOL, ARGS)


# --- key-order independence ---------------------------------------------------


def test_argument_key_order_does_not_change_the_fingerprint() -> None:
    reversed_insertion = dict(reversed(list(ARGS.items())))
    assert list(reversed_insertion) != list(ARGS)
    assert fingerprint(TOOL, reversed_insertion) == fingerprint(TOOL, ARGS)


def test_nested_key_order_does_not_change_the_fingerprint() -> None:
    forward: dict[str, object] = {"outer": {"a": 1, "b": {"x": True, "y": None}}}
    shuffled: dict[str, object] = {"outer": {"b": {"y": None, "x": True}, "a": 1}}
    assert fingerprint(TOOL, shuffled) == fingerprint(TOOL, forward)


def test_list_order_does_change_the_fingerprint() -> None:
    """Sequences are ordered data, not a set: [a, b] is not the same request as [b, a]."""
    assert fingerprint(TOOL, {"ids": [1, 2]}) != fingerprint(TOOL, {"ids": [2, 1]})


# --- type sensitivity ---------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        pytest.param(1, "1", id="int-vs-str"),
        pytest.param(True, 1, id="bool-vs-int"),
        pytest.param(False, 0, id="false-vs-zero"),
        pytest.param(None, "null", id="none-vs-str"),
        pytest.param(1, 1.5, id="int-vs-float"),
        pytest.param("", None, id="empty-str-vs-none"),
        pytest.param([1], [[1]], id="flat-vs-nested-list"),
        pytest.param({"a": 1}, [["a", 1]], id="object-vs-pairs"),
    ],
)
def test_values_of_different_types_fingerprint_differently(left: object, right: object) -> None:
    assert fingerprint(TOOL, {"value": left}) != fingerprint(TOOL, {"value": right})


# --- every part of the request participates -----------------------------------


def test_tool_name_participates() -> None:
    assert fingerprint("revoke_access", ARGS) != fingerprint("approve_request", ARGS)


def test_tool_name_and_args_cannot_be_confused_for_each_other() -> None:
    """A flat concatenation would let a crafted tool name imitate another request."""
    assert fingerprint("a", {"b": "c"}) != fingerprint("a\x00b", {"": "c"})


@pytest.mark.parametrize("key", sorted(ARGS))
def test_changing_any_single_argument_changes_the_fingerprint(key: str) -> None:
    changed = dict(ARGS)
    changed[key] = "obviously-different-fake-value"
    assert fingerprint(TOOL, changed) != fingerprint(TOOL, ARGS)


def test_adding_an_argument_changes_the_fingerprint() -> None:
    assert fingerprint(TOOL, {**ARGS, "extra": None}) != fingerprint(TOOL, ARGS)


def test_removing_an_argument_changes_the_fingerprint() -> None:
    without = {key: value for key, value in ARGS.items() if key != "reason"}
    assert fingerprint(TOOL, without) != fingerprint(TOOL, ARGS)


def test_empty_args_are_a_valid_request() -> None:
    digest = fingerprint("unlock_own_account", {})
    assert len(digest) == 64
    assert digest != fingerprint("reset_own_password", {})


# --- boundaries of the encoder ------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        pytest.param({"id": SAMPLE_UUID}, id="uuid"),
        pytest.param({"at": SAMPLE_TIME}, id="datetime"),
        pytest.param({"note": "grüße-fäke"}, id="non-ascii"),
    ],
)
def test_values_the_encoder_renders_are_fingerprintable(args: Mapping[str, object]) -> None:
    assert len(fingerprint(TOOL, args)) == 64


def test_a_type_with_no_stable_rendering_is_refused() -> None:
    """Accepting one would let the same request fingerprint two ways."""
    with pytest.raises(TypeError):
        fingerprint(TOOL, {"unsupported": object()})
