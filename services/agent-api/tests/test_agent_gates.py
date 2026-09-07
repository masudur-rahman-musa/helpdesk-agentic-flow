"""An earlier change §6: the two turn gates, as pure predicates with no graph at all.

Expected implementation surface (the implementer must provide exactly this):

``agent_api.graph.state``
    ``class Intent(StrEnum)`` — exactly the design's thirteen labels.
    ``class IntentEntities(BaseModel)`` (frozen) — ``system``, ``role``,
        ``target_user``, ``department``, ``expiry_days``, ``justification``,
        every one optional and defaulting to ``None``.
    ``class IntentExtraction(BaseModel)`` (frozen) — ``intent``, ``entities``,
        ``confidence``, ``clarifying_question: str | None = None``.
    ``class TurnOutcome(StrEnum)`` — ``CONTINUE``, ``CLARIFY``,
        ``DISAMBIGUATE``, ``FAILED``.

``agent_api.graph.gates`` (PURE: no I/O, no langgraph import)
    ``CONFIDENCE_THRESHOLD = 0.7`` · ``MAX_DISAMBIGUATION_OPTIONS = 3``
    ``REQUIRED_ENTITIES: Mapping[Intent, frozenset[str]]`` — a key for EVERY
        intent, so a new label cannot arrive with no answer to "what does this
        need before it can act".
    ``def missing_required_entities(extraction) -> frozenset[str]``
    ``def needs_clarification(extraction) -> bool``
    ``def candidate_outcome(candidate_count: int) -> TurnOutcome``
    ``def capped_options[T](candidates: Sequence[T]) -> tuple[T, ...]``

the design's boundary rule is what makes this file possible: every decision is
computed by framework-free code and the conditional edge only reads the
result. Nothing here imports langgraph, opens a socket or touches Postgres.
"""

import ast
from pathlib import Path
from typing import Any

import pytest
from agent_api.graph import gates as gates_module
from agent_api.graph.gates import (
    CONFIDENCE_THRESHOLD,
    MAX_DISAMBIGUATION_OPTIONS,
    REQUIRED_ENTITIES,
    candidate_outcome,
    capped_options,
    missing_required_entities,
    needs_clarification,
)
from agent_api.graph.state import Intent, IntentEntities, IntentExtraction, TurnOutcome

SPEC_INTENTS: frozenset[str] = frozenset(
    {
        "reset_password",
        "unlock_account",
        "request_access",
        "revoke_access",
        "report_mover",
        "onboard",
        "offboard",
        "open_ticket",
        "ask_policy",
        "check_my_access",
        "approvals",
        "review",
        "other",
    }
)
"""the design's closed label set, written out rather than derived: counted from
the specification's own list, thirteen of them."""

PLANNED_REQUIREMENTS: dict[str, frozenset[str]] = {
    "reset_password": frozenset(),
    "unlock_account": frozenset(),
    "request_access": frozenset({"system"}),
    "revoke_access": frozenset({"system"}),
    "report_mover": frozenset({"target_user", "department"}),
    "onboard": frozenset({"target_user", "department"}),
    "offboard": frozenset({"target_user"}),
    "open_ticket": frozenset(),
    "ask_policy": frozenset(),
    "check_my_access": frozenset(),
    "approvals": frozenset(),
    "review": frozenset(),
    "other": frozenset(),
}
"""The plan's decision table as a literal, so a requirement quietly dropped
from the code is a red row here and not a system the agent guesses at."""

FULL_CONFIDENCE = 1.0
"""Used wherever the case must be decided by the ENTITY half of the gate, so
deleting the confidence comparison alone cannot make it pass."""


def _extraction(intent: Intent, *, confidence: float, **entities: Any) -> IntentExtraction:
    return IntentExtraction(
        intent=intent,
        entities=IntentEntities(**entities),
        confidence=confidence,
    )


def _filled(names: frozenset[str]) -> dict[str, Any]:
    return {name: (30 if name == "expiry_days" else f"fake-{name}") for name in names}


# --- the constants and the closed sets ----------------------------------------


def test_the_gate_constants_are_exactly_the_planned_values() -> None:
    """a project rule: catches a threshold nudged to make a flaky eval pass.
    0.65 would let a guess through and 0.75 would ask a clarifying question
    for turns the model got right; four options would put a fourth system in
    front of a user the design caps at three."""
    assert CONFIDENCE_THRESHOLD == 0.7
    assert MAX_DISAMBIGUATION_OPTIONS == 3


def test_intent_has_exactly_the_thirteen_spec_members() -> None:
    """Catches plan §5's first risk from the code's side: an intent added,
    renamed or dropped relative to the design. The value is what the gateway's
    JSON schema encloses, so a renamed member silently changes the closed set
    the model may answer with."""
    assert {intent.value for intent in Intent} == SPEC_INTENTS
    assert len(list(Intent)) == 13


def test_required_entities_covers_exactly_every_intent() -> None:
    """Catches a table keyed only by the intents that happen to need
    something: a `.get(intent, frozenset())` lookup would then make any FUTURE
    intent require nothing by default, which is the failure mode where a new
    mutating intent runs on entities nobody checked."""
    assert set(REQUIRED_ENTITIES) == set(Intent)


@pytest.mark.parametrize(
    ("intent_value", "expected"),
    sorted((key, value) for key, value in PLANNED_REQUIREMENTS.items()),
)
def test_each_intents_required_entity_set_is_the_planned_one(
    intent_value: str, expected: frozenset[str]
) -> None:
    """Catches a requirement dropped from one row — most of all
    `request_access` losing `system`, which is precisely the design's "never
    guess a system or role" turning into a guess."""
    assert REQUIRED_ENTITIES[Intent(intent_value)] == expected


def test_every_required_entity_name_is_a_field_the_extraction_carries() -> None:
    """Catches a table naming an entity the model never returns (`system_slug`
    for `system`, say): the required entity would be missing on every turn, so
    that intent could never do anything but ask a clarifying question."""
    fields = set(IntentEntities.model_fields)
    for intent, required in REQUIRED_ENTITIES.items():
        assert required <= fields, intent


# --- the confidence half of the gate ------------------------------------------


def test_confidence_exactly_at_the_threshold_does_not_clarify() -> None:
    """The inclusive half of the boundary: catches a `<=` comparison, which
    refuses the exact confidence the design accepts and turns a decided turn
    into a question. The intent needs no entity, so the entity half of the
    gate cannot be what answers."""
    extraction = _extraction(Intent.ASK_POLICY, confidence=CONFIDENCE_THRESHOLD)
    assert needs_clarification(extraction) is False


def test_confidence_just_under_the_threshold_clarifies() -> None:
    """The exclusive half: catches a missing or inverted confidence
    comparison. Every required entity for this intent is present (there are
    none), so deleting the confidence check alone flips this case."""
    extraction = _extraction(Intent.ASK_POLICY, confidence=0.699)
    assert needs_clarification(extraction) is True


@pytest.mark.parametrize("intent_value", sorted(SPEC_INTENTS))
def test_a_confident_turn_with_its_entities_filled_does_not_clarify(intent_value: str) -> None:
    """Catches an entity requirement that can never be satisfied — a name the
    extraction cannot carry, or a check reading the wrong field — which would
    strand that intent in a clarifying loop no answer escapes."""
    intent = Intent(intent_value)
    extraction = _extraction(
        intent, confidence=FULL_CONFIDENCE, **_filled(REQUIRED_ENTITIES[intent])
    )
    assert missing_required_entities(extraction) == frozenset()
    assert needs_clarification(extraction) is False


@pytest.mark.parametrize(
    "intent_value", sorted(name for name, required in PLANNED_REQUIREMENTS.items() if required)
)
def test_a_confident_turn_missing_its_entities_clarifies(intent_value: str) -> None:
    """Catches the entity half of the gate being dropped: confidence is 1.0,
    so the confidence comparison cannot decide these rows and only the missing
    entity can. Without it the agent would act on `request_access`
    with no system named."""
    intent = Intent(intent_value)
    extraction = _extraction(intent, confidence=FULL_CONFIDENCE)
    assert missing_required_entities(extraction) == PLANNED_REQUIREMENTS[intent_value]
    assert needs_clarification(extraction) is True


def test_missing_required_entities_names_every_absent_field_not_just_the_first() -> None:
    """Catches a check that returns on the first miss: a `report_mover` turn
    naming neither person nor department would come back asking for one of
    them, and the next turn would ask for the other."""
    extraction = _extraction(Intent.REPORT_MOVER, confidence=FULL_CONFIDENCE)
    assert missing_required_entities(extraction) == frozenset({"target_user", "department"})

    half = _extraction(Intent.REPORT_MOVER, confidence=FULL_CONFIDENCE, target_user="fake-person")
    assert missing_required_entities(half) == frozenset({"department"})


def test_an_empty_string_entity_does_not_satisfy_a_requirement() -> None:
    """Catches a presence check written as `is not None`: the model returning
    `""` for a system it could not read would count as an answer, and the turn
    would go on to match candidates against an empty term."""
    extraction = _extraction(Intent.REQUEST_ACCESS, confidence=FULL_CONFIDENCE, system="")
    assert missing_required_entities(extraction) == frozenset({"system"})
    assert needs_clarification(extraction) is True


# --- the ambiguity half -------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (0, TurnOutcome.CLARIFY),
        (1, TurnOutcome.CONTINUE),
        (2, TurnOutcome.DISAMBIGUATE),
        (3, TurnOutcome.DISAMBIGUATE),
        (4, TurnOutcome.DISAMBIGUATE),
    ],
)
def test_candidate_outcome_maps_each_count_to_its_branch(count: int, expected: TurnOutcome) -> None:
    """Catches the two ends being collapsed: zero candidates answered as a
    disambiguation offers a user an empty list of options, and two candidates
    answered as `CONTINUE` is the agent picking a system for them — SPEC
    §10.1's "never guess a system or role"."""
    assert candidate_outcome(count) is expected


def test_capped_options_returns_at_most_three_and_keeps_the_order() -> None:
    """Catches a cap applied by slicing a set (order lost, so the same
    question offers different options on a retry) or not applied at all, which
    puts a fourth option in front of a user the design caps at three."""
    candidates = ("first", "second", "third", "fourth")
    assert capped_options(candidates) == ("first", "second", "third")


def test_capped_options_returns_everything_below_the_cap() -> None:
    """Catches a cap that pads or truncates to a fixed length: two candidates
    must be offered as two options, not as three with one repeated."""
    assert capped_options(["only", "two"]) == ("only", "two")
    assert capped_options([]) == ()


# --- the design's boundary rule, statically ------------------------------------


def test_the_gates_module_imports_no_framework_and_no_io() -> None:
    """Catches the gate migrating into a conditional edge or a node: an import
    of langgraph, sqlalchemy or httpx here means the decision is no longer
    framework-free code that a test can call without a graph, which is the one
    property the design's boundary rule asks for."""
    source = Path(str(gates_module.__file__))
    tree = ast.parse(source.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module.split(".")[0])

    forbidden = {"langgraph", "sqlalchemy", "httpx", "redis", "qdrant_client"}
    assert imported & forbidden == set()
