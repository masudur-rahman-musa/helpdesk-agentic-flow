"""The decision-word post-check as a pure table (step 8).

Expected implementation surface (the implementer must provide exactly this):

``agent_api.graph.postcheck``
    ``DECISION_WORDS: frozenset[str]`` — exactly
        ``{"approved", "granted", "blocked", "pending"}``, 4 members.
    ``def words_in(text: str) -> frozenset[str]`` — a lowercase token scan
        (``re.findall(r"[a-z]+", text.lower())``) intersected with
        ``DECISION_WORDS``, so ``approvals``, ``ungranted`` and ``pendingly``
        cannot match by construction.
    ``def permitted_words(state: TurnState) -> frozenset[str]`` — pure, over
        the turn's SUCCESSFUL (``ok=True``) ``ToolResult`` payloads plus
        ``state.outcome``. No I/O, no gateway, no database.
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

from helpdesk.agent_api.agent.postcheck import (
    DECISION_WORDS,
    POSTCHECK_FALLBACK_REPLY,
    asks_after_filing,
    contradicts,
    outcome_reply,
    permitted_words,
    request_filed,
    words_in,
)
from helpdesk.agent_api.agent.state import PostCheckOutcome, ToolResult, TurnOutcome, TurnState
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


# --- the permitted vocabulary --------------------------------------------------


def test_decision_words_is_exactly_the_four_the_check_polices() -> None:
    """On the set the check is about: asserted by
    equality and not by length, so a fifth word or a misspelt one is caught.

    Catches a module that policed a wider vocabulary than the plan fixed —
    every extra word is a reply regenerated for prose the engine never
    contradicted — and one missing a word, which is a decision word that can
    never be caught at all."""
    assert frozenset({APPROVED, GRANTED, BLOCKED, PENDING}) == DECISION_WORDS
    assert len(DECISION_WORDS) == 4


# --- exact tokens, case-insensitive -------------------------------


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
    """Row by row.

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


# --- the permitted set, 11 rows -----------------------------------


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
    """The whole decision table, one row per case.

    The `decision-*` rows catch a derivation keyed only on `status`, and the
    `status-*` rows catch one keyed only on `decision`: either half alone
    leaves the tools the other half describes permitting nothing, so every
    reply about them would be regenerated and then fall back to the template.

    `status-rejected`, `status-failed` and `no-tool-results-at-all` are the
    rows that catch the failure mode of a mapping generous enough that
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
    """Only `ok=True` results contribute.

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


# --- the reply judged against the set -----------------------------


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
    """The predicate.

    `one-permitted-word-and-one-that-is-not` is the row that names the kill:
    an implementation asking whether ANY word in the reply is permitted (a
    non-empty intersection) answers `False` here, because `approved` is
    permitted, and ships the sentence claiming the access was also granted.
    Only a subset test flips this row, and no other row in the table can.

    `a-word-a-turn-that-ran-nothing-cannot-claim` catches a predicate that
    treats an empty permitted set as "no information, allow anything", which
    is the empty case read the wrong way round;
    `no-decision-word-against-an-empty-permit` catches the mirror image, a
    predicate that contradicts every reply once nothing is permitted and so
    sends every toolless turn to the template."""
    assert contradicts(reply, _state(tool_results=tool_results)) is expected


# --- the template that answers the check ------------------------


def test_the_fallback_reply_is_storable_and_claims_no_decision_at_all() -> None:
    """Catches a template that itself contains a decision
    word — it would fail the very check it is the answer to, and it is the one
    string here no model wrote and nobody would re-read — and one
    longer than the `messages.content` column, which would raise inside the
    turn's transaction on the path taken precisely because everything else
    already went wrong.

    The empty check is not decoration: an empty template passes the word scan
    trivially and would store a blank assistant row as the turn's answer."""
    assert words_in(POSTCHECK_FALLBACK_REPLY) == frozenset()
    assert POSTCHECK_FALLBACK_REPLY.strip() != ""
    assert len(POSTCHECK_FALLBACK_REPLY) < MAX_MESSAGE_CHARS
    assert contradicts(POSTCHECK_FALLBACK_REPLY, _state()) is False


# --- what the composition did, on the state -----------------------


def test_post_check_outcome_has_exactly_the_three_members_by_value() -> None:
    """On an enum whose VALUE crosses the checkpointer's serde.
    Asserted by value and not by name, because a checkpoint
        written by one process is read back by another.

        Catches a fourth member added without a matching metric — the metrics count fallbacks,
        and a member it has no row for is a turn nobody can classify — and a
        member spelled `regenerate` or `fell_back`, which reads fine in code and
        breaks every stored state."""
    assert {member.value for member in PostCheckOutcome} == {"passed", "regenerated", "fallback"}
    assert len(PostCheckOutcome) == 3


def test_a_turn_records_a_passed_post_check_until_something_says_otherwise() -> None:
    """The additive field. Catches a field added WITHOUT a default,
    which makes every existing `TurnState(...)` in this repo a validation
    error, and one defaulting to `fallback` or `regenerated`, which would
    report a fallback for every turn that never reached composition and make
    the fallback count meaningless from the first day."""
    assert _state().postcheck is PostCheckOutcome.PASSED


# --- a question after the request was already filed ----------------------------


@pytest.mark.parametrize(
    ("reply", "tool_results", "expected"),
    [
        pytest.param(
            "I need one more detail. How long do you need this access for?",
            (_result({"id": "r1", "status": RequestStatus.PENDING.value}),),
            True,
            id="question-after-a-pending-request",
        ),
        pytest.param(
            "Done. Do you need anything else?",
            (_result({"decision": Decision.AUTO.value}),),
            False,
            id="question-after-an-auto-decision",
        ),
        pytest.param(
            "Your request is with your manager.",
            (_result({"id": "r1", "status": RequestStatus.PENDING.value}),),
            False,
            id="statement-after-a-pending-request",
        ),
        pytest.param(
            "How long do you need this access for?",
            (),
            False,
            id="question-when-nothing-was-filed",
        ),
        pytest.param(
            "Should I open a ticket instead?",
            (_result({"decision": Decision.BLOCKED.value}),),
            False,
            id="question-after-a-block-is-allowed",
        ),
        pytest.param(
            "How long do you need this access for?",
            (_result({"id": "r1", "status": RequestStatus.PENDING.value}, ok=False),),
            False,
            id="failed-call-filed-nothing",
        ),
    ],
)
def test_a_reply_may_not_ask_a_question_once_the_request_is_filed(
    reply: str, tool_results: tuple[ToolResult, ...], expected: bool
) -> None:
    """Catches the reply that says "before I can submit this, how long?" over
    a card that shows the request already filed. The question reads as if the
    person's answer still mattered, and it does not. A blocked turn and a turn
    that filed nothing may still ask, so the rule cannot be a bare `?` check."""
    assert asks_after_filing(reply, _state(tool_results=tool_results)) is expected


# --- a classification is not a filing ------------------------------------------


def _classify(payload: dict[str, Any], *, ok: bool = True) -> ToolResult:
    """The answer of `classify_access_request`: a decision, nothing filed."""
    return ToolResult(tool="classify_access_request", ok=ok, payload=payload)


SEATS_MANAGER_AND_ADMIN = [
    {"slot": "department_manager", "user_id": None},
    {"slot": "it_admin", "user_id": None},
]


@pytest.mark.parametrize("decision", [Decision.APPROVAL.value, Decision.AUTO.value])
def test_a_classification_alone_is_not_a_filed_request(decision: str) -> None:
    """Catches a `request_filed` that counts a bare `decision` as a filing: the
    model's natural "Shall I file it for you?" would then be refused twice and
    the turn would end on the fixed fallback text."""
    state = _state(
        tool_results=(
            _classify(
                {
                    "decision": decision,
                    "required_approvers": SEATS_MANAGER_AND_ADMIN,
                    "effective_expiry_days": 30,
                }
            ),
        )
    )

    assert request_filed(state) is False
    assert asks_after_filing("Shall I file it for you?", state) is False


# --- the last reply, built from the turn's own results -------------------------

_PENDING_FILED = _result(
    {
        "request_id": "r1",
        "status": RequestStatus.PENDING.value,
        "decision": Decision.APPROVAL.value,
        "required_approvers": SEATS_MANAGER_AND_ADMIN,
        "effective_expiry_days": 30,
    }
)
_GRANTED_FILED = _result(
    {
        "request_id": "r2",
        "status": RequestStatus.EXECUTED.value,
        "decision": Decision.AUTO.value,
        "grant_id": str(GRANT_ID),
        "effective_expiry_days": 14,
    }
)
_BLOCKED = _classify({"decision": Decision.BLOCKED.value, "required_approvers": []})
_CLASSIFIED_ONLY = _classify(
    {
        "decision": Decision.APPROVAL.value,
        "required_approvers": SEATS_MANAGER_AND_ADMIN,
        "effective_expiry_days": 30,
    }
)


def _reply_rows() -> list[Any]:
    return [
        pytest.param((_PENDING_FILED,), id="pending"),
        pytest.param((_GRANTED_FILED,), id="granted"),
        pytest.param((_BLOCKED,), id="blocked"),
        pytest.param((_CLASSIFIED_ONLY,), id="classified-only"),
        pytest.param((), id="no-results"),
    ]


def test_the_last_reply_names_what_the_turn_did() -> None:
    """Catches a last reply that stays generic (or claims the wrong outcome):
    the chat would disagree with the decision card under it."""
    pending = outcome_reply(_state(tool_results=(_PENDING_FILED,)))
    assert "Department manager" in pending
    assert "IT admin" in pending
    assert "approval" in pending.lower()
    assert "not filed" not in pending.lower()

    granted = outcome_reply(_state(tool_results=(_GRANTED_FILED,)))
    assert "granted" in granted.lower()
    assert "approval" not in granted.lower()

    blocked = outcome_reply(_state(tool_results=(_BLOCKED,)))
    assert "blocked" in blocked.lower()
    assert "nothing was filed" in blocked.lower()

    classified = outcome_reply(_state(tool_results=(_CLASSIFIED_ONLY,)))
    assert "not filed yet" in classified.lower() or "nothing is filed yet" in classified.lower()
    assert "Department manager" in classified
    assert "IT admin" in classified
    assert "granted" not in classified.lower()

    assert outcome_reply(_state()) == POSTCHECK_FALLBACK_REPLY


@pytest.mark.parametrize("tool_results", _reply_rows())
def test_every_last_reply_passes_its_own_check(tool_results: tuple[ToolResult, ...]) -> None:
    """Catches a last reply that uses a decision word the turn did not earn,
    asks a question, is empty, or is too long to store: it would fail the very
    check it answers, on the path taken when everything else went wrong."""
    state = _state(tool_results=tool_results)
    reply = outcome_reply(state)

    assert contradicts(reply, state) is False
    assert asks_after_filing(reply, state) is False
    assert "?" not in reply
    assert reply.strip() != ""
    assert len(reply) < MAX_MESSAGE_CHARS


def test_the_last_reply_ignores_failed_results() -> None:
    """Catches a last reply that reads a failed call's payload: it would tell the
    person their request waits for approval when the call never succeeded."""
    state = _state(
        tool_results=(
            _result(
                {"request_id": "r1", "status": RequestStatus.PENDING.value},
                ok=False,
            ),
        )
    )

    reply = outcome_reply(state)

    assert reply == POSTCHECK_FALLBACK_REPLY
    assert "approval" not in reply.lower()
