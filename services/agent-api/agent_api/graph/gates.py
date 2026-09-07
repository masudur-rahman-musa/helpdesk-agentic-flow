"""Every turn gate as a pure predicate (the design's boundary rule).

LangGraph owns routing; it never owns a decision. Each branch is computed here,
by code that imports no framework and touches no store, and the conditional
edge downstream only reads the field the node wrote: clarify-or-continue after
intent extraction, clarify/continue/disambiguate after entity resolution, and
an earlier change's pair — whether the turn retrieves at all, and what the tool's own
refusal flag means. That is what makes the rule
enforceable rather than a claim: every case below is tested with no graph at
all, and `test_agent_gates.py::test_the_gates_module_imports_no_framework_and_no_io`
parses this file to prove the property has not quietly moved.
"""

from collections.abc import Mapping, Sequence

from agent_api.graph.state import Intent, IntentExtraction, TurnOutcome

CONFIDENCE_THRESHOLD = 0.7
"""the design step 3's gate, INCLUSIVE: exactly 0.7 proceeds.

a project rule pins the value from both sides
(`test_agent_gates.py::test_confidence_exactly_at_the_threshold_does_not_clarify`
and `::test_confidence_just_under_the_threshold_clarifies`). Lowering it lets
a guess act; raising it asks a question about turns the model read correctly.
"""

MAX_DISAMBIGUATION_OPTIONS = 3
"""the design step 4's cap on the options one question may offer.

A list a person can hold in their head, and a hard bound on how much of a
tenant's directory one ambiguous word can enumerate.
"""

REQUIRED_ENTITIES: Mapping[Intent, frozenset[str]] = {
    Intent.RESET_PASSWORD: frozenset(),
    Intent.UNLOCK_ACCOUNT: frozenset(),
    Intent.REQUEST_ACCESS: frozenset({"system"}),
    Intent.REVOKE_ACCESS: frozenset({"system"}),
    Intent.REPORT_MOVER: frozenset({"target_user", "department"}),
    Intent.ONBOARD: frozenset({"target_user", "department"}),
    Intent.OFFBOARD: frozenset({"target_user"}),
    Intent.OPEN_TICKET: frozenset(),
    Intent.ASK_POLICY: frozenset(),
    Intent.CHECK_MY_ACCESS: frozenset(),
    Intent.APPROVALS: frozenset(),
    Intent.REVIEW: frozenset(),
    Intent.OTHER: frozenset(),
}
"""What each intent must have named before the turn may go on.

A row for EVERY intent, including the nine that require nothing. A table
keyed only by the intents that need something would be read through a
`.get(intent, frozenset())` default, and the next mutating intent added to
`Intent` would then silently require nothing at all. Pinned by
`test_agent_gates.py::test_required_entities_covers_exactly_every_intent`.

`role` is deliberately not required by `request_access`: the design lets a
request name a system and let the template or the policy engine pick the
role, and requiring it here would ask a question the engine can answer.
"""


RETRIEVES_POLICY: Mapping[Intent, bool] = {
    Intent.RESET_PASSWORD: False,
    Intent.UNLOCK_ACCOUNT: False,
    Intent.REQUEST_ACCESS: True,
    Intent.REVOKE_ACCESS: False,
    Intent.REPORT_MOVER: False,
    Intent.ONBOARD: False,
    Intent.OFFBOARD: False,
    Intent.OPEN_TICKET: False,
    Intent.ASK_POLICY: True,
    Intent.CHECK_MY_ACCESS: False,
    Intent.APPROVALS: False,
    Intent.REVIEW: False,
    Intent.OTHER: False,
}
"""Which intents pay for a policy search (the design step 5).

Exactly two: `request_access`, whose decision is made against policy, and
`ask_policy`, which IS a policy question. The other eleven are answered from
the directory or from a tool's own result, so a retrieval there is a round
trip nobody reads.

A row for EVERY intent, for the same reason `REQUIRED_ENTITIES` has one: a
table keyed only by the two would be read through a `.get(intent, False)`
default, and the next policy-answering intent added to `Intent` would silently
answer from nothing. Pinned by
`test_agent_gates.py::test_retrieves_policy_covers_exactly_every_intent`.
"""


def missing_required_entities(extraction: IntentExtraction) -> frozenset[str]:
    """Every required entity this extraction did not supply, not just the first.

    Absence is "None or empty", never "is None": a model that could not read a
    system name may answer `""`, and treating that as an answer would send an
    empty term to the matcher. Pinned by
    `test_agent_gates.py::test_an_empty_string_entity_does_not_satisfy_a_requirement`.

    All of them at once, because a turn that asks for one missing field and
    then asks for the next is two questions where one would do
    (`::test_missing_required_entities_names_every_absent_field_not_just_the_first`).
    """
    return frozenset(
        name
        for name in REQUIRED_ENTITIES[extraction.intent]
        if not _is_supplied(getattr(extraction.entities, name))
    )


def needs_clarification(extraction: IntentExtraction) -> bool:
    """the design step 3: unsure of the label, or missing something it needs.

    The two halves are independent and each is pinned by a case the other
    cannot decide: the confidence pair runs on an intent
    that requires nothing, and every entity row runs at confidence 1.0.
    """
    return extraction.confidence < CONFIDENCE_THRESHOLD or bool(
        missing_required_entities(extraction)
    )


def candidate_outcome(candidate_count: int) -> TurnOutcome:
    """the design step 4's branch, from the number of candidates alone.

    Zero clarifies rather than disambiguating, because an empty option list is
    not a question anyone can answer; two or more disambiguate rather than
    continuing, because picking one for the user IS guessing a system, which
    step 3 forbids. Pinned across 0/1/2/3/4 by
    `test_agent_gates.py::test_candidate_outcome_maps_each_count_to_its_branch`.
    """
    if candidate_count == 0:
        return TurnOutcome.CLARIFY
    if candidate_count == 1:
        return TurnOutcome.CONTINUE
    return TurnOutcome.DISAMBIGUATE


def should_retrieve(intent: Intent) -> bool:
    """Whether this turn asks the policy corpus anything (the design step 5).

    Reads the table rather than naming the two intents, so a row edited above
    changes behaviour instead of leaving the table decorative. Pinned by
    `test_agent_gates.py::test_should_retrieve_agrees_with_the_table_it_reads`.
    """
    return RETRIEVES_POLICY[intent]


def retrieval_outcome(refused: bool) -> TurnOutcome:
    """The branch the tool's own `refused` flag names (decision D).

    The threshold is NOT recomputed here: `should_refuse` decided it over the
    fused hits inside the tool (15a decision D), so this function interprets a
    boolean and nothing else. Pinned both ways by
    `test_agent_gates.py::test_retrieval_outcome_maps_the_refusal_flag_to_its_branch`.
    """
    return TurnOutcome.REFUSE if refused else TurnOutcome.CONTINUE


def capped_options[T](candidates: Sequence[T]) -> tuple[T, ...]:
    """At most `MAX_DISAMBIGUATION_OPTIONS` candidates, order preserved.

    Order is part of the answer: the matcher ranks its fuzzy tier best-first,
    so slicing an unordered collection would offer a different three each time
    the same question is asked.
    """
    return tuple(candidates[:MAX_DISAMBIGUATION_OPTIONS])


def _is_supplied(value: object) -> bool:
    """Whether the model actually named this entity.

    A whitespace-only string is as absent as an empty one: it resolves to the
    empty term after normalisation, and the matcher would compare it against
    every candidate in the tenant.
    """
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True
