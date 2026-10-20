"""The one-clarifying-question cap as pure tables.

Expected implementation surface: ``gates.clarify_outcome(extraction,
asked_before)``, ``state.TurnOutcome.ANSWER``, and ``clarify_cap`` holding
``parse_asked_at``, ``CLARIFY_CAP_LIFE``, ``BEST_READING_FALLBACK`` and
``CORRECT_ME_LINE``, plus ``postcheck.asks_on_a_best_reading``.

The code adds ``gates.REQUIRED_ENTITIES[REQUEST_ACCESS] == {"system",
"justification"}`` and ``clarify_cap.details_question(missing, *, length_missing)``:
``missing`` is the set from ``gates.missing_required_entities`` and
``length_missing`` is True when ``expiry_days`` is not given. It returns the
code-worded sentence. ``gates.clarify_outcome`` asks (CLARIFY) a confident
``request_access`` that lacks the length only, when not asked before.

Cost: nothing. No database, no gateway, no socket.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from helpdesk.agent_api.agent.clarify_cap import (
    BEST_READING_FALLBACK,
    CLARIFY_CAP_LIFE,
    CORRECT_ME_LINE,
    details_question,
    parse_asked_at,
)
from helpdesk.agent_api.agent.gates import (
    REQUIRED_ENTITIES,
    clarify_outcome,
    missing_required_entities,
)
from helpdesk.agent_api.agent.postcheck import asks_on_a_best_reading
from helpdesk.agent_api.agent.state import (
    Intent,
    IntentEntities,
    IntentExtraction,
    TurnOutcome,
    TurnState,
)

NOW = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)


def _extraction(intent: Intent, *, confidence: float, **entities: Any) -> IntentExtraction:
    return IntentExtraction(
        intent=intent,
        entities=IntentEntities(**{**dict.fromkeys(IntentEntities.model_fields), **entities}),
        confidence=confidence,
        clarifying_question=None,
        second_request=None,
    )


# --- the gate -------------------------------------------------------------------


@pytest.mark.parametrize("asked_before", [False, True])
def test_not_needed_continues(asked_before: bool) -> None:
    """Catches a gate that clarifies or answers a clear message just because a
    question came before it."""
    extraction = _extraction(Intent.ASK_POLICY, confidence=0.9)

    assert clarify_outcome(extraction, asked_before) is TurnOutcome.CONTINUE


def test_first_unclear_message_clarifies() -> None:
    """Catches a cap that removed the question altogether: with no question asked
    before, a low-confidence message must still be asked about."""
    extraction = _extraction(Intent.ASK_POLICY, confidence=0.5)

    assert clarify_outcome(extraction, False) is TurnOutcome.CLARIFY


def test_second_unclear_message_goes_on() -> None:
    """Catches a gate that asks a second question in a row (the loop of the
    screenshot): low confidence, nothing missing, asked before must go on."""
    extraction = _extraction(Intent.ASK_POLICY, confidence=0.5)

    assert clarify_outcome(extraction, True) is TurnOutcome.CONTINUE


@pytest.mark.parametrize("confidence", [0.9, 0.5])
def test_second_message_missing_a_required_entity_answers(confidence: float) -> None:
    """Catches a gate that lets a request with no system on into the tools after a
    question (it would plan on a guess), or one that asks again. The same message
    asked about for the first time must clarify, so only `asked_before` flips it."""
    extraction = _extraction(Intent.REQUEST_ACCESS, confidence=confidence)

    assert clarify_outcome(extraction, True) is TurnOutcome.ANSWER
    assert clarify_outcome(extraction, False) is TurnOutcome.CLARIFY


# --- the details a request needs ------------------------------------------------


def test_a_request_with_no_reason_and_no_length_asks_once() -> None:
    """Catches a gate that lets a confident request with only a system go on to the
    tools (the reason would be asked after the yes); the same message asked before
    must not ask again, so only the missing reason and length flip the first row."""
    extraction = _extraction(Intent.REQUEST_ACCESS, confidence=0.9, system="wiki")

    assert clarify_outcome(extraction, False) is TurnOutcome.CLARIFY


def test_a_request_missing_only_the_length_asks() -> None:
    """Catches a gate that never asks for the length because it does not block: with
    the reason given and no length, a first message must still get the question."""
    extraction = _extraction(
        Intent.REQUEST_ACCESS, confidence=0.9, system="wiki", justification="Fake reason."
    )

    assert clarify_outcome(extraction, False) is TurnOutcome.CLARIFY


@pytest.mark.parametrize("asked_before", [False, True])
def test_a_request_with_a_reason_and_a_length_goes_on(asked_before: bool) -> None:
    """Catches a gate that asks anyway once both details are in (or asks because a
    question came before)."""
    extraction = _extraction(
        Intent.REQUEST_ACCESS,
        confidence=0.9,
        system="wiki",
        justification="Fake reason.",
        expiry_days=14,
    )

    assert clarify_outcome(extraction, asked_before) is TurnOutcome.CONTINUE


def test_after_the_question_a_missing_length_goes_on() -> None:
    """Catches a length that blocks: after the one question, a reason with no
    length must go on (the role's default applies), not answer or ask again."""
    extraction = _extraction(
        Intent.REQUEST_ACCESS, confidence=0.9, system="wiki", justification="Fake reason."
    )

    assert clarify_outcome(extraction, True) is TurnOutcome.CONTINUE


def test_after_the_question_a_missing_reason_files_nothing() -> None:
    """Catches a gate that goes on to file with no reason after the question: the
    outcome must be ANSWER (no tools), and a length alone must not rescue it."""
    extraction = _extraction(Intent.REQUEST_ACCESS, confidence=0.9, system="wiki", expiry_days=14)

    assert clarify_outcome(extraction, True) is TurnOutcome.ANSWER


@pytest.mark.parametrize("blank", ["", " ", "\n", "\t \r\n"])
def test_a_blank_reason_counts_as_missing(blank: str) -> None:
    """Catches a reason check that accepts blank text, which would file a request
    with an empty business reason."""
    extraction = _extraction(
        Intent.REQUEST_ACCESS, confidence=0.9, system="wiki", justification=blank
    )

    assert "justification" in missing_required_entities(extraction)


def test_the_request_access_row_requires_the_system_and_the_reason() -> None:
    """Catches a row that still requires the system only, or that adds the length
    (which must never block): the set is exactly these two names."""
    assert REQUIRED_ENTITIES[Intent.REQUEST_ACCESS] == {"system", "justification"}


def test_revoke_access_needs_no_reason() -> None:
    """Catches a reason requirement spread to every access intent: a revoke with a
    system and no reason must go on."""
    extraction = _extraction(Intent.REVOKE_ACCESS, confidence=0.9, system="wiki")

    assert clarify_outcome(extraction, False) is TurnOutcome.CONTINUE


@pytest.mark.parametrize(
    ("missing", "length_missing", "expected"),
    [
        (
            {"system", "justification"},
            True,
            "Before I check this, tell me which system you need, how long you need it "
            "for and the business reason.",
        ),
        (
            {"justification"},
            True,
            "Before I check this, tell me how long you need it for and the business reason.",
        ),
        ({"justification"}, False, "Before I check this, tell me the business reason."),
        (set(), True, "Before I check this, tell me how long you need it for."),
    ],
)
def test_the_details_question_names_every_missing_detail_in_order(
    missing: set[str], length_missing: bool, expected: str
) -> None:
    """Catches a question that drops a missing detail, orders them other than system,
    length, reason, or joins them without "a, b and c"; the exact sentence is pinned."""
    assert details_question(frozenset(missing), length_missing=length_missing) == expected


# --- the stored time ------------------------------------------------------------


def test_the_cap_life_is_24_hours() -> None:
    """Catches a life of another length: the plan and ADR fix it at 24 hours."""
    assert CLARIFY_CAP_LIFE.total_seconds() == 24 * 60 * 60


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (timedelta(0), True),
        (CLARIFY_CAP_LIFE - timedelta(seconds=1), True),
        (CLARIFY_CAP_LIFE, False),
        (CLARIFY_CAP_LIFE + timedelta(seconds=1), False),
    ],
)
def test_a_stored_time_at_24_hours_is_none_and_just_under_is_kept(
    age: timedelta, expected: bool
) -> None:
    """Catches an age check off by one: `>` keeps a time at exactly 24 hours, and a
    shorter life drops one at 24 hours minus a second."""
    assert parse_asked_at((NOW - age).isoformat(), now=NOW) is expected


@pytest.mark.parametrize("raw", ["x", "", "2026-13-45T99:00:00", 5, {}, [], None])
def test_a_bad_stored_time_counts_as_none(raw: object) -> None:
    """Catches a parser that trusts the metadata: a string that is not a time, a
    number or a mapping must not raise and must not count as a question asked."""
    assert parse_asked_at(raw, now=NOW) is False


# --- the fixed lines ------------------------------------------------------------


def test_the_fixed_lines_are_exactly_the_planned_words() -> None:
    """Catches reworded fixed text: the golden set and the owner's screen rely on
    these exact sentences."""
    assert CORRECT_ME_LINE == "If I read that wrong, tell me what you meant."
    assert BEST_READING_FALLBACK == (
        "I could not pin down what you need yet. Tell me the system or the task "
        "in a few words and I will take it from there."
    )
    assert "?" not in BEST_READING_FALLBACK
    assert "?" not in CORRECT_ME_LINE


# --- the post-check -------------------------------------------------------------


def _state(*, best_reading: bool) -> TurnState:
    return TurnState(
        tenant_id=uuid.UUID(int=1),
        user_id=uuid.UUID(int=2),
        session_id=uuid.UUID(int=3),
        tenant_slug="cap-tenant",
        user_message="hello",
    ).model_copy(update={"best_reading": best_reading})


def test_asks_on_a_best_reading() -> None:
    """Catches a check that reads only the writer's mark (a normal turn's question
    refused), only the flag (a statement refused), or never the flag. Takes the
    mark, not text."""
    assert asks_on_a_best_reading(True, _state(best_reading=True)) is True
    assert asks_on_a_best_reading(True, _state(best_reading=False)) is False
    assert asks_on_a_best_reading(False, _state(best_reading=True)) is False
