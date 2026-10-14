"""An earlier change §6: the decision-word post-check as a pure table (the design step 8).

Expected implementation surface (the implementer must provide exactly this):

``agent_api.graph.postcheck``
    ``DECISION_WORDS: frozenset[str]`` — exactly
        ``{"approved", "granted", "blocked", "pending"}``, 4 members.
    ``def words_in(text: str) -> frozenset[str]`` — a lowercase token scan
        (``re.findall(r"[a-z]+", text.lower())``) intersected with
        ``DECISION_WORDS``, so ``approvals``, ``ungranted`` and ``pendingly``
        cannot match by construction (decision C).
    ``def permitted_words(state: TurnState) -> frozenset[str]`` — pure, over
        the turn's SUCCESSFUL (``ok=True``) ``ToolResult`` payloads plus
        ``state.outcome`` (decision B). No I/O, no gateway, no database.
    ``def contradicts(reply: str, state: TurnState) -> bool`` — true when
        ``words_in(reply)`` is not a SUBSET of ``permitted_words(state)``.
    ``POSTCHECK_FALLBACK_REPLY: str`` — the template a twice-contradicting
        composition ends on.

``agent_api.graph.state``
    ``PostCheckOutcome`` — a ``StrEnum`` with exactly the 3 members
        ``passed``, ``regenerated``, ``fallback``.
    ``TurnState.postcheck: PostCheckOutcome`` — additive, defaulting to
        ``PostCheckOutcome.PASSED`` so no existing state construction moves.

Cost: nothing. No Postgres, no Redis, no gateway and no socket — the whole
file is a table over frozen Pydantic objects, the same shape `gates.py`'s
predicates are tested in.
"""

import uuid
from typing import Any

import pytest

from helpdesk.agent_api.graph.postcheck import (
    DECISION_WORDS,
    POSTCHECK_FALLBACK_REPLY,
    contradicts,
    permitted_words,
    words_in,
)
from helpdesk.agent_api.graph.state import PostCheckOutcome, ToolResult, TurnOutcome, TurnState
from helpdesk.shared.models.access import RequestStatus
from helpdesk.shared.models.conversation import MAX_MESSAGE_CHARS
from helpdesk.shared.policy.types import Decision

TENANT_ID = uuid.UUID("22222222-2222-5222-8222-222222222222")
USER_ID = uuid.UUID("33333333-3333-5333-8333-333333333333")
SESSION_ID = uuid.UUID("11111111-1111-5111-8111-111111111111")
GRANT_ID = uuid.UUID("44444444-4444-5444-8444-444444444444")

MESSAGE = "I need access to fake payroll"

APPROVED = "approved"
GRANTED = "granted"
BLOCKED = "blocked"
PENDING = "pending"


def _result(payload: dict[str, Any], *, ok: bool = True) -> ToolResult:
    """One executed call's answer, as `execute_tools` records it."""
    return ToolResult(tool="create_access_request", ok=ok, payload=payload)


def _state(
    *,
    tool_results: tuple[ToolResult, ...] = (),
    outcome: TurnOutcome = TurnOutcome.DONE,
) -> TurnState:
    """The smallest turn the post-check can read: results and an outcome."""
    return TurnState(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        session_id=SESSION_ID,
        tenant_slug="postcheck-tenant",
        user_message=MESSAGE,
        outcome=outcome,
        tool_results=tool_results,
    )


# --- criterion 4's vocabulary --------------------------------------------------


def test_decision_words_is_exactly_the_four_the_check_polices() -> None:
    """a project rule, on the set the whole plan is about: asserted by
    equality and not by length, so a fifth word or a misspelt one is caught.

    Catches a module that policed a wider vocabulary than the plan fixed —
    every extra word is a reply regenerated for prose the engine never
    contradicted — and one missing a word, which is a decision word that can
    never be caught at all."""
    assert frozenset({APPROVED, GRANTED, BLOCKED, PENDING}) == DECISION_WORDS
    assert len(DECISION_WORDS) == 4


# --- criterion 5: exact tokens, case-insensitive -------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("Approved.", frozenset({APPROVED}), id="trailing-punctuation"),
        pytest.param("APPROVED", frozenset({APPROVED}), id="upper-case"),
        pytest.param("approvals are open", frozenset(), id="longer-word-approvals"),
        pytest.param("ungranted", frozenset(), id="prefixed-word-ungranted"),
        pytest.param("pendingly", frozenset(), id="suffixed-word-pendingly"),
        pytest.param(
            "The request is pending and nothing was granted yet.",
            frozenset({PENDING, GRANTED}),
            id="two-words-in-one-sentence",
        ),
    ],
)
def test_words_in_matches_whole_tokens_case_insensitively(
    text: str, expected: frozenset[str]
) -> None:
    """Criterion 5, row by row.

    `trailing-punctuation` and `upper-case` catch a naive `word in text`
    scan's opposite — a check that compares the reply to the words verbatim
    and so misses `Approved.`, the single commonest way a model writes one.

    `longer-word-approvals`, `prefixed-word-ungranted` and
    `suffixed-word-pendingly` catch the substring scan itself: `in` finds
    `granted` inside `ungranted`, which would regenerate a reply that said the
    OPPOSITE of the word it was accused of, and burn the second gateway call
    doing it.

    `two-words-in-one-sentence` catches a scan that stops at the first hit;
    the second word is the one the turn may not have permitted."""
    assert words_in(text) == expected


# --- criterion 4: the permitted set, 11 rows -----------------------------------


@pytest.mark.parametrize(
    ("tool_results", "outcome", "expected"),
    [
        pytest.param(
            (_result({"decision": Decision.BLOCKED.value}),),
            TurnOutcome.DONE,
            frozenset({BLOCKED}),
            id="decision-blocked",
        ),
        pytest.param(
            (_result({"decision": Decision.APPROVAL.value}),),
            TurnOutcome.DONE,
            frozenset({PENDING}),
            id="decision-approval",
        ),
        pytest.param(
            (_result({"decision": Decision.AUTO.value}),),
            TurnOutcome.DONE,
            frozenset({APPROVED, GRANTED}),
            id="decision-auto",
        ),
        pytest.param(
            (_result({"status": RequestStatus.PENDING.value}),),
            TurnOutcome.DONE,
            frozenset({PENDING}),
            id="status-pending",
        ),
        pytest.param(
            (_result({"status": RequestStatus.APPROVED.value}),),
            TurnOutcome.DONE,
            frozenset({APPROVED}),
            id="status-approved",
        ),
        pytest.param(
            (_result({"status": RequestStatus.EXECUTED.value}),),
            TurnOutcome.DONE,
            frozenset({APPROVED, GRANTED}),
            id="status-executed",
        ),
        pytest.param(
            (_result({"status": RequestStatus.REJECTED.value}),),
            TurnOutcome.DONE,
            frozenset(),
            id="status-rejected",
        ),
        pytest.param(
            (_result({"status": RequestStatus.FAILED.value}),),
            TurnOutcome.DONE,
            frozenset(),
            id="status-failed",
        ),
        pytest.param(
            (_result({"grant_id": str(GRANT_ID)}),),
            TurnOutcome.DONE,
            frozenset({GRANTED}),
            id="payload-carries-a-grant-id",
        ),
        pytest.param(
            (),
            TurnOutcome.AWAIT_APPROVAL,
            frozenset({PENDING}),
            id="outcome-await-approval",
        ),
        pytest.param((), TurnOutcome.DONE, frozenset(), id="no-tool-results-at-all"),
    ],
)
def test_permitted_words_is_derived_row_by_row_from_what_the_turn_returned(
    tool_results: tuple[ToolResult, ...], outcome: TurnOutcome, expected: frozenset[str]
) -> None:
    """Criterion 4's whole decision table, one row per case.

    The `decision-*` rows catch a derivation keyed only on `status`, and the
    `status-*` rows catch one keyed only on `decision`: either half alone
    leaves the tools the other half describes permitting nothing, so every
    reply about them would be regenerated and then fall back to the template.

    `status-rejected`, `status-failed` and `no-tool-results-at-all` are the
    rows that catch the failure mode of §5 — a mapping generous enough that
    the check can never fire. All three permit NOTHING: a derivation that
    defaulted to `DECISION_WORDS` for an unrecognised status, or that returned
    everything when the turn ran no tool, is red here and nowhere else.

    `payload-carries-a-grant-id` catches a derivation that reads only the two
    named keys, which would regenerate the one reply that is certainly true —
    the grant exists and the payload carries its id.

    `outcome-await-approval` catches a derivation reading payloads only: a
    paused turn's request row is not in `tool_results` by the time the reply
    is composed, so `pending` would be forbidden on the exact turn whose whole
    content is that something is pending."""
    assert permitted_words(_state(tool_results=tool_results, outcome=outcome)) == expected


def test_a_failed_tool_result_permits_nothing_even_when_it_carries_a_decision() -> None:
    """The §2 case: only `ok=True` results contribute.

    The payload is `decision="auto"`, which the `decision-auto` row above
    proves permits `{approved, granted}` on a SUCCESSFUL result — so the only
    difference between the two cases is the `ok` flag, and this row is red
    precisely when the derivation reads every payload it can see rather than
    the successful ones.

    That is the sentence the post-check exists to catch: a failed
    `create_access_request` licensing the model to write that access was
    granted."""
    state = _state(tool_results=(_result({"decision": Decision.AUTO.value}, ok=False),))

    assert permitted_words(state) == frozenset()


# --- criterion 2: the reply judged against the set -----------------------------


@pytest.mark.parametrize(
    ("reply", "tool_results", "expected"),
    [
        pytest.param(
            "Your fake payroll access is with your manager now.",
            (),
            False,
            id="no-decision-word-against-an-empty-permit",
        ),
        pytest.param(
            "Your access was approved.",
            (),
            True,
            id="a-word-a-turn-that-ran-nothing-cannot-claim",
        ),
        pytest.param(
            "Your access was approved.",
            (_result({"status": RequestStatus.APPROVED.value}),),
            False,
            id="the-word-the-turn-returned",
        ),
        pytest.param(
            "Your access was approved and granted.",
            (_result({"status": RequestStatus.APPROVED.value}),),
            True,
            id="one-permitted-word-and-one-that-is-not",
        ),
    ],
)
def test_contradicts_is_a_subset_test_and_not_an_overlap_test(
    reply: str, tool_results: tuple[ToolResult, ...], expected: bool
) -> None:
    """Criterion 2's predicate.

    `one-permitted-word-and-one-that-is-not` is the row that names the kill:
    an implementation asking whether ANY word in the reply is permitted (a
    non-empty intersection) answers `False` here, because `approved` is
    permitted, and ships the sentence claiming the access was also granted.
    Only a subset test flips this row, and no other row in the table can.

    `a-word-a-turn-that-ran-nothing-cannot-claim` catches a predicate that
    treats an empty permitted set as "no information, allow anything", which
    is criterion 4's empty case read the wrong way round;
    `no-decision-word-against-an-empty-permit` catches the mirror image, a
    predicate that contradicts every reply once nothing is permitted and so
    sends every toolless turn to the template."""
    assert contradicts(reply, _state(tool_results=tool_results)) is expected


# --- §5's own risk: the template that answers the check ------------------------


def test_the_fallback_reply_is_storable_and_claims_no_decision_at_all() -> None:
    """§5's first risk. Catches a template that itself contains a decision
    word — it would fail the very check it is the answer to, and it is the one
    string in this plan no model wrote and nobody would re-read — and one
    longer than the `messages.content` column, which would raise inside the
    turn's transaction on the path taken precisely because everything else
    already went wrong.

    The empty check is not decoration: an empty template passes the word scan
    trivially and would store a blank assistant row as the turn's answer."""
    assert words_in(POSTCHECK_FALLBACK_REPLY) == frozenset()
    assert POSTCHECK_FALLBACK_REPLY.strip() != ""
    assert len(POSTCHECK_FALLBACK_REPLY) < MAX_MESSAGE_CHARS
    assert contradicts(POSTCHECK_FALLBACK_REPLY, _state()) is False


# --- criterion 7: what the composition did, on the state -----------------------


def test_post_check_outcome_has_exactly_the_three_members_by_value() -> None:
    """a project rule on an enum whose VALUE crosses the checkpointer's serde
    (decision E). Asserted by value and not by name, because a checkpoint
    written by one process is read back by another.

    Catches a fourth member added without a plan — row 20 counts fallbacks,
    and a member it has no row for is a turn nobody can classify — and a
    member spelled `regenerate` or `fell_back`, which reads fine in code and
    breaks every stored state."""
    assert {member.value for member in PostCheckOutcome} == {"passed", "regenerated", "fallback"}
    assert len(PostCheckOutcome) == 3


def test_a_turn_records_a_passed_post_check_until_something_says_otherwise() -> None:
    """Decision E's additive field. Catches a field added WITHOUT a default,
    which makes every existing `TurnState(...)` in this repo a validation
    error, and one defaulting to `fallback` or `regenerated`, which would
    report a fallback for every turn that never reached composition and make
    row 20's count meaningless from the first day."""
    assert _state().postcheck is PostCheckOutcome.PASSED
