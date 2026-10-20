"""How each turn ended, and how many turns it took to file.

Written before the implementation exists. Surface under test:

``helpdesk.shared.core.observability.metrics.build_metrics`` registers two
more metrics:
    ``helpdesk_turn_endings_total`` (counter, labels ``tenant`` and
        ``outcome``) with ``Metrics.turn_endings_total``;
    ``helpdesk_turns_to_file`` (histogram, label ``tenant``, buckets 1, 2, 3, 4,
        5, 6, 8, 10, 15) with ``Metrics.turns_to_file``.
``helpdesk.agent_api.agent.turn_metrics.record_turn_state(state)`` records them
beside the counters it already moves, once per finished turn:
    outcome is one of ``filed``, ``asked``, ``stopped``, ``refused``,
    ``failed``, ``unclear``; the FIRST match in the order failed, refused,
    stopped, unclear, filed, asked wins; a plain answer counts none.
    ``failed`` is outcome FAILED; ``refused`` is REFUSE; ``stopped`` is STOP;
    ``unclear`` is the best-reading flag; ``filed`` is
    ``postcheck.request_filed``; ``asked`` is CLARIFY, DISAMBIGUATE or CONFIRM.
    The histogram observes, when the turn filed a request, the user messages in
    the chat: the "user:" lines of ``state.history`` plus the message itself.

Pure: a state is a plain object and each case reads its own fresh
``CollectorRegistry`` swapped over the module-level pair, so every number is an
absolute value. No backing service.
"""

import uuid
from collections.abc import Iterator, Mapping
from typing import Any

import pytest
from prometheus_client import CollectorRegistry

from helpdesk.agent_api.agent.clarify_cap import QuestionKind
from helpdesk.agent_api.agent.state import ToolResult, TurnOutcome, TurnState
from helpdesk.agent_api.agent.turn_metrics import record_turn_state
from helpdesk.shared.core.errors import ErrorCode
from helpdesk.shared.core.observability import metrics

ENDINGS = "helpdesk_turn_endings_total"
TURNS_TO_FILE = "helpdesk_turns_to_file"
OUTCOMES = ("filed", "asked", "stopped", "refused", "failed", "unclear")

TENANT_ID = uuid.UUID("b1111111-1111-5111-8111-111111111111")
OTHER_TENANT_ID = uuid.UUID("b2222222-2222-5222-8222-222222222222")

FILED_RESULT = ToolResult(
    tool="create_access_request",
    ok=True,
    payload={"status": "pending", "decision": "approval", "risk_class": "elevated"},
)
CLASSIFY_ONLY = ToolResult(
    tool="classify_access_request",
    ok=True,
    payload={"decision": "auto", "risk_class": "low"},
)
"""A bare classification files nothing, so it must never count as filed."""


@pytest.fixture
def fresh_registry(monkeypatch: pytest.MonkeyPatch) -> Iterator[CollectorRegistry]:
    registry = CollectorRegistry()
    monkeypatch.setattr(metrics, "REGISTRY", registry)
    monkeypatch.setattr(metrics, "METRICS", metrics.build_metrics(registry))
    yield registry


def _state(
    outcome: TurnOutcome = TurnOutcome.DONE,
    *,
    tenant_id: uuid.UUID = TENANT_ID,
    **more: Any,
) -> TurnState:
    return TurnState(
        tenant_id=tenant_id,
        user_id=uuid.UUID(int=1),
        session_id=uuid.UUID(int=2),
        tenant_slug="fake-tenant",
        user_message="fake message",
        outcome=outcome,
        **more,
    )


def _endings(registry: CollectorRegistry) -> dict[tuple[str, str], float]:
    """Every ending sample as `(tenant, outcome) -> value`, labels checked."""
    found: dict[tuple[str, str], float] = {}
    names = {family.name for family in registry.collect()}
    assert "helpdesk_turn_endings" in names, "the ending counter is not registered"
    for family in registry.collect():
        for sample in family.samples:
            if sample.name != ENDINGS:
                continue
            assert set(sample.labels) == {"tenant", "outcome"}
            found[(sample.labels["tenant"], sample.labels["outcome"])] = sample.value
    return found


def _histogram(registry: CollectorRegistry) -> list[Any]:
    names = {family.name for family in registry.collect()}
    assert TURNS_TO_FILE in names, "the turns-to-file histogram is not registered"
    return [
        sample
        for family in registry.collect()
        for sample in family.samples
        if sample.name.startswith(TURNS_TO_FILE)
    ]


# --- the six endings ----------------------------------------------------------

ONE_TURN_PER_ENDING: tuple[tuple[str, TurnState], ...] = (
    ("failed", _state(TurnOutcome.FAILED, error_code=ErrorCode.INTERNAL)),
    ("refused", _state(TurnOutcome.REFUSE)),
    ("stopped", _state(TurnOutcome.STOP)),
    ("unclear", _state(TurnOutcome.ANSWER, best_reading=True)),
    ("filed", _state(TurnOutcome.DONE, tool_results=(FILED_RESULT,))),
    ("asked", _state(TurnOutcome.CLARIFY, question_kind=QuestionKind.DETAILS)),
)


def test_each_ending_counts_once_with_tenant_and_outcome(
    fresh_registry: CollectorRegistry,
) -> None:
    """Catches an ending that is missing, misspelt, counted twice, or labelled
    with anything but the tenant and the outcome (a user id or a message would
    be a person in the scrape).

    One turn per ending, all in one tenant. Each of the six outcome labels
    reads exactly 1 and nothing else moved, so a turn that counts under two
    endings (a failed turn also counted as asked) fails the total. Label names
    are checked inside the sample reader: exactly `tenant` and `outcome`.
    """
    for _name, state in ONE_TURN_PER_ENDING:
        record_turn_state(state)

    assert _endings(fresh_registry) == {(str(TENANT_ID), outcome): 1.0 for outcome in OUTCOMES}


@pytest.mark.parametrize(
    "outcome", [TurnOutcome.CLARIFY, TurnOutcome.DISAMBIGUATE, TurnOutcome.CONFIRM]
)
def test_a_question_of_any_of_the_three_kinds_counts_asked(
    fresh_registry: CollectorRegistry, outcome: TurnOutcome
) -> None:
    """Catches an `asked` that covers only a clarify: a disambiguation (which
    option?) and a confirmation ("I'll file this") are questions too, and each
    leaves the chat waiting on the person."""
    record_turn_state(_state(outcome))

    assert _endings(fresh_registry) == {(str(TENANT_ID), "asked"): 1.0}


def test_the_tenant_label_is_the_turns_own_tenant(fresh_registry: CollectorRegistry) -> None:
    """Catches a label taken from process context or a constant: two turns of two
    tenants must land under their own tenant ids."""
    record_turn_state(_state(TurnOutcome.REFUSE))
    record_turn_state(_state(TurnOutcome.REFUSE, tenant_id=OTHER_TENANT_ID))
    record_turn_state(_state(TurnOutcome.REFUSE, tenant_id=OTHER_TENANT_ID))

    assert _endings(fresh_registry) == {
        (str(TENANT_ID), "refused"): 1.0,
        (str(OTHER_TENANT_ID), "refused"): 2.0,
    }


@pytest.mark.parametrize(
    "state",
    [
        pytest.param(_state(TurnOutcome.DONE), id="answered"),
        pytest.param(_state(TurnOutcome.ANSWER), id="answer-with-no-tools"),
        pytest.param(_state(TurnOutcome.DONE, tool_results=(CLASSIFY_ONLY,)), id="classify-only"),
        pytest.param(
            _state(TurnOutcome.DONE, tool_results=(ToolResult(tool="search_policy", ok=False),)),
            id="a-failed-tool-call",
        ),
    ],
)
def test_a_plain_answer_counts_no_ending(
    fresh_registry: CollectorRegistry, state: TurnState
) -> None:
    """Catches a catch-all ending: an answered policy question, or a turn that
    only classified, is not filed, asked or anything else, so no ending counter
    moves and `helpdesk_turns_to_file` stays empty.

    The classify-only row stops "any ok tool result counts as filed", the
    mistake `postcheck.request_filed` was written to avoid.
    """
    record_turn_state(state)

    assert _endings(fresh_registry) == {}
    assert _histogram(fresh_registry) == []


# --- the order ----------------------------------------------------------------


def test_a_failed_turn_with_a_question_counts_failed(fresh_registry: CollectorRegistry) -> None:
    """Catches an order that lets a later ending win over `failed`: this turn
    failed AND carries a question, the unclear flag and a filed result, so any
    ending but `failed` that is checked first would be counted instead.

    Only `failed` moves, and the histogram stays empty: a failed turn is not a
    filing, whatever its tool results hold.
    """
    state = _state(
        TurnOutcome.FAILED,
        error_code=ErrorCode.INTERNAL,
        question_kind=QuestionKind.DETAILS,
        best_reading=True,
        tool_results=(FILED_RESULT,),
    )

    record_turn_state(state)

    assert _endings(fresh_registry) == {(str(TENANT_ID), "failed"): 1.0}
    assert _histogram(fresh_registry) == []


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        pytest.param(
            _state(TurnOutcome.REFUSE, best_reading=True, tool_results=(FILED_RESULT,)),
            "refused",
            id="refused-beats-unclear-and-filed",
        ),
        pytest.param(
            _state(TurnOutcome.STOP, best_reading=True, tool_results=(FILED_RESULT,)),
            "stopped",
            id="stopped-beats-unclear-and-filed",
        ),
        pytest.param(
            _state(TurnOutcome.ANSWER, best_reading=True, tool_results=(FILED_RESULT,)),
            "unclear",
            id="unclear-beats-filed",
        ),
        pytest.param(
            _state(TurnOutcome.CLARIFY, tool_results=(FILED_RESULT,)),
            "filed",
            id="filed-beats-asked",
        ),
        pytest.param(
            _state(TurnOutcome.AWAIT_APPROVAL),
            "filed",
            id="a-request-waiting-for-approval-is-filed",
        ),
    ],
)
def test_the_first_matching_ending_wins(
    fresh_registry: CollectorRegistry, state: TurnState, expected: str
) -> None:
    """Catches an order other than failed, refused, stopped, unclear, filed,
    asked. Each row has a later ending ALSO true, so only the order decides.

    The approval row pins `filed` for a request waiting on approvers: the
    request exists, so it was filed, although no tool result says so here.
    """
    record_turn_state(state)

    assert _endings(fresh_registry) == {(str(TENANT_ID), expected): 1.0}


# --- turns to file ------------------------------------------------------------


def test_turns_to_file_records_the_chat_length(fresh_registry: CollectorRegistry) -> None:
    """Catches a count of every message (the assistant's too), a count that
    leaves out the message being filed on, and a label other than the tenant.

    The chat holds three earlier user messages and three assistant replies, so
    the request filed on the fourth user message observes exactly 4: counting
    all lines gives 7, and counting only the history gives 3.
    """
    history = (
        "user: first",
        "assistant: question one",
        "user: second",
        "assistant: question two",
        "user: third",
        "assistant: summary",
    )

    record_turn_state(_state(TurnOutcome.DONE, history=history, tool_results=(FILED_RESULT,)))

    samples = _histogram(fresh_registry)
    by_name = {sample.name: sample for sample in samples if not sample.name.endswith("_bucket")}
    assert by_name[f"{TURNS_TO_FILE}_count"].value == 1.0
    assert by_name[f"{TURNS_TO_FILE}_sum"].value == 4.0
    for sample in samples:
        assert sample.labels.get("tenant") == str(TENANT_ID)
        assert set(sample.labels) <= {"tenant", "le"}


def test_a_request_filed_on_the_first_message_observes_one(
    fresh_registry: CollectorRegistry,
) -> None:
    """Catches an off-by-one at the low end: with an empty history the message
    itself is the only user message, so the value is 1, not 0."""
    record_turn_state(_state(TurnOutcome.DONE, tool_results=(FILED_RESULT,)))

    sums = [s.value for s in _histogram(fresh_registry) if s.name == f"{TURNS_TO_FILE}_sum"]
    assert sums == [1.0]


def test_the_turns_to_file_buckets_are_exactly_the_planned_ones(
    fresh_registry: CollectorRegistry,
) -> None:
    """Catches default or invented buckets: they are 1, 2, 3, 4, 5, 6, 8, 10, 15
    and the open one, so a chat of seven turns lands in `le=8` and a chat of
    sixteen only in `+Inf`.

    Compared as a whole mapping of upper bound to count after observing 4, so
    one extra or missing bound fails.
    """
    history = tuple(f"user: message {n}" for n in range(3))
    record_turn_state(_state(TurnOutcome.DONE, history=history, tool_results=(FILED_RESULT,)))

    buckets: Mapping[str, float] = {
        sample.labels["le"]: sample.value
        for sample in _histogram(fresh_registry)
        if sample.name == f"{TURNS_TO_FILE}_bucket"
    }
    assert buckets == {
        "1.0": 0.0,
        "2.0": 0.0,
        "3.0": 0.0,
        "4.0": 1.0,
        "5.0": 1.0,
        "6.0": 1.0,
        "8.0": 1.0,
        "10.0": 1.0,
        "15.0": 1.0,
        "+Inf": 1.0,
    }
