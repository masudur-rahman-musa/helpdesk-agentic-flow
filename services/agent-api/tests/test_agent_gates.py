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

An earlier change extends the same file with the planner's own gates:

``agent_api.graph.gates``
    ``INTENT_TOOLS: Mapping[Intent, frozenset[str]]`` — a row for EVERY
        intent, union exactly the eighteen planner tools, ``search_policy`` in
        none of them.
    ``def allowed_tools(intent: Intent) -> frozenset[str]`` — reads the table.
    ``MAX_TOOL_CALLS = 6`` · ``MAX_CALLS_PER_ROUND = 3`` ·
        ``MAX_TURN_TOKENS = 40000`` · ``TURN_DEADLINE_SECONDS = 90.0``
    ``def may_plan_another_round(*, tool_calls_made: int, tokens_used: int,
        elapsed_seconds: float) -> bool`` — all three bounds INCLUSIVE.
    ``def validation_outcome(accepted: bool) -> TurnOutcome``

An earlier change extends it once more, with the two predicates the execution cycle
reads and the sixth outcome:

``agent_api.graph.state``
    ``TurnOutcome`` gains ``DONE`` (value ``"done"``): the round that proposed
        nothing, and the budget spent by a turn that already called something.
        SIX members, no seventh.

``agent_api.graph.gates``
    ``def execution_outcome(validated_count: int) -> TurnOutcome`` —
        ``CONTINUE`` for a round with calls to make, ``DONE`` for an empty one.
    ``def exhausted_outcome(result_count: int) -> TurnOutcome`` —
        ``REFUSE`` when the turn has no tool results, ``DONE`` when it has any. Both take a COUNT and are called positionally
        here, so the parameter names are the implementer's to choose.

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
    INTENT_TOOLS,
    MAX_CALLS_PER_ROUND,
    MAX_DISAMBIGUATION_OPTIONS,
    MAX_TOOL_CALLS,
    MAX_TURN_TOKENS,
    REQUIRED_ENTITIES,
    RETRIEVES_POLICY,
    TURN_DEADLINE_SECONDS,
    allowed_tools,
    candidate_outcome,
    capped_options,
    execution_outcome,
    exhausted_outcome,
    may_plan_another_round,
    missing_required_entities,
    needs_clarification,
    retrieval_outcome,
    should_retrieve,
    validation_outcome,
)
from agent_api.graph.state import Intent, IntentEntities, IntentExtraction, TurnOutcome
from agent_api.tools.catalogue import SEARCH_POLICY, TOOL_INPUTS

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

PLANNED_RETRIEVAL: dict[str, bool] = {
    "reset_password": False,
    "unlock_account": False,
    "request_access": True,
    "revoke_access": False,
    "report_mover": False,
    "onboard": False,
    "offboard": False,
    "open_ticket": False,
    "ask_policy": True,
    "check_my_access": False,
    "approvals": False,
    "review": False,
    "other": False,
}
"""the design step 5 as a literal: retrieval runs for exactly two of the
thirteen intents. Written out rather than derived from a `True` set, so a row
added to `Intent` with no answer here is a red test and not a silent default."""

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


# --- an earlier change: the retrieval half ---------------------------------------------


def test_turn_outcome_carries_the_refusal_branch() -> None:
    """Catches an implementation that reuses `CLARIFY` for a policy refusal.

    The two branches store different things — a clarify writes the model's own
    question, a refusal writes the fixed `REFUSAL_REPLY` and offers a ticket —
    so collapsing them would answer "no policy covers this" with a question the
    user has already answered. The VALUE is pinned as well as the member: it is
    what a checkpointed state and a trace line are read by."""
    assert TurnOutcome.REFUSE.value == "refuse"


def test_retrieves_policy_covers_exactly_every_intent() -> None:
    """Catches a table keyed only by the two intents that retrieve.

    A `.get(intent, False)` lookup over a two-key map reads identically today
    and makes every FUTURE intent silently non-retrieving, which is how a new
    policy-answering intent ships answering from nothing. Same reasoning as
    `REQUIRED_ENTITIES`."""
    assert set(RETRIEVES_POLICY) == set(Intent)
    assert len(RETRIEVES_POLICY) == 13


@pytest.mark.parametrize(
    ("intent_value", "expected"),
    sorted((key, value) for key, value in PLANNED_RETRIEVAL.items()),
)
def test_should_retrieve_is_true_for_exactly_the_two_planned_intents(
    intent_value: str, expected: bool
) -> None:
    """Catches both directions of plan §5's third risk.

    A predicate returning True for everything makes eleven intents — a password
    reset, an approvals list — pay a Qdrant round trip they never read, and one
    returning False for `ask_policy` makes the agent answer a policy question
    with no policy in front of it. Each row is decided by this predicate alone;
    nothing else in `gates.py` reads an intent."""
    assert should_retrieve(Intent(intent_value)) is expected


def test_should_retrieve_agrees_with_the_table_it_reads() -> None:
    """Catches a `should_retrieve` that hard-codes its two intents in an `in`
    check instead of reading `RETRIEVES_POLICY`: the table would then be a
    decoration the tests above pass on, and a row edited there would change
    nothing at all."""
    for intent in Intent:
        assert should_retrieve(intent) is RETRIEVES_POLICY[intent]


@pytest.mark.parametrize(
    ("refused", "expected"),
    [(True, TurnOutcome.REFUSE), (False, TurnOutcome.CONTINUE)],
)
def test_retrieval_outcome_maps_the_refusal_flag_to_its_branch(
    refused: bool, expected: TurnOutcome
) -> None:
    """Catches an inverted read of the tool's `refused` flag, which would send
    every answerable question to the refusal ending and hand every genuinely
    unanswerable one on to 17c with an empty citation list. Decision D puts the
    threshold in the tool, so this is the only place the flag is interpreted."""
    assert retrieval_outcome(refused) is expected


# --- an earlier change: the tool table and loop control --------------------------------

PLANNER_TOOLS: frozenset[str] = frozenset(
    {
        "get_me",
        "list_my_access",
        "list_my_requests",
        "classify_access_request",
        "create_access_request",
        "revoke_access",
        "onboard_user",
        "transfer_user",
        "offboard_user",
        "open_access_review",
        "list_review_items",
        "decide_review_item",
        "list_pending_approvals",
        "approve_request",
        "reject_request",
        "reset_own_password",
        "unlock_own_account",
        "create_ticket",
    }
)
"""The eighteen tools a planner may propose, as a literal.

`TOOL_INPUTS`' nineteen minus `search_policy`, counted from the server's own
registry: twenty-two registered, less the three row 24 owns (`search_audit`,
`set_kill_switch`, `rollback_request`), less the one the agent never plans."""

PLANNED_INTENT_TOOLS: dict[str, frozenset[str]] = {
    "reset_password": frozenset({"reset_own_password", "create_ticket"}),
    "unlock_account": frozenset({"unlock_own_account", "create_ticket"}),
    "request_access": frozenset(
        {
            "get_me",
            "list_my_access",
            "classify_access_request",
            "create_access_request",
            "create_ticket",
        }
    ),
    "revoke_access": frozenset({"list_my_access", "revoke_access", "create_ticket"}),
    "report_mover": frozenset({"transfer_user", "create_ticket"}),
    "onboard": frozenset({"onboard_user", "create_ticket"}),
    "offboard": frozenset({"offboard_user", "create_ticket"}),
    "open_ticket": frozenset({"create_ticket"}),
    "ask_policy": frozenset(),
    "check_my_access": frozenset({"get_me", "list_my_access", "list_my_requests"}),
    "approvals": frozenset({"list_pending_approvals", "approve_request", "reject_request"}),
    "review": frozenset({"open_access_review", "list_review_items", "decide_review_item"}),
    "other": frozenset(),
}
"""Decision A's table as a literal: thirteen rows whose union is the eighteen.

Written out here rather than derived, so a row widened in `gates.py` — an
approvals turn gaining `offboard_user` — is a red row rather than a tool the
model may now propose. The plan's §6 fixes the union, the two empty rows and
`search_policy`'s absence; the membership of each remaining row is derived
from what the design says that intent DOES, and is fixed here."""


def test_the_loop_control_constants_are_exactly_the_planned_values() -> None:
    """a project rule on decision F's four numbers. Catches every one of them
    being quietly widened to make a long eval turn finish: eight tool calls is
    a loop nobody budgeted, a per-round cap equal to the per-turn cap makes the
    per-turn cap unreachable, and a deadline under one MCP read timeout
    (`MCP_READ_TIMEOUT_SECONDS = 30.0`) would fire INSTEAD of the tool's own
    timeout rather than after a tool genuinely stalled."""
    assert MAX_TOOL_CALLS == 6
    assert MAX_CALLS_PER_ROUND == 3
    assert MAX_TURN_TOKENS == 40000
    assert TURN_DEADLINE_SECONDS == 90.0
    assert MAX_CALLS_PER_ROUND < MAX_TOOL_CALLS


def test_intent_tools_covers_exactly_every_intent() -> None:
    """The same shape as `REQUIRED_ENTITIES` and `RETRIEVES_POLICY`, for the
    same reason: catches a table keyed only by the intents that plan
    something. Read through a `.get(intent, ...)` default, the next intent
    added to the enum would inherit whatever that default is — either every
    tool or none — instead of waiting for somebody to write its row."""
    assert set(INTENT_TOOLS) == set(Intent)
    assert len(INTENT_TOOLS) == 13


def test_the_union_of_the_rows_is_exactly_the_eighteen_planner_tools() -> None:
    """Both directions at once. Catches a tool the catalogue registers that no
    row offers — dead weight nothing can ever propose — and a row naming a
    tool the catalogue does not carry, which would be proposed and then fail
    argument validation on every turn. `search_policy` is asserted absent
    separately: retrieval is `retrieve_policy`'s own call, and a row offering
    it lets the model re-run the corpus search inside its own loop."""
    union: frozenset[str] = frozenset().union(*INTENT_TOOLS.values())

    assert union == PLANNER_TOOLS
    assert len(union) == 18
    assert union == set(TOOL_INPUTS) - {SEARCH_POLICY}
    assert all(SEARCH_POLICY not in row for row in INTENT_TOOLS.values())


@pytest.mark.parametrize(
    ("intent_value", "expected"),
    sorted((key, value) for key, value in PLANNED_INTENT_TOOLS.items()),
)
def test_each_intents_row_is_the_planned_one(intent_value: str, expected: frozenset[str]) -> None:
    """Decision A row by row, which the union test cannot see: a tool moved
    from one row to another leaves the union untouched. Catches the case the
    decision exists for — an approvals or a policy turn that can reach a
    lifecycle mutation — and the opposite, a row narrowed until the intent can
    no longer do the thing it names."""
    assert INTENT_TOOLS[Intent(intent_value)] == expected


def test_the_two_intents_that_plan_nothing_are_offered_nothing() -> None:
    """Criterion 4's table half. `ask_policy` is answered from the citations
    alone and `other` is not a request at all, so a row with anything in it
    invites a model call and a mutation for a turn that asked for neither.
    Kept apart from the row table above because this is the pair the planner
    short-circuits on BEFORE the gateway is touched."""
    assert allowed_tools(Intent.ASK_POLICY) == frozenset()
    assert allowed_tools(Intent.OTHER) == frozenset()


def test_allowed_tools_agrees_with_the_table_it_reads() -> None:
    """Catches an `allowed_tools` that names intents in an `if` chain instead
    of reading `INTENT_TOOLS`: every row test above would still pass, the
    table would be a decoration, and an edit to a row would change nothing at
    all. Same failure `should_retrieve` is guarded against."""
    for intent in Intent:
        assert allowed_tools(intent) == INTENT_TOOLS[intent]


# --- the design's loop control, as a pure predicate ----------------------------


@pytest.mark.parametrize(
    ("tool_calls_made", "tokens_used", "elapsed_seconds", "expected"),
    [
        (0, 0, 0.0, True),
        (MAX_TOOL_CALLS, 0, 0.0, True),
        (MAX_TOOL_CALLS + 1, 0, 0.0, False),
        (0, MAX_TURN_TOKENS, 0.0, True),
        (0, MAX_TURN_TOKENS + 1, 0.0, False),
        (0, 0, TURN_DEADLINE_SECONDS, True),
        (0, 0, TURN_DEADLINE_SECONDS + 0.1, False),
    ],
)
def test_may_plan_another_round_is_inclusive_at_each_of_the_three_bounds(
    tool_calls_made: int, tokens_used: int, elapsed_seconds: float, expected: bool
) -> None:
    """Three inclusive pairs, each decided by ONE bound: in every
    row the other two counters sit at zero, so deleting the bound under test
    is the only change that flips that row.

    Catches an off-by-one at any of the three — a turn cut off at five calls,
    at one token under the budget, or a tenth of a second before the deadline
    — and a budget compared against the wrong operand, which is how a bound
    that never bites ships green."""
    assert (
        may_plan_another_round(
            tool_calls_made=tool_calls_made,
            tokens_used=tokens_used,
            elapsed_seconds=elapsed_seconds,
        )
        is expected
    )


def test_a_turn_past_every_bound_at_once_still_refuses() -> None:
    """Catches the three checks combined with `and` instead of `or`, which the
    single-bound rows above cannot see: each of them has two counters at zero,
    so an `and` would answer them correctly and only fail once more than one
    bound is live."""
    assert (
        may_plan_another_round(
            tool_calls_made=MAX_TOOL_CALLS + 1,
            tokens_used=MAX_TURN_TOKENS + 1,
            elapsed_seconds=TURN_DEADLINE_SECONDS + 0.1,
        )
        is False
    )


@pytest.mark.parametrize(
    ("accepted", "expected"),
    [(True, TurnOutcome.CONTINUE), (False, TurnOutcome.REFUSE)],
)
def test_validation_outcome_maps_the_rounds_verdict_to_its_branch(
    accepted: bool, expected: TurnOutcome
) -> None:
    """Catches an inverted read of the round's verdict, which would refuse
    every well-formed plan and hand every malformed one on to 17d for
    execution — the second half being decision C's exact failure, a half-done
    mutation nobody asked for."""
    assert validation_outcome(accepted) is expected


# --- an earlier change: the execution cycle's two predicates ----------------------------

SPEC_OUTCOMES: frozenset[str] = frozenset(
    {"continue", "clarify", "disambiguate", "refuse", "failed", "done", "await_approval"}
)
"""Every branch a node may record, written out as a literal: SEVEN, counted
from the five an earlier change fixed, plus an earlier change's `DONE` and an earlier change's
`AWAIT_APPROVAL`."""


def test_turn_outcome_has_exactly_the_seven_members() -> None:
    """Catches `DONE` added as a seventh spelling of an existing branch (a
    `COMPOSE` beside it), and catches it never being added at all — the empty
    round would then have to reuse `CONTINUE`, which the edge after validation
    cannot tell apart from a round with calls to execute, so a turn with
    nothing left to do would execute an empty list and plan again forever.

    An earlier change §8: `AWAIT_APPROVAL` is a NEW branch, not a synonym for one of
    the six. Catches a pause expressed by reusing `DONE` — `_after_execution`
    would then route a paused turn to the composition, which would tell the
    user their request was handled while nothing is waiting on the approval.

    The VALUE is pinned as well as the member for both: the state is written
    to a Postgres checkpoint, where the outcome is a bare string and a
    renamed member is a checkpoint no later process can read
    (`test_agent_approval_interrupt.py::test_the_paused_state_is_readable_from_a_second_checkpointer`)."""
    assert {member.value for member in TurnOutcome} == SPEC_OUTCOMES
    assert len(TurnOutcome) == 7
    assert TurnOutcome.DONE.value == "done"
    assert TurnOutcome.AWAIT_APPROVAL.value == "await_approval"


@pytest.mark.parametrize(
    ("validated_count", "expected"),
    [(0, TurnOutcome.DONE), (1, TurnOutcome.CONTINUE), (MAX_CALLS_PER_ROUND, TurnOutcome.CONTINUE)],
)
def test_execution_outcome_sends_only_a_non_empty_round_to_execution(
    validated_count: int, expected: TurnOutcome
) -> None:
    """Catches a predicate that answers `CONTINUE` for the empty round, which
    is the ordinary end of a turn: `execute_tools` would run over an empty
    tuple, edge back to `plan_tools`, and the turn would circle until the
    recursion limit aborted it rather than composing a reply.

    The inverted twin is caught by the non-zero rows: a `DONE` there would
    compose a reply before the calls the planner asked for were ever made."""
    assert execution_outcome(validated_count) is expected


@pytest.mark.parametrize(
    ("result_count", "expected"),
    [(0, TurnOutcome.REFUSE), (1, TurnOutcome.DONE), (MAX_TOOL_CALLS, TurnOutcome.DONE)],
)
def test_exhausted_outcome_refuses_only_a_turn_that_has_done_nothing(
    result_count: int, expected: TurnOutcome
) -> None:
    """Decision C, and the reason it amends the design. Catches the literal
    reading of the specification — every exhausted budget to
    `refuse_and_ticket` — which after a real mutation answers the person who
    made it with the refusal template: "nothing in the current policy covers
    this", said to somebody whose access request was just filed.

    The zero row is the other half: a turn that spent its budget having called
    nothing has nothing to compose from, so composing would put the model in
    front of a user with no tool result behind it."""
    assert exhausted_outcome(result_count) is expected
