"""The decision-word post-check as a pure table (the design step 8).

A composed reply may not contradict what the engine decided. This module says
what "contradict" means, with no gateway, no database and no graph in it — the
same separation `gates.py` draws between a predicate and the node that acts on
it, so the whole table below is tested as a table
(`test_agent_postcheck.py`).

**The permitted set is derived from what the turn RETURNED, not from one
decision** (decision B). A turn can run several tools, so there is nothing for
an equality check to compare against; a set built from every successful result
answers the question either way, and the EMPTY set falls out for free — a turn
that ran no tool permits no decision word at all, which is what stops a reply
claiming an outcome nothing produced. Pinned row by row by
`::test_permitted_words_is_derived_row_by_row_from_what_the_turn_returned`.

**Only `ok=True` results contribute.** A call that failed decided nothing, and
a failed `create_access_request` permitting "granted" would license the exact
sentence this module exists to catch. Pinned by
`::test_a_failed_tool_result_permits_nothing_even_when_it_carries_a_decision`.

**An unrecognised value permits nothing.** Every lookup below reads through
`.get` with an empty default, so a status this table has no row for — and a
tool a later plan adds — narrows the reply rather than widening it. That is
also why `rejected` and `failed` have explicit empty rows rather than being
left out: the row states the answer instead of relying on the default.

**Matching is a whole-token scan, case-insensitive** (decision C). One pass
over `[a-z]+` tokens, so `approvals`, `ungranted` and `pendingly` cannot match
by construction rather than by getting four regex boundaries right. Pinned by
`::test_words_in_matches_whole_tokens_case_insensitively`.
"""

import re
from collections.abc import Mapping

from helpdesk.agent_api.agent.state import TurnOutcome, TurnState
from helpdesk.shared.models.access import RequestStatus
from helpdesk.shared.policy.types import Decision

DECISION_WORDS = frozenset({"approved", "granted", "blocked", "pending"})
"""The four words a reply may not use unless the turn returned them.

Exactly four, pinned by equality rather than by length
(`::test_decision_words_is_exactly_the_four_the_check_polices`). A fifth word
regenerates replies the engine never contradicted; a missing one is a decision
word that can never be caught.
"""

_TOKENS = re.compile(r"[a-z]+")

_DECISION_PERMITS: Mapping[str, frozenset[str]] = {
    Decision.AUTO.value: frozenset({"approved", "granted"}),
    Decision.APPROVAL.value: frozenset({"pending"}),
    Decision.BLOCKED.value: frozenset({"blocked"}),
}
"""What each policy-engine decision licenses a reply to say.

`auto` permits both words because an auto-approved request is executed in the
same call: the grant exists, so "approved" and "granted" are each true of it.
"""

_STATUS_PERMITS: Mapping[str, frozenset[str]] = {
    RequestStatus.PENDING.value: frozenset({"pending"}),
    RequestStatus.APPROVED.value: frozenset({"approved"}),
    RequestStatus.EXECUTED.value: frozenset({"approved", "granted"}),
    RequestStatus.REJECTED.value: frozenset(),
    RequestStatus.FAILED.value: frozenset(),
}
"""What each request status licenses. `approved` is deliberately NOT `granted`:
an approved request whose execution has not run yet has no grant behind it, and
that is the one gap between the two words a reader would never notice.

`RequestStatus` also carries `rolled_back` and `expired`, which have no row and
so permit nothing: a reply about either is regenerated and, failing that,
templated, which is the fail-closed side to be wrong on.
"""

POSTCHECK_FALLBACK_REPLY = (
    "Something went wrong while putting this answer into words, so I would "
    "rather say nothing about the outcome than describe it wrongly. Please "
    "open the requests page to see where this stands, or ask the service desk "
    "to look at it with you."
)
"""What a twice-contradicting composition ends on.

It claims no outcome at all, which is the only safe thing to say when the
prose about the turn could not be trusted twice running. It carries none of
`DECISION_WORDS` — it would otherwise fail the check it is the answer to — and
is well under `MAX_MESSAGE_CHARS`, both pinned by
`::test_the_fallback_reply_is_storable_and_claims_no_decision_at_all`.
"""


def words_in(text: str) -> frozenset[str]:
    """The decision words `text` actually uses, as whole lowercase tokens."""
    return DECISION_WORDS & frozenset(_TOKENS.findall(text.lower()))


def permitted_words(state: TurnState) -> frozenset[str]:
    """The decision words this turn's own results entitle a reply to use."""
    permitted: set[str] = set()

    if state.outcome is TurnOutcome.AWAIT_APPROVAL:
        permitted |= {"pending"}

    for result in state.tool_results:
        if not result.ok:
            continue
        decision = result.payload.get("decision")
        if isinstance(decision, str):
            permitted |= _DECISION_PERMITS.get(decision, frozenset())
        status = result.payload.get("status")
        if isinstance(status, str):
            permitted |= _STATUS_PERMITS.get(status, frozenset())
        if result.payload.get("grant_id") is not None:
            permitted |= {"granted"}

    return frozenset(permitted)


def contradicts(reply: str, state: TurnState) -> bool:
    """Whether `reply` claims a decision word the turn did not return.

    A SUBSET test and not an overlap test: a reply saying the access was
    "approved and granted" over a turn that only approved it contradicts,
    even though one of its two words is permitted. Pinned by
    `::test_contradicts_is_a_subset_test_and_not_an_overlap_test`.
    """
    return not words_in(reply) <= permitted_words(state)
