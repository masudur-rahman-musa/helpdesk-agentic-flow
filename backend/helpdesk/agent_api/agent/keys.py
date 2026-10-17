"""Idempotency keys for changes, derived rather than random.

Each key is a `uuid5` over values the caller already has, so a retried or
resumed call gets the same key and the change happens once. Pure.
"""

import uuid
from datetime import datetime

from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.models.idempotency import KEY_LENGTH

KEY_NAMESPACE = uuid.UUID("6f9619ff-8b86-5d11-b42d-00c04fc964ff")
"""Fixed for good: a new namespace would give a resumed turn a new key and
file its request again."""

_SEPARATOR = "\x1f"
"""ASCII unit separator, which no input contains, so `("a", "bc")` and
`("ab", "c")` can never join to the same string."""


class InvalidIdempotencyKeyError(ToolError):
    """A key that is empty or too long for its column, refused before it is sent."""

    code = ErrorCode.VALIDATION


def derive_idempotency_key(
    *,
    session_id: uuid.UUID,
    user_message: str,
    round_number: int,
    index: int,
    tool_name: str,
) -> str:
    """The key for one planned call. Without the tool, round, index or session
    in it, two different calls could share a key."""
    material = _SEPARATOR.join(
        [str(session_id), user_message, str(round_number), str(index), tool_name]
    )
    return check_idempotency_key(str(uuid.uuid5(KEY_NAMESPACE, material)))


def derive_decision_key(*, approver_user_id: uuid.UUID, request_id: uuid.UUID, verb: str) -> str:
    """The key for one web approval decision. The comment is left out, so a
    double-clicked Approve replays instead of filing twice."""
    material = _SEPARATOR.join([str(approver_user_id), str(request_id), verb])
    return check_idempotency_key(str(uuid.uuid5(KEY_NAMESPACE, material)))


def derive_review_decision_key(
    *, reviewer_user_id: uuid.UUID, item_id: uuid.UUID, decision: str
) -> str:
    """The key for one web review decision. The decision is in it, so a changed
    mind is a new call that meets the tool's refusal, not an old answer."""
    material = _SEPARATOR.join([str(reviewer_user_id), str(item_id), decision])
    return check_idempotency_key(str(uuid.uuid5(KEY_NAMESPACE, material)))


def derive_notification_read_key(*, reader_user_id: uuid.UUID, notification_id: uuid.UUID) -> str:
    """The key for marking one notification read. Only two values: there is
    just one action here, so a double click replays instead of calling twice."""
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
    """The key for one web kill-switch change.

    A switch may rightly be set the same way twice (stop, clear, stop) and keys
    live for days, so the reason and the minute are in it too: a double click
    replays, a later stop is new. The caller passes the minute; no clock here.
    """
    stamp = minute.replace(second=0, microsecond=0).isoformat()
    material = _SEPARATOR.join([str(admin_user_id), tool_name, str(enabled), scope, reason, stamp])
    return check_idempotency_key(str(uuid.uuid5(KEY_NAMESPACE, material)))


def check_idempotency_key(value: str) -> str:
    """The key back, or a typed refusal before it is sent. Too long fails only
    after the change ran; empty would be shared by every call."""
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
