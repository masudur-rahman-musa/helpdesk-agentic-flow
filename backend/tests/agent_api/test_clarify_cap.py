"""The one-clarifying-question cap as pure tables.

Expected implementation surface: ``gates.clarify_outcome(extraction,
asked_before)``, ``state.TurnOutcome.ANSWER``, and ``clarify_cap`` holding
``parse_asked_at``, ``CLARIFY_CAP_LIFE``, ``BEST_READING_FALLBACK`` and
``CORRECT_ME_LINE``, plus ``postcheck.asks_on_a_best_reading``.

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
    parse_asked_at,
)
from helpdesk.agent_api.agent.gates import clarify_outcome
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
    """Catches a check that reads only the "?" (a normal turn's question refused),
    only the flag (a statement refused), or never the flag."""
    assert asks_on_a_best_reading("Did you mean the wiki?", _state(best_reading=True)) is True
    assert asks_on_a_best_reading("Did you mean the wiki?", _state(best_reading=False)) is False
    assert asks_on_a_best_reading("Here is what I found.", _state(best_reading=True)) is False
