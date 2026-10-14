"""An earlier change §6: the execution node and its plain-language error table.

Expected implementation surface (the implementer must provide exactly this):

``agent_api.graph.plain_errors``
    ``PLAIN_ERRORS: Mapping[ErrorCode, str]`` — a row for EVERY one of the
        eight ``helpdesk.errors.ErrorCode`` members, read through no ``.get()``
        default (criterion 6). One plain sentence each, in this service's own
        words: never the server's message, never the exception's text, and
        naming no host, scheme, status code, port, tool name or tool argument.

``agent_api.graph.nodes.execute_tools``
    ``async def execute_tools(state: TurnState, deps: TurnDeps) -> TurnState``
        — calls every ``ValidatedCall`` on the state IN ORDER through
        ``deps.tools``, rebuilding each call's arguments through
        ``agent_api.tools.catalogue.TOOL_INPUTS``; writes one ``ToolResult``
        per call and increments ``tool_calls_made`` by the number of calls
        ATTEMPTED (decision F). A ``ToolError`` whose code is not ``INTERNAL``
        is recorded as ``ok=False`` with that code, the REMAINING calls of the
        round are dropped (decision B) and the turn continues; an ``INTERNAL``
        ends the turn as state — ``TurnOutcome.FAILED``, ``error_code``
        ``INTERNAL`` and the templated ``FAILURE_REPLY`` — with no further
        call made. The node never raises.

Cost: nothing at all. No Postgres, no Redis, no gateway, no graph and no MCP
session. Every case hands the node a ``TurnDeps`` whose ``db`` and ``model``
are ``None`` behind a cast, which is itself an assertion: a node that reached
for the database or the gateway raises ``AttributeError`` here rather than
quietly putting a store on the execution path.
"""

import ast
import asyncio
import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.agent_api.graph import plain_errors as plain_errors_module
from helpdesk.agent_api.graph.deps import TurnDeps
from helpdesk.agent_api.graph.nodes import execute_tools as execute_tools_module
from helpdesk.agent_api.graph.nodes.execute_tools import execute_tools
from helpdesk.agent_api.graph.nodes.extract_intent import FAILURE_REPLY
from helpdesk.agent_api.graph.plain_errors import PLAIN_ERRORS
from helpdesk.agent_api.graph.state import (
    Intent,
    IntentEntities,
    IntentExtraction,
    TurnOutcome,
    TurnState,
    ValidatedCall,
)
from helpdesk.agent_api.mcp_client import REMOTE_ERRORS, RemoteToolError, ToolCaller
from helpdesk.agent_api.tools.catalogue import IDEMPOTENCY_KEY, TOOL_INPUTS
from helpdesk.shared.errors import ErrorCode
from helpdesk.shared.gateway.client import ModelClient

NOW = datetime(2026, 6, 2, 10, 15, tzinfo=UTC)

SESSION_ID = uuid.UUID("11111111-1111-5111-8111-111111111111")
TENANT_ID = uuid.UUID("22222222-2222-5222-8222-222222222222")
USER_ID = uuid.UUID("33333333-3333-5333-8333-333333333333")
TARGET_USER_ID = uuid.UUID("44444444-4444-5444-8444-444444444444")
ROLE_ID = uuid.UUID("55555555-5555-5555-8555-555555555555")

MESSAGE = "I need access to fake payroll"

DERIVED_KEY = "obviously-fake-derived-key-0001"
"""Stands in for `derive_idempotency_key`'s output: this node never derives a
key, it carries the one `validate_tool_calls` already wrote into the call."""

CLASSIFY = "classify_access_request"
CREATE_REQUEST = "create_access_request"

NON_INTERNAL_CODES: tuple[ErrorCode, ...] = tuple(
    code for code in ErrorCode if code is not ErrorCode.INTERNAL
)
"""Seven of the eight: every code that is an answer a person can act on.

Derived from the enum rather than listed, so a ninth code added to the design
is executed by these rows on the day it lands instead of quietly falling into
the untested half of decision B.
"""


def _classify_call() -> ValidatedCall:
    """A read call, arguments already validated and dumped in json mode."""
    return ValidatedCall(
        tool=CLASSIFY,
        arguments={"target_user_id": str(TARGET_USER_ID), "role_id": str(ROLE_ID)},
    )


def _create_request_call() -> ValidatedCall:
    """A MUTATING call, carrying the key the agent derived (decision D).

    Deliberately second in every round below: the pair is a classification and
    the request it justifies, which is the exact case decision B's "drop the
    rest of the round" exists for.
    """
    return ValidatedCall(
        tool=CREATE_REQUEST,
        arguments={
            "target_user_id": str(TARGET_USER_ID),
            "role_id": str(ROLE_ID),
            "justification": "Obviously fake justification.",
            "requested_expiry_days": None,
            "citations": [],
            IDEMPOTENCY_KEY: DERIVED_KEY,
        },
    )


class ScriptedToolCaller:
    """A `ToolCaller` answering one scripted result per call, in order.

    A call beyond the script fails the test rather than repeating the last
    answer, so "the second call of the round was never made" is caught at the
    moment it is made and not only by a count afterwards.
    """

    def __init__(self, *answers: Mapping[str, Any] | BaseException) -> None:
        self._answers = list(answers)
        self.calls: list[tuple[str, BaseModel]] = []

    async def __aenter__(self) -> ToolCaller:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def call_tool(self, name: str, arguments: BaseModel) -> Mapping[str, Any]:
        self.calls.append((name, arguments))
        index = len(self.calls) - 1
        if index >= len(self._answers):
            raise AssertionError(
                f"the node made {len(self.calls)} tool calls; "
                f"only {len(self._answers)} were scripted"
            )
        answer = self._answers[index]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    @property
    def names(self) -> list[str]:
        return [name for name, _arguments in self.calls]


def _state(*validated: ValidatedCall, tool_calls_made: int = 0) -> TurnState:
    """A turn standing where `validate_tool_calls` accepted a round."""
    return TurnState(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        session_id=SESSION_ID,
        tenant_slug="fake-tenant",
        user_message=MESSAGE,
        extraction=IntentExtraction(
            intent=Intent.REQUEST_ACCESS,
            entities=IntentEntities(
                system="payroll",
                role=None,
                target_user=None,
                department=None,
                expiry_days=None,
                justification=None,
            ),
            confidence=0.95,
            clarifying_question=None,
        ),
        outcome=TurnOutcome.CONTINUE,
        round_number=1,
        validated=validated,
        tool_calls_made=tool_calls_made,
        started_at=NOW,
    )


def _execute(state: TurnState, caller: ScriptedToolCaller) -> TurnState:
    """`execute_tools` alone: no database, no gateway, no graph.

    `db` and `model` are `None` behind a cast, so an implementation that wrote
    a message row or called the model from this node raises `AttributeError`
    rather than passing quietly (the same shape `test_agent_planning.py` uses).
    """
    deps = TurnDeps(
        db=cast(AsyncSession, None),
        model=cast(ModelClient, None),
        tools=caller,
        now=lambda: NOW,
    )
    return asyncio.run(execute_tools(state, deps))


# --- criterion 1: every call, in order, counted as attempted -------------------


def test_a_round_of_two_makes_both_calls_in_order_and_records_both() -> None:
    """Catches a node that executes only the first validated call and reports
    the round done, and one that runs them in an order of its own — the round
    IS the plan, so a `create_access_request` made before the classification
    that justifies it is a request decided against nothing.

    The two payloads differ, so a node that wrote one `ToolResult` per call
    but paired them with the wrong call fails here as well."""
    caller = ScriptedToolCaller({"risk": "low"}, {"request_id": "fake-request-1"})

    state = _execute(_state(_classify_call(), _create_request_call()), caller)

    assert caller.names == [CLASSIFY, CREATE_REQUEST]
    assert [result.tool for result in state.tool_results] == [CLASSIFY, CREATE_REQUEST]
    assert [result.ok for result in state.tool_results] == [True, True]
    assert [result.payload for result in state.tool_results] == [
        {"risk": "low"},
        {"request_id": "fake-request-1"},
    ]
    assert [result.error_code for result in state.tool_results] == [None, None]
    assert state.outcome is TurnOutcome.CONTINUE


def test_tool_calls_made_rises_by_the_number_of_calls_attempted() -> None:
    """Decision F, from the side the cap reads. Catches a counter left at zero
    or reset per round: `may_plan_another_round` would then never see the
    budget spent, and the only thing left stopping the cycle would be the
    recursion limit.

    The state starts at a non-zero count, so a node that ASSIGNS the round's
    length instead of adding it fails this row — that implementation would
    hold a six-call turn at two forever."""
    caller = ScriptedToolCaller({"risk": "low"}, {"request_id": "fake-request-1"})

    state = _execute(_state(_classify_call(), _create_request_call(), tool_calls_made=3), caller)

    assert state.tool_calls_made == 5


def test_the_call_goes_out_as_the_catalogues_own_model_carrying_the_derived_key() -> None:
    """Criterion 1's "through `deps.tools`" half. The protocol takes a
    `BaseModel`, so a node handing it the raw `arguments` dict would fail at
    the real `McpToolCaller` and nowhere else — the stub in
    `test_agent_turn.py` would accept it.

    Also catches a node that rebuilds the arguments from `ProposedCall` or
    strips unknown-looking fields: `idempotency_key` is on the VALIDATED call
    only, and a mutating call that lost it is a replay the server cannot
    dedupe."""
    caller = ScriptedToolCaller({"risk": "low"}, {"request_id": "fake-request-1"})
    calls = (_classify_call(), _create_request_call())

    _execute(_state(*calls), caller)

    for (name, arguments), expected in zip(caller.calls, calls, strict=True):
        assert name == expected.tool
        assert isinstance(arguments, TOOL_INPUTS[expected.tool])
        # `exclude_unset`: the catalogue models carry optional fields with
        # defaults (`classify_access_request.requested_expiry_days`), so a plain
        # dump adds keys the validated call never named. What this case is about
        # is that every named argument arrives, the key included.
        assert arguments.model_dump(mode="json", exclude_unset=True) == expected.arguments
    assert caller.calls[1][1].model_dump(mode="json")[IDEMPOTENCY_KEY] == DERIVED_KEY


# --- criterion 2 and decision B: a typed failure is an answer ------------------


@pytest.mark.parametrize("code", NON_INTERNAL_CODES, ids=[c.value for c in NON_INTERNAL_CODES])
def test_a_typed_failure_records_its_code_drops_the_rest_and_continues(code: ErrorCode) -> None:
    """Decision B on the seven codes that are decisions a person can act on.

    Catches three implementations at once. One that ends the turn on any
    typed error replaces the policy engine's answer — a `POLICY_BLOCKED` or a
    `SOD_CONFLICT` the user could act on — with a generic failure. One that
    swallows the error and carries on with the round makes the second call of
    a plan whose first step did not happen. One that records `ok=True` with a
    code hands the composition a result it will read as a success.

    The scripted caller has NO second answer, so a node that made the second
    call fails at that call rather than by a count read afterwards."""
    caller = ScriptedToolCaller(REMOTE_ERRORS[code]("obviously fake server detail"))

    state = _execute(_state(_classify_call(), _create_request_call()), caller)

    assert caller.names == [CLASSIFY]
    assert len(state.tool_results) == 1
    assert state.tool_results[0].tool == CLASSIFY
    assert state.tool_results[0].ok is False
    assert state.tool_results[0].error_code is code
    assert state.tool_results[0].payload == {}
    assert state.tool_calls_made == 1
    assert state.outcome is TurnOutcome.CONTINUE
    assert state.error_code is None
    assert state.reply is None


def test_a_typed_failure_keeps_the_results_of_the_calls_that_already_ran() -> None:
    """The other side of "the rest of the round is dropped": what already
    happened is still what happened. Catches a node that throws the round's
    results away on the first failure, which would compose a reply as though
    the classification had never been made — and, worse, would let the next
    planning round propose it again against a turn that has no record of it."""
    caller = ScriptedToolCaller(
        {"risk": "low"},
        REMOTE_ERRORS[ErrorCode.POLICY_BLOCKED]("obviously fake server detail"),
    )

    state = _execute(_state(_classify_call(), _create_request_call()), caller)

    assert caller.names == [CLASSIFY, CREATE_REQUEST]
    assert [result.tool for result in state.tool_results] == [CLASSIFY, CREATE_REQUEST]
    assert [result.ok for result in state.tool_results] == [True, False]
    assert state.tool_results[1].error_code is ErrorCode.POLICY_BLOCKED
    assert state.tool_calls_made == 2
    assert state.outcome is TurnOutcome.CONTINUE


# --- criterion 3: the one code that is an outage -------------------------------


def test_an_internal_failure_ends_the_turn_and_makes_no_further_call() -> None:
    """Criterion 3, and the one code decision B exempts. Catches a node that
    treats `INTERNAL` like the other seven: the turn would compose a reply
    over a result carrying nothing worth composing, and the user would be
    answered about an outage as though it were a policy decision.

    Only the code separates this case from the parametrized rows above — the
    same first call, the same second call never made — so an implementation
    with no `INTERNAL` branch at all flips exactly this one.

    The templated reply, never the model's words and never the server's: what
    failed is the far side, so anything it said is what must not be shown."""
    caller = ScriptedToolCaller(REMOTE_ERRORS[ErrorCode.INTERNAL]("obviously fake server detail"))

    state = _execute(_state(_classify_call(), _create_request_call()), caller)

    assert caller.names == [CLASSIFY]
    assert state.outcome is TurnOutcome.FAILED
    assert state.error_code is ErrorCode.INTERNAL
    assert state.reply == FAILURE_REPLY
    assert state.tool_calls_made == 1


# --- criterion 6: the plain-language table -------------------------------------

_FORBIDDEN_SUBSTRINGS: tuple[str, ...] = ("http", "://", ".invalid", "localhost", "traceback")
_STATUS_OR_PORT = re.compile(r"\d{3,}")

SNAKE_CASE_ARGUMENTS: frozenset[str] = frozenset(
    name for model in TOOL_INPUTS.values() for name in model.model_fields if "_" in name
)
"""Every tool argument whose spelling is unambiguously internal.

The snake_case half only: a plain sentence may perfectly well contain the word
`reason` or `title`, but `target_user_id` and `idempotency_key` are this
system's vocabulary and belong in no chat window (criterion 6).
"""


def test_plain_errors_covers_exactly_the_eight_error_codes() -> None:
    """Criterion 6. Catches a table keyed only by the codes this plan's own
    cases exercise: read through a `.get(code, "something went wrong")`
    default, a `SOD_CONFLICT` the user could act on — the one refusal an
    auditor and a user both need in words — becomes the generic sentence, and
    nothing in the suite would see it. The same shape `REMOTE_ERRORS`,
    `INTENT_TOOLS` and `REQUIRED_ENTITIES` are pinned for."""
    assert set(PLAIN_ERRORS) == set(ErrorCode)
    assert len(PLAIN_ERRORS) == 8


@pytest.mark.parametrize("code", list(ErrorCode), ids=[c.value for c in ErrorCode])
def test_every_plain_sentence_is_this_services_own_words(code: ErrorCode) -> None:
    """Plan §5's fourth risk, swept row by row. Catches a table built from the
    exception's own text or from the server's message: those carry a host, a
    URL, a status code and the tool that raised, and this string goes into a
    prompt and from there into a chat window.

    A tool NAME is checked as well as a URL: "create_access_request failed" is
    internal vocabulary in front of a person who asked for payroll access."""
    sentence = PLAIN_ERRORS[code]

    assert sentence.strip() == sentence
    assert sentence
    lowered = sentence.lower()
    assert not [bad for bad in _FORBIDDEN_SUBSTRINGS if bad in lowered]
    assert _STATUS_OR_PORT.search(sentence) is None
    assert not [name for name in TOOL_INPUTS if name in lowered]
    assert not [name for name in SNAKE_CASE_ARGUMENTS if name in lowered]


def test_the_eight_sentences_are_eight_different_sentences() -> None:
    """The reason the table exists rather than one generic string. Catches a
    mapping whose eight rows all say "something went wrong": it would pass
    every criterion above and answer a `SOD_CONFLICT` — a conflict the person
    can resolve by asking for a different role — with nothing they can act on.
    """
    assert len({sentence for sentence in PLAIN_ERRORS.values()}) == 8


def test_the_plain_error_module_reads_its_table_through_no_default() -> None:
    """Criterion 6's "no `.get()`" half, statically. A `.get()` with a default
    is exactly the shape that makes a missing row invisible: the module keeps
    compiling, every existing case passes, and the next code added to SPEC
    §8.1 answers with whatever the default says.

    Read off the source rather than the behaviour because a default can only
    be seen when a row is MISSING, and the case above guarantees none is."""
    source = Path(str(plain_errors_module.__file__))
    tree = ast.parse(source.read_text())

    getters = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
    ]
    assert getters == []


# --- the node's reach ----------------------------------------------------------


def test_execute_tools_imports_no_store_and_no_gateway() -> None:
    """Criterion 1's "through `deps.tools`" half, statically. Catches a node
    that writes its own message row, audit row or model call while it runs the
    round: every effect of this turn other than the tool calls themselves
    belongs to a node the graph's edges can see, and a store reached from
    inside the execution cycle would be written once per ROUND with nothing
    counting it.

    The `None` deps in every case above catch the same thing at runtime; this
    catches an import added for a path no case happens to drive."""
    tree = ast.parse(Path(str(execute_tools_module.__file__)).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)

    forbidden = {"sqlalchemy", "httpx", "redis", "qdrant_client", "langgraph"}
    assert imported & forbidden == set()
    assert "helpdesk.shared.core.repository" not in imported


def test_the_scripted_caller_refuses_a_call_beyond_its_script() -> None:
    """Guard against a vacuous fixture: every "the rest of the round was not
    made" case above rests on the script running out, so a caller that quietly
    repeated its last answer would make those cases pass whatever the node
    did. Catches exactly that."""
    caller = ScriptedToolCaller({"risk": "low"})

    async def scenario() -> Sequence[object]:
        model = TOOL_INPUTS[CLASSIFY].model_validate(_classify_call().arguments)
        first = await caller.call_tool(CLASSIFY, model)
        with pytest.raises(AssertionError):
            await caller.call_tool(CLASSIFY, model)
        return [first]

    assert asyncio.run(scenario()) == [{"risk": "low"}]


def test_every_remote_error_class_carries_the_code_its_row_is_keyed_by() -> None:
    """A premise the decision-B rows are read off, asserted rather than
    assumed: those cases raise `REMOTE_ERRORS[code]` and
    then assert the recorded `error_code` is `code`, which proves nothing if
    the class were keyed under the wrong member. Catches a mis-keyed row that
    would otherwise make seven parametrized cases agree with themselves."""
    for code, error_class in REMOTE_ERRORS.items():
        assert issubclass(error_class, RemoteToolError)
        assert error_class.code is code
