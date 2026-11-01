"""Plain words the chat shows while a tool step runs, one line per tool.

Pure: no I/O. The words say what is happening, never what was found, so they
need no check before they are shown.
"""

from collections.abc import Mapping, Sequence
from types import MappingProxyType

__all__ = ["PROGRESS_WORDS", "SEVERAL", "progress_words"]

SEVERAL = "Looking a few things up"
"""Said when one step runs more than one kind of tool, or one with no words."""

PROGRESS_WORDS: Mapping[str, str] = MappingProxyType(
    {
        "get_me": "Checking who you are",
        "list_my_access": "Checking your access",
        "list_my_requests": "Checking your requests",
        "list_my_tickets": "Checking your tickets",
        "list_pending_approvals": "Checking what waits for your approval",
        "search_policy": "Reading the access rules",
        "classify_access_request": "Working out what this request needs",
        "find_role": "Finding the role",
        "find_person": "Finding the person",
        "find_department": "Finding the department",
        "note_request": "Noting your request",
        "create_access_request": "Getting your request ready",
        "revoke_access": "Getting the removal ready",
        "onboard_user": "Getting the new starter ready",
        "transfer_user": "Getting the move ready",
        "offboard_user": "Getting the leaver ready",
        "open_access_review": "Getting the review ready",
        "decide_review_item": "Getting your review answer ready",
        "approve_request": "Getting your approval ready",
        "reject_request": "Getting your rejection ready",
        "cancel_request": "Getting the cancel ready",
        "reset_own_password": "Getting your password reset ready",
        "unlock_own_account": "Getting your unlock ready",
        "create_ticket": "Getting your ticket ready",
    }
)
"""Each tool the loop may offer, in the words the person sees."""


def progress_words(tool_names: Sequence[str]) -> str:
    """The words for one tool step: one tool's own words, else `SEVERAL`."""
    kinds = set(tool_names)
    if len(kinds) == 1:
        return PROGRESS_WORDS.get(kinds.pop(), SEVERAL)
    return SEVERAL
