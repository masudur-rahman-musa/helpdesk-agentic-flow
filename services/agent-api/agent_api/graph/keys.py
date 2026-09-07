"""The idempotency key a mutating call goes out with, derived not invented.

An earlier change decision D. The key is a `uuid5` over five values the turn already
carries — the session, the user's message, the round, the proposal's index and
the tool name — so the SAME turn, replanned in this process or resumed by plan
18 in another one, derives the SAME key and the request is filed once. A fresh
`uuid4` would file it twice under two keys, which is exactly the failure
`idempotency_keys` exists to prevent; a model-supplied key would be an
identifier the model is free to reuse across two DIFFERENT requests, which is
`IDEMPOTENCY_CONFLICT` taken by accident.

Pure, and its own module so it stays that way: nothing here reads a clock, a
store or a random source, which is what
`test_agent_planning.py::test_the_same_five_values_derive_the_same_key_every_time`
asserts and what an earlier change needs when it re-derives a key for a replanned round.
"""

import uuid

from helpdesk.errors import ErrorCode, ToolError
from helpdesk.models.idempotency import KEY_LENGTH

KEY_NAMESPACE = uuid.UUID("6f9619ff-8b86-5d11-b42d-00c04fc964ff")
"""The uuid5 namespace every derived key is taken under.

A fixed constant, never regenerated: it is part of the derivation, so a new
namespace would give the same turn a different key after a deploy and file a
resumed request a second time.
"""

_SEPARATOR = "\x1f"
"""ASCII unit separator, joining the five values.

A character no session id, tool name or user message can contain, so
`("a", "bc")` and `("ab", "c")` cannot join to the same string — two different
turns sharing one key is the one thing this derivation may not do.
"""


class InvalidIdempotencyKeyError(ToolError):
    """A key the `idempotency_keys.key` column cannot hold, or an empty one.

    `VALIDATION` rather than `INTERNAL` because it is the same answer the
    guard gives for the same shape (the design), and it is raised BEFORE the
    key can reach the wire. Pinned at both ends of the bound by
    `test_agent_planning.py::test_a_key_one_character_over_the_column_length_is_refused_as_validation`
    and `::test_an_empty_key_is_refused_as_validation`.
    """

    code = ErrorCode.VALIDATION


def derive_idempotency_key(
    *,
    session_id: uuid.UUID,
    user_message: str,
    round_number: int,
    index: int,
    tool_name: str,
) -> str:
    """The key for one proposal, a pure function of exactly these five values.

    All five are in the digest and each one matters: without the tool name a
    classification and a request planned together would share a key, without
    the round a replanned round would replay the first round's answer, without
    the index two tickets in one round would collapse into one, and without
    the session two people's identical messages would collide. Pinned one
    value at a time by
    `test_agent_planning.py::test_changing_any_one_of_the_five_values_changes_the_key`.

    The result is a canonical uuid string, 36 characters, so it fits
    `KEY_LENGTH` with room to spare
    (`::test_the_derived_key_fits_the_column_it_is_stored_in`).
    """
    material = _SEPARATOR.join(
        [str(session_id), user_message, str(round_number), str(index), tool_name]
    )
    return check_idempotency_key(str(uuid.uuid5(KEY_NAMESPACE, material)))


def check_idempotency_key(value: str) -> str:
    """The key back, or a typed refusal before it can reach the wire.

    a project rule on a value this service produces rather than receives: the
    column is `String(KEY_LENGTH)`, so a longer key would abort AFTER the
    effect had run and surface as a database error instead of a code the design
    names. The bound is INCLUSIVE — exactly `KEY_LENGTH` characters is the
    longest the column holds and is accepted
    (`test_agent_planning.py::test_the_key_check_accepts_a_key_of_exactly_the_column_length`).

    Empty is refused too. An empty string is storable, so nothing downstream
    would refuse it, and every mutation in the turn would then claim the same
    key — one replay per call.
    """
    if not value or len(value) > KEY_LENGTH:
        raise InvalidIdempotencyKeyError(
            f"idempotency key must be 1..{KEY_LENGTH} characters, got {len(value)}"
        )
    return value


__all__ = [
    "KEY_NAMESPACE",
    "InvalidIdempotencyKeyError",
    "check_idempotency_key",
    "derive_idempotency_key",
]
