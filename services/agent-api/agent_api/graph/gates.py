"""Every turn gate as a pure predicate (the design's boundary rule).

LangGraph owns routing; it never owns a decision. Each branch is computed here,
by code that imports no framework and touches no store, and the conditional
edge downstream only reads the field the node wrote: clarify-or-continue after
intent extraction, clarify/continue/disambiguate after entity resolution, and
an earlier change's pair — whether the turn retrieves at all, and what the tool's own
refusal flag means — and an earlier change's, which end the loop: whether an accepted
round has anything to execute, and what a spent budget means to a turn that has
already called something. An earlier change adds the last one: whether a round's answers
PAUSE the turn, and which request such a pause is waiting on. That is what
makes the rule
enforceable rather than a claim: every case below is tested with no graph at
all, and `test_agent_gates.py::test_the_gates_module_imports_no_framework_and_no_io`
parses this file to prove the property has not quietly moved.
"""

import uuid
from collections.abc import Mapping, Sequence

from agent_api.graph.state import Intent, IntentExtraction, ToolResult, TurnOutcome

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


INTENT_TOOLS: Mapping[Intent, frozenset[str]] = {
    Intent.RESET_PASSWORD: frozenset({"reset_own_password", "create_ticket"}),
    Intent.UNLOCK_ACCOUNT: frozenset({"unlock_own_account", "create_ticket"}),
    Intent.REQUEST_ACCESS: frozenset(
        {
            "get_me",
            "list_my_access",
            "classify_access_request",
            "create_access_request",
            "create_ticket",
        }
    ),
    Intent.REVOKE_ACCESS: frozenset({"list_my_access", "revoke_access", "create_ticket"}),
    Intent.REPORT_MOVER: frozenset({"transfer_user", "create_ticket"}),
    Intent.ONBOARD: frozenset({"onboard_user", "create_ticket"}),
    Intent.OFFBOARD: frozenset({"offboard_user", "create_ticket"}),
    Intent.OPEN_TICKET: frozenset({"create_ticket"}),
    Intent.ASK_POLICY: frozenset(),
    Intent.CHECK_MY_ACCESS: frozenset({"get_me", "list_my_access", "list_my_requests"}),
    Intent.APPROVALS: frozenset({"list_pending_approvals", "approve_request", "reject_request"}),
    Intent.REVIEW: frozenset({"open_access_review", "list_review_items", "decide_review_item"}),
    Intent.OTHER: frozenset(),
}
"""Which tools the planner may propose for each intent.

A row for EVERY intent, read through no `.get()` default, for the same reason
`REQUIRED_ENTITIES` and `RETRIEVES_POLICY` have one: the next intent added to
the enum is offered NOTHING until somebody writes its row, rather than
inheriting a permissive default. Pinned by
`test_agent_gates.py::test_intent_tools_covers_exactly_every_intent`, row by
row by `::test_each_intents_row_is_the_planned_one`, and as a union against
the catalogue by `::test_the_union_of_the_rows_is_exactly_the_eighteen_planner_tools`.

The RBAC matrix already refuses what a principal may not call, so this is not
authorization — it is the model's search space. Offering an approvals turn a
lifecycle mutation costs nothing today and makes a prompt-injection attempt
worth trying; thirteen rows cost one test and remove the class.

`search_policy` is in NO row (criterion 2). Retrieval is `retrieve_policy`'s
own call, decided by `RETRIEVES_POLICY` before the planner runs, and a row
offering it would let the model re-run the corpus search inside its own loop.

The two empty rows are the two intents that plan nothing: `ask_policy` is
answered from the citations alone, and `other` is not a request at all. Both
are decided by `allowed_tools` BEFORE the gateway is touched, so neither pays
a strong-alias call to be told there was nothing to call.
"""

MAX_TOOL_CALLS = 6
"""The most tool calls one turn may EXECUTE, INCLUSIVE (the design).

Exactly six is still allowed; the seventh is what stops the loop. Standing
rule 2, pinned from both sides by
`test_agent_gates.py::test_may_plan_another_round_is_inclusive_at_each_of_the_three_bounds`
and through the node by
`test_agent_turn.py::test_a_turn_exactly_at_the_call_budget_still_plans`.
"""

MAX_CALLS_PER_ROUND = 3
"""The most calls one planning round may PROPOSE (decision F).

Three because two chained calls plus a listing is the widest useful round: a
classification, the request it justifies, and the access listing that shaped
it. A round of six would make `MAX_TOOL_CALLS` unreachable in more than one
round and turn the per-turn cap into a per-round one.
"""

MAX_TURN_TOKENS = 40000
"""The most gateway tokens one turn may spend, INCLUSIVE (the design).

Counted from the gateway's own `total_tokens`, roughly two planning rounds
plus a composed reply on the strong alias. Exactly 40000 may still plan; one
more may not.
"""

TURN_DEADLINE_SECONDS = 90.0
"""How long one turn may run before it stops planning, INCLUSIVE (the design).

Three MCP read timeouts (`MCP_READ_TIMEOUT_SECONDS = 30.0`), so the deadline
can only fire AFTER a tool has genuinely stalled rather than instead of that
tool's own timeout. Measured from `started_at`, which `run_turn` stamps from
the injected clock, so no test sleeps and no node reads a wall clock of its
own.
"""


def allowed_tools(intent: Intent) -> frozenset[str]:
    """The tools this intent's planner may propose.

    Reads the table rather than naming intents, so a row edited above changes
    behaviour instead of leaving the table decorative — the same property
    `should_retrieve` is guarded for, pinned by
    `test_agent_gates.py::test_allowed_tools_agrees_with_the_table_it_reads`.
    """
    return INTENT_TOOLS[intent]


def may_plan_another_round(
    *, tool_calls_made: int, tokens_used: int, elapsed_seconds: float
) -> bool:
    """Whether the turn may plan again, on the three budgets alone (the design).

    Every bound is INCLUSIVE: exactly six executed calls, exactly the token
    budget and exactly the deadline are all still allowed, and it is the value
    one past each that refuses. The three are combined with `or` — ANY budget
    spent stops the loop — which
    `test_agent_gates.py::test_a_turn_past_every_bound_at_once_still_refuses`
    pins separately, because the single-bound rows cannot tell `or` from `and`.

    Nothing is measured here. The counters are written by the nodes that spend
    them and the elapsed seconds are computed by the caller from `started_at`
    and the injected clock, which is what keeps loop control pure and testable
    with no graph, no gateway and no sleep.
    """
    if tool_calls_made > MAX_TOOL_CALLS:
        return False
    if tokens_used > MAX_TURN_TOKENS:
        return False
    return not elapsed_seconds > TURN_DEADLINE_SECONDS


def validation_outcome(accepted: bool) -> TurnOutcome:
    """The branch a round's verdict names (decision C).

    A round is accepted or it is not; there is no partial answer, because
    running the valid two thirds of a plan is a half-done mutation nobody
    asked for. Pinned both ways by
    `test_agent_gates.py::test_validation_outcome_maps_the_rounds_verdict_to_its_branch`.
    """
    return TurnOutcome.CONTINUE if accepted else TurnOutcome.REFUSE


def execution_outcome(validated_count: int) -> TurnOutcome:
    """Whether an accepted round has anything to execute (17d criterion 4).

    The empty round is the ordinary end of a turn: a planner with nothing left
    to call proposes nothing, and answering `CONTINUE` here would send the turn
    through `execute_tools` over an empty tuple, back to `plan_tools`, and
    round again until the recursion limit aborted what should have been a
    composed reply. Pinned across an empty round and a full one by
    `test_agent_gates.py::test_execution_outcome_sends_only_a_non_empty_round_to_execution`.
    """
    return TurnOutcome.CONTINUE if validated_count else TurnOutcome.DONE


def exhausted_outcome(result_count: int) -> TurnOutcome:
    """What a spent budget means, given what the turn has already done (17d decision C).

    This AMENDS the design, which routes every exhausted budget to
    `refuse_and_ticket`. That is right for a turn which did nothing
    and wrong for one which already filed a request: the refusal template says
    no current policy covers the question, which after a real mutation is a
    false statement to the person who made it. So a turn with results composes
    over them, and only a turn with none refuses. Pinned both ways by
    `test_agent_gates.py::test_exhausted_outcome_refuses_only_a_turn_that_has_done_nothing`.
    """
    return TurnOutcome.DONE if result_count else TurnOutcome.REFUSE


CREATE_ACCESS_REQUEST = "create_access_request"
"""The one tool whose answer can pause a turn.

A chain that becomes pending some other way — a seat opened later, a review
item, a request filed by somebody else — is not this turn's business and gets
no pause here. Naming the tool is what makes that a property rather than a
coincidence: `classify_access_request` answers with the SAME `decision` field,
so a gate that scanned the round for a `decision` key would pause on a
classification that filed nothing. Pinned by that row of
`test_agent_approval_interrupt.py::test_approval_outcome_answers_await_only_for_an_approval_decision`.
"""

APPROVAL_DECISION = "approval"
"""The decision value the design gives a request that needs an approval chain."""


def _approval_payload(results: Sequence[ToolResult]) -> Mapping[str, object] | None:
    """The most recent successful `create_access_request` answer that pauses.

    Most recent, not first: a turn may file more than one request, and the
    pause belongs to the last thing that happened. A failed call carries no
    decision at all, so `ok` is checked before `payload` is read — a gate that
    read the payload of a refusal would find an empty dict and answer for it.
    """
    for result in reversed(tuple(results)):
        if result.tool != CREATE_ACCESS_REQUEST or not result.ok:
            continue
        if str(result.payload.get("decision", "")) != APPROVAL_DECISION:
            return None
        return result.payload
    return None


def approval_request_id(results: Sequence[ToolResult]) -> uuid.UUID | None:
    """The request a paused turn would wait on, or `None` if there is none.

    The companion the branch needs: `approval_outcome` says WHETHER to pause
    and this says what the pause is for, both read off the same payload by the
    same rules, so the outcome and the id on the state cannot disagree.

    Fails closed on every unusable shape — an absent key, `None`, an empty or
    whitespace string, a value that is not a canonical uuid — because
    `awaiting_request_id` is what an earlier change matches an approval decision
    against, and a pause carrying anything else is one nobody could ever
    resume. Pinned through the outcome by
    `test_agent_approval_interrupt.py::test_approval_outcome_fails_a_decision_with_no_request_id`
    and `::test_approval_outcome_fails_a_decision_whose_payload_omits_the_request_id`.
    """
    payload = _approval_payload(results)
    if payload is None:
        return None
    value: object = payload.get("request_id")
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value.strip())
    except ValueError:
        return None


def approval_seat_count(results: Sequence[ToolResult]) -> int:
    """How many approvals the paused request is waiting for.

    Read off the same payload as the branch itself, so the number a paused
    reply states is the number the request was actually filed with rather than
    a count the reply invented. Zero for a round that pauses nothing, and zero
    for a payload whose `required_approvers` is missing or is not a list: the
    reply renders "waiting for approval" either way, and a chain shape this
    gate cannot read is not a reason to fail a request that was filed.
    """
    payload = _approval_payload(results)
    if payload is None:
        return 0
    approvers: object = payload.get("required_approvers")
    if not isinstance(approvers, list):
        return 0
    return len(approvers)


def approval_outcome(results: Sequence[ToolResult]) -> TurnOutcome:
    """Whether this round's answers pause the turn.

    Three branches and no store: `AWAIT_APPROVAL` when a successful
    `create_access_request` says its decision went to an approval chain and
    named the request, `FAILED` when it says so and named nothing usable, and
    `CONTINUE` for everything else — an auto-granted request, a blocked one, a
    failed call, another tool's answer, an empty round.

    `FAILED` rather than a pause is the unhappy path criterion 7 fixes: a
    checkpoint claiming an approval nobody can decide would sit paused for ever
    behind a reply telling the user a person is looking at it.
    """
    if _approval_payload(results) is None:
        return TurnOutcome.CONTINUE
    if approval_request_id(results) is None:
        return TurnOutcome.FAILED
    return TurnOutcome.AWAIT_APPROVAL


APPROVE_REQUEST = "approve_request"
"""The tool an approver's recorded APPROVAL stands for.

The name is the contract, not a label: `compose_reply` quotes `tool_results` as
data and `plain_errors.PLAIN_ERRORS` is keyed by code, so a resumed turn's
decision has to read to the model exactly like a call this agent made. It did
run — in another process, through MCP, when the approver decided — and this is
that call's recorded output arriving late.
"""

REJECT_REQUEST = "reject_request"
"""The same for a recorded rejection. Two names because the composition is told
WHICH decision it is composing, and one name with a status field inside would
put that distinction where only a reader who parses the payload finds it."""

DECIDED_RESULTS: Mapping[str, tuple[str, bool]] = {
    "approved": (APPROVE_REQUEST, True),
    "executed": (APPROVE_REQUEST, True),
    "rejected": (REJECT_REQUEST, True),
    "failed": (APPROVE_REQUEST, False),
}
"""Every `RequestStatus` a resume may act on, and what it means (decision B).

Four rows, and the other three statuses are absent on purpose. `pending` has no
decision at all. `rolled_back` and `expired` are ENDED rather than decided: a
run resumed on one would compose a reply about an outcome that has since been
undone. Absence is the refusal — `decision_result` answers `None` for anything
not listed, and `resume.resume_turn` refuses on exactly that.

`failed` is decided and NOT ok: the chain completed, the fresh the design check
refused execution and the approval was kept. Telling the
composition that as a success would promise a grant that does not exist.

The keys are the string VALUES of `helpdesk.models.access.RequestStatus`,
written out rather than imported, because importing the ORM module here would
end this file's framework-free property, which
`test_agent_gates.py::test_the_gates_module_imports_no_framework_and_no_io`
asserts.
Pinned against the enum's own members, row by row, by
`test_agent_approval_resume.py::test_the_decision_result_is_built_from_the_row_and_not_from_an_argument`
and `::test_a_pending_request_maps_to_no_decision_result`, which parametrise
over `RequestStatus` and so go red if a member is renamed here or there.
"""


def decision_result(*, status: str, request_id: uuid.UUID) -> ToolResult | None:
    """A decided request as the tool result a resumed turn composes over.

    Pure, and built from the ROW's status alone (decision B): a caller names a
    request id, never an outcome, so an approval API that was wrong or lying
    cannot turn a rejection into a grant. `None` for every status that is not a
    recorded decision, which is what `resume.resume_turn` refuses on.

    The payload's ids are STRINGS because the whole result crosses LangGraph's
    serde as the resume value and is validated back in `await_approval`; a
    `uuid.UUID` in there would come back as something else.
    """
    row = DECIDED_RESULTS.get(status)
    if row is None:
        return None
    tool, ok = row
    return ToolResult(
        tool=tool,
        ok=ok,
        payload={"request_id": str(request_id), "status": status},
    )
