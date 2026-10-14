"""One plain sentence for each of the design's eight error codes (17d §1).

What a tool failure sounds like to the person who asked. The composition is
handed THESE words and never the exception's own text or the server's message:
both carry a host, a path, a status code and the tool that raised, and this
string travels into a prompt and from there into a chat window. Pinned row by
row by
`test_agent_execution.py::test_every_plain_sentence_is_this_services_own_words`,
which sweeps every sentence for a scheme, a host, a three-digit number, a
catalogued tool name and a snake_case argument name.

A row for EVERY member of `ErrorCode`, read through no `.get()` default, for
the reason `mcp_client.REMOTE_ERRORS` and `gates.INTENT_TOOLS` have one: a code
with no row would fall through to a generic sentence, and a `SOD_CONFLICT` the
user could resolve by asking for a different role would be answered with
"something went wrong". The next code added to the design raises a `KeyError` in
a test rather than reaching a user as a shrug. Pinned by
`::test_plain_errors_covers_exactly_the_eight_error_codes` and, as eight
DIFFERENT sentences, by `::test_the_eight_sentences_are_eight_different_sentences`
— a table whose rows all said the same thing would satisfy every other
assertion and still answer nothing anyone can act on.

Six of the eight are decisions a person can do something about, which is
exactly why an earlier change decision B composes over them rather than ending the turn.
The sentences say what the person's next move is where there is one.
"""

from collections.abc import Mapping
from types import MappingProxyType

from helpdesk.shared.errors import ErrorCode

PLAIN_ERRORS: Mapping[ErrorCode, str] = MappingProxyType(
    {
        ErrorCode.FORBIDDEN: (
            "You do not have permission to do that, so I did not go ahead. "
            "Your manager or an administrator can do it for you."
        ),
        ErrorCode.NOT_FOUND: (
            "I could not find what that refers to in your organisation. "
            "It may have been renamed or removed, so it is worth checking the name."
        ),
        ErrorCode.SOD_CONFLICT: (
            "That combination of access is not allowed to be held by one person, "
            "so it was blocked. Asking for a narrower role usually resolves it."
        ),
        ErrorCode.POLICY_BLOCKED: (
            "A policy rule blocked this, so nothing was changed. "
            "A person on the helpdesk team can review it with you if you think it is wrong."
        ),
        ErrorCode.IDEMPOTENCY_CONFLICT: (
            "This looks like a repeat of something already in progress, "
            "so I stopped rather than doing it twice. Check the original before asking again."
        ),
        ErrorCode.TOOL_DISABLED: (
            "That action is switched off at the moment, so nothing was changed. "
            "It is usually turned back on shortly."
        ),
        ErrorCode.VALIDATION: (
            "Some of the details were not in a form I could use, so I stopped. "
            "Saying it again with the system and the access you need should fix it."
        ),
        ErrorCode.INTERNAL: (
            "Something on our side went wrong and I could not finish that. "
            "Nothing was changed as far as I can tell, and trying again shortly may work."
        ),
    }
)
