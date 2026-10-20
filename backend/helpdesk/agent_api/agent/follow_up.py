"""Fixed words for the three things a message can ask besides a new request.

Pure. A message about the last reply ("say it shorter", "why?") reshapes that
stored reply; a message that holds two requests gets one fixed line naming the
second; a message that cannot be placed gets an honest line saying what the
assistant helps with. A message asking to skip approval is told first that
it cannot be. None of the words here comes from the model
(`test_follow_up.py`, `test_second_request.py`).
"""

from collections.abc import Mapping, Sequence

from helpdesk.agent_api.agent.pushback import SKIP_LINE, skips_approval
from helpdesk.agent_api.agent.state import Intent, TurnState

ASSISTANT_PREFIX = "assistant: "

CANNOT_PLACE_REPLY = (
    "I am not sure what you need there. I can help with access to a system, "
    "with a password reset or an unlocked account, with where your requests "
    "and tickets stand, and with questions about the rules. Tell me which of "
    "these you want and I will take it from there."
)
"""Said when a message cannot be placed. It uses no decision word and asks nothing."""

OUT_OF_SCOPE_REPLY = (
    "Sorry, I can't help with that here. I can help with access requests, "
    "tickets, passwords and account unlocks."
)
"""Said on an `other` turn that has nothing better to say: a plain no, never
text that reads like a fault (`test_out_of_scope_reply_on_other_intent`)."""

INTENT_NAMES: Mapping[Intent, str] = {
    Intent.RESET_PASSWORD: "a password reset",
    Intent.UNLOCK_ACCOUNT: "an unlocked account",
    Intent.REQUEST_ACCESS: "access to a system",
    Intent.REVOKE_ACCESS: "taking access away",
    Intent.REPORT_MOVER: "a change of department",
    Intent.ONBOARD: "a new joiner's starting access",
    Intent.OFFBOARD: "a leaver's access removed",
    Intent.OPEN_TICKET: "a ticket for a person to pick up",
    Intent.ASK_POLICY: "an answer about the rules",
    Intent.CHECK_MY_ACCESS: "a look at your access and requests",
    Intent.ASK_CAPABILITIES: "what you can ask for here",
    Intent.APPROVALS: "the requests waiting for you",
    Intent.REVIEW: "an access review",
    Intent.FOLLOW_UP: "a change to my last reply",
    Intent.OTHER: "something else",
}
"""Every intent in plain words, for the line that names a second request."""


def second_request_line(intent: Intent) -> str:
    """The fixed line for a second request in the same message."""
    return (
        f"You also asked for {INTENT_NAMES[intent]}. That needs its own "
        "message, so send it next and I will take it then."
    )


def with_fixed_lines(reply: str, state: TurnState) -> str:
    """The reply with this turn's fixed lines: the skip-approval line first
    when the message asked to skip approval (`test_skip_line_comes_first`), and
    the second-request line last when it held two requests. Both belong to that
    turn only: a yes turn's message is the yes, and it runs on the stored
    extraction with no second request."""
    if skips_approval(state.user_message) and not reply.startswith(SKIP_LINE):
        reply = f"{SKIP_LINE} {reply}"
    extraction = state.extraction
    if extraction is None or extraction.second_request is None:
        return reply
    return f"{reply}\n\n{second_request_line(extraction.second_request)}"


def last_assistant_reply(history: Sequence[str]) -> str | None:
    """The text of the last assistant line of the history (`"{role}: {text}"`),
    or `None` when the assistant has not spoken yet."""
    for line in reversed(history):
        if line.startswith(ASSISTANT_PREFIX):
            return line[len(ASSISTANT_PREFIX) :]
    return None
