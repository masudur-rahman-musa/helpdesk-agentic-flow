"""The idempotency key a mutating call goes out with, derived not invented.

An earlier change decision D. The key is a `uuid5` over five values the turn already
carries — the session, the user's message, the round, the proposal's index and
the tool name — so the SAME turn, replanned in this process or resumed by plan
18 in another one, derives the SAME key and the request is filed once. A fresh
`uuid4` would file it twice under two keys, which is exactly the failure
`idempotency_keys` exists to prevent; a model-supplied key would be an
identifier the model is free to reuse across two DIFFERENT requests, which is
`IDEMPOTENCY_CONFLICT` taken by accident.

An earlier change adds the second caller: a decision the WEB initiates carries no turn,
so `derive_decision_key` takes the approver, the request and the verb instead.
An earlier change adds the third, `derive_review_decision_key`, over the reviewer, the
review item and the decision word, and an earlier change the fourth,
`derive_notification_read_key`, over the reader and the notification alone.
Four derivations, one namespace and one separator, in one module — which is
the whole reason it lives here rather than beside the routes.

Pure, and its own module so it stays that way: nothing here reads a clock, a
store or a random source, which is what
`test_agent_planning.py::test_the_same_five_values_derive_the_same_key_every_time`
asserts and what an earlier change needs when it re-derives a key for a replanned round.
"""

import uuid
from datetime import datetime

from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.models.idempotency import KEY_LENGTH

KEY_NAMESPACE = uuid.UUID("6f9619ff-8b86-5d11-b42d-00c04fc964ff")
"""The uuid5 namespace every derived key is taken under.

A fixed constant, never regenerated: it is part of the derivation, so a new
namespace would give the same turn a different key after a deploy and file a
resumed request a second time.
"""

_SEPARATOR = "\x1f"
"""ASCII unit separator, joining the values of every derivation above.

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


def derive_decision_key(*, approver_user_id: uuid.UUID, request_id: uuid.UUID, verb: str) -> str:
    """The key one web-initiated approval decision goes out with.

    A pure function of exactly three values, taken under the SAME namespace
    and separator as the turn's own keys above: agent-api has one module that
    makes idempotency keys, and a second derivation under this namespace is
    the drift a project rule is about.

    All three are in the digest and each one matters: without the approver two
    approvers deciding one request would share a key and the second would
    replay the first's answer; without the request id one approver's second
    decision would replay their first on a DIFFERENT request; without the verb
    an Approve and a Reject on one row would collapse into whichever was filed
    first. Pinned one value at a time by
    `test_approval_routes.py::test_changing_any_one_of_the_three_values_changes_the_key`,
    and against the namespace and the separator by
    `::test_the_key_is_the_uuid5_of_the_three_values_under_the_modules_namespace`.

    The comment and the reason are deliberately NOT in the material (decision
    B): a double-clicked Approve then replays the stored response rather than
    filing a second decision, which is what
    `::test_two_approvals_of_one_request_with_different_comments_send_one_key`
    asserts. The result is a canonical uuid string, length-checked before it
    can reach the wire (`::test_the_derived_key_fits_the_column_it_is_stored_in`).
    """
    material = _SEPARATOR.join([str(approver_user_id), str(request_id), verb])
    return check_idempotency_key(str(uuid.uuid5(KEY_NAMESPACE, material)))


def derive_review_decision_key(
    *, reviewer_user_id: uuid.UUID, item_id: uuid.UUID, decision: str
) -> str:
    """The key one web-initiated review decision goes out with.

    A sibling of `derive_decision_key` above rather than a reuse of it
    (decision B): the material has the same shape, but an approval's parameter
    names would lie about what is being digested — a review item is not a
    request, and the decision WORD is not a verb of this router's invention but
    a `ReviewDecision` member the tool declares.

    All three values are in the digest, under the same namespace and separator
    as every other key this module makes. Without the reviewer two reviewers
    deciding one item would share a key and the second would replay the first's
    answer; without the item one reviewer's second decision would replay their
    first on a DIFFERENT item; without the decision word a reviewer changing
    their mind would arrive under the Keep's key and be handed the stored Keep
    back as if the Revoke had worked, instead of meeting the tool's own
    re-decision refusal. Pinned one value at a time by
    `test_review_routes.py::test_changing_any_one_of_the_three_values_changes_the_key`,
    and against the namespace and the separator by
    `::test_the_key_is_the_uuid5_of_the_three_values_under_the_modules_namespace`.

    The same reviewer, item and decision derive the same key every time
    (`::test_the_same_reviewer_item_and_decision_derive_the_same_key_every_time`),
    which is what makes a double-clicked Keep a replay rather than a second
    decision. The result is a canonical uuid string, length-checked before it
    can reach the wire (`::test_the_derived_key_fits_the_column_it_is_stored_in`).
    """
    material = _SEPARATOR.join([str(reviewer_user_id), str(item_id), decision])
    return check_idempotency_key(str(uuid.uuid5(KEY_NAMESPACE, material)))


def derive_notification_read_key(*, reader_user_id: uuid.UUID, notification_id: uuid.UUID) -> str:
    """The key one web-initiated read stamp goes out with.

    A sibling of the two above rather than a reuse of either (decision B), and
    over TWO values with no verb: there is exactly one verb on this surface, so
    a verb in the material would be a constant pretending to be a variable, and
    `derive_decision_key`'s parameter names would lie about what is digested.

    Both values are in the digest, under the same namespace and separator as
    every other key this module makes. Without the reader two people stamping
    their own copy of one notification would share a key and the second would
    be handed the first's answer; without the notification id one reader's
    second stamp would replay their first on a DIFFERENT row. Pinned one value
    at a time by
    `test_notification_routes.py::test_changing_either_of_the_two_values_changes_the_key`,
    and against the namespace and the separator by
    `::test_the_key_is_the_uuid5_of_the_two_values_under_the_modules_namespace`.

    The same reader and notification derive the same key every time
    (`::test_the_same_reader_and_notification_derive_the_same_key_every_time`),
    so a double-clicked bell replays the stored response rather than filing a
    second call. The tool's already-read branch (23ga decision A) answers the
    STORED timestamp either way; the key is what keeps the second click from
    being a second call at all. The result is a canonical uuid string,
    length-checked before it can reach the wire
    (`::test_the_derived_key_fits_the_column_it_is_stored_in`).
    """
    material = _SEPARATOR.join([str(reader_user_id), str(notification_id)])
    return check_idempotency_key(str(uuid.uuid5(KEY_NAMESPACE, material)))


def derive_kill_switch_key(
    *,
    admin_user_id: uuid.UUID,
    tool_name: str,
    enabled: bool,
    scope: str,
    reason: str,
    minute: datetime,
) -> str:
    """The key one web-initiated kill-switch toggle goes out with.

    A sibling of the web keys above, under the same namespace and separator.
    The admin, the tool, the direction and the scope are all in the digest:
    without the direction a clear would replay the stored stop, and without
    the scope a tenant toggle would replay the platform-wide one. Pinned by
    `test_killswitch_routes.py::test_the_set_route_derives_the_key_and_the_body_still_forbids_one`.

    Unlike an approval, a switch can be set the same way twice on purpose:
    stop, clear, stop again. Keys live 7 days (the design), so a key over the
    first four values alone would hand that second stop the first one's stored
    answer while the tool kept running. The reason and the minute the click
    landed in are in the digest too, so a double click replays and a later
    re-stop is a new call. The minute is passed in, not read here, so this
    module still reads no clock.
    """
    stamp = minute.replace(second=0, microsecond=0).isoformat()
    material = _SEPARATOR.join([str(admin_user_id), tool_name, str(enabled), scope, reason, stamp])
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
    "derive_decision_key",
    "derive_idempotency_key",
    "derive_kill_switch_key",
    "derive_notification_read_key",
    "derive_review_decision_key",
]
