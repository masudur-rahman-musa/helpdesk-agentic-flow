"""The saved request draft: what the chat has gathered for one request.

Pure. The draft is kept on the chat row as plain JSON and checked here on
every read, like the open question of `confirmation.py`. The model fills only
what the latest message says; `merge_draft` lays that over the saved fields so
every later step reads one complete extraction (`test_request_draft.py`).
"""

from datetime import datetime, timedelta
from typing import Any

from pydantic import ValidationError

from helpdesk.agent_api.agent.gates import CONFIDENCE_THRESHOLD
from helpdesk.agent_api.agent.required_fields import is_non_answer
from helpdesk.agent_api.agent.state import Intent, IntentEntities, IntentExtraction, RequestDraft

__all__ = ["DRAFT_LIFE", "RequestDraft", "draft_dump", "merge_draft", "parse_request_draft"]

DRAFT_LIFE = timedelta(hours=24)
"""A draft this old or older counts as none: the person has moved on."""

_KEEPS_THE_DRAFT = frozenset({Intent.FOLLOW_UP, Intent.OTHER})
"""A message about the last reply, or one that fits nothing, never changes the subject."""

_FIELDS = tuple(IntentEntities.model_fields)


def parse_request_draft(raw: object, *, now: datetime) -> RequestDraft | None:
    """The stored draft, or `None` when it is missing, badly shaped or
    `DRAFT_LIFE` old or older."""
    if not isinstance(raw, dict):
        return None
    try:
        draft = RequestDraft.model_validate(raw)
    except ValidationError:
        return None
    if draft.saved_at.tzinfo is None or now - draft.saved_at >= DRAFT_LIFE:
        return None
    return draft


def draft_dump(draft: RequestDraft) -> dict[str, Any]:
    """The draft as the plain JSON the chat row holds."""
    return draft.model_dump(mode="json")


def _given(value: object) -> bool:
    """Whether the message gave this field. Blank text and an answer that gives
    nothing ("no reason") replace nothing."""
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip()) and not is_non_answer(value)
    return True


def _same_name(first: str, second: str) -> bool:
    return first.strip().lower() == second.strip().lower()


def _repeats(value: object, old: object) -> bool:
    """Whether a reason only repeats the saved one
    (`test_the_old_reason_repeated_with_a_new_role_is_dropped`)."""
    return (
        isinstance(value, str)
        and isinstance(old, str)
        and value.strip().lower() == old.strip().lower()
    )


def _changes_subject(
    draft: RequestDraft | None, extraction: IntentExtraction, threshold: float
) -> bool:
    """A clear intent other than the draft's, except the two that keep it."""
    return (
        draft is not None
        and extraction.intent not in _KEEPS_THE_DRAFT
        and extraction.intent is not draft.intent
        and extraction.confidence >= threshold
    )


def merge_draft(
    draft: RequestDraft | None,
    extraction: IntentExtraction,
    *,
    now: datetime,
    threshold: float = CONFIDENCE_THRESHOLD,
) -> tuple[IntentExtraction, RequestDraft | None]:
    """The message laid over the saved draft: the extraction the turn goes on
    with, and the draft to save (`None` when there is nothing to keep).

    A given field replaces the saved one. A new system drops the saved role and
    reason, and a new role drops the saved reason, even when the message
    repeats it word for word. A
    clear change of subject starts empty. The merged extraction keeps the
    message's own intent label, so routing sees a follow-up as one."""
    start = None if _changes_subject(draft, extraction, threshold) else draft
    base: dict[str, Any] = {name: getattr(start, name, None) for name in _FIELDS}
    message = extraction.entities
    new_system = (
        _given(message.system)
        and isinstance(base["system"], str)
        and not _same_name(base["system"], str(message.system))
    )
    new_role = (
        _given(message.role)
        and isinstance(base["role"], str)
        and not _same_name(base["role"], str(message.role))
    )
    old_reason = base["justification"]
    if new_system:
        base["role"] = None
    if new_system or new_role:
        # The reason was given for the old role; ask it again.
        base["justification"] = None
    for name in _FIELDS:
        value = getattr(message, name)
        if name == "justification" and base[name] is None and _repeats(value, old_reason):
            # The model copied the old reason from the history; it is not a new one.
            continue
        if _given(value) or base[name] is None:
            base[name] = value
    merged = extraction.model_copy(update={"entities": IntentEntities(**base)})

    if start is None and extraction.intent in _KEEPS_THE_DRAFT:
        return merged, None
    intent = extraction.intent if start is None else start.intent
    return merged, RequestDraft(intent=intent, saved_at=now, **base)
