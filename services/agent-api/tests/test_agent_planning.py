"""An earlier change §6: validation of a planned round, and the derived idempotency key.

Expected implementation surface (the implementer must provide exactly this):

``agent_api.graph.state``
    ``class ProposedCall(BaseModel)`` (frozen) — ``tool: str``,
        ``arguments: dict[str, Any]``. What the MODEL said, unchecked.
    ``class ToolPlan(BaseModel)`` (frozen) — ``calls: list[ProposedCall]``.
        The schema ``plan_tools`` hands to ``complete_json``.
    ``class ValidatedCall(BaseModel)`` (frozen) — ``tool: str``,
        ``arguments: dict[str, Any]``. The arguments a call actually goes out
        with, so a mutating tool's ``idempotency_key`` is IN here and never in
        ``ProposedCall``.
    ``TurnState`` gains ``proposed: tuple[ProposedCall, ...] = ()``,
        ``validated: tuple[ValidatedCall, ...] = ()``,
        ``tool_results: tuple[ToolResult, ...] = ()``,
        ``tool_calls_made: int = 0``, ``tokens_used: int = 0``,
        ``started_at: datetime | None = None``, ``round_number: int = 0``,
        ``planning_prompt_version: str | None = None``.

``agent_api.graph.keys`` (PURE)
    ``def derive_idempotency_key(*, session_id: uuid.UUID, user_message: str,
        round_number: int, index: int, tool_name: str) -> str`` — a ``uuid5``
        over exactly those five values (decision D).
    ``def check_idempotency_key(value: str) -> str`` — refuses anything the
        ``idempotency_keys.key`` column cannot hold, raising a ``ToolError``
        whose ``code`` is ``ErrorCode.VALIDATION``, BEFORE the key can reach
        the wire.

``agent_api.graph.nodes.validate_tool_calls``
    ``async def validate_tool_calls(state: TurnState, deps: TurnDeps)
        -> TurnState`` — accepts a round only if EVERY proposal names a tool in
        the intent's ``INTENT_TOOLS`` row and validates against that tool's
        planner-facing shape; one failure rejects the WHOLE round (decision C),
        leaves ``validated`` empty and records ``TurnOutcome.REFUSE``.

Cost: nothing. No Postgres, no Redis, no gateway, no graph. The node is handed
a ``TurnDeps`` whose four members are ``None`` behind a cast, which is itself
an assertion: a validator that reached for the database, the model client or
the tool caller raises ``AttributeError`` here instead of passing quietly.
"""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from agent_api.graph.deps import TurnDeps
from agent_api.graph.keys import check_idempotency_key, derive_idempotency_key
from agent_api.graph.nodes.validate_tool_calls import validate_tool_calls
from agent_api.graph.state import (
    Intent,
    IntentEntities,
    IntentExtraction,
    ProposedCall,
    TurnOutcome,
    TurnState,
)
from agent_api.mcp_client import ToolCaller
from helpdesk.errors import ErrorCode, ToolError
from helpdesk.gateway.client import ModelClient
from helpdesk.models.idempotency import KEY_LENGTH
from sqlalchemy.ext.asyncio import AsyncSession

NOW = datetime(2026, 6, 2, 10, 15, tzinfo=UTC)

SESSION_ID = uuid.UUID("11111111-1111-5111-8111-111111111111")
TENANT_ID = uuid.UUID("22222222-2222-5222-8222-222222222222")
USER_ID = uuid.UUID("33333333-3333-5333-8333-333333333333")
TARGET_USER_ID = uuid.UUID("44444444-4444-5444-8444-444444444444")
ROLE_ID = uuid.UUID("55555555-5555-5555-8555-555555555555")
DEPARTMENT_ID = uuid.UUID("66666666-6666-5666-8666-666666666666")
MANAGER_ID = uuid.UUID("77777777-7777-5777-8777-777777777777")

MESSAGE = "I need access to fake payroll"

IDEMPOTENCY_KEY = "idempotency_key"

_NO_DEPS = TurnDeps(
    db=cast(AsyncSession, None),
    model=cast(ModelClient, None),
    tools=cast(ToolCaller, None),
    now=lambda: NOW,
)
"""Deps a pure node may hold and must never touch.

`validate_tool_calls` decides with the state and `gates.py` alone (plan §2), so
every member here is `None`: an implementation that opened a session, called
the gateway or reached the MCP caller raises `AttributeError` rather than
quietly making this file depend on the compose stack."""


def _state(
    intent: Intent,
    *proposed: ProposedCall,
    message: str = MESSAGE,
    round_number: int = 1,
) -> TurnState:
    """A turn standing where `plan_tools` has just written its proposals."""
    return TurnState(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        session_id=SESSION_ID,
        tenant_slug="fake-tenant",
        user_message=message,
        extraction=IntentExtraction(
            intent=intent,
            entities=IntentEntities(),
            confidence=1.0,
        ),
        round_number=round_number,
        proposed=proposed,
    )


def _validate(state: TurnState) -> TurnState:
    return asyncio.run(validate_tool_calls(state, _NO_DEPS))


def _create_ticket(title: str = "Fake payroll access") -> ProposedCall:
    """A proposal every planning intent's row carries, with legal arguments."""
    return ProposedCall(
        tool="create_ticket",
        arguments={"kind": "general", "title": title, "body": "Obviously fake ticket body."},
    )


def _classify() -> ProposedCall:
    return ProposedCall(
        tool="classify_access_request",
        arguments={"target_user_id": str(TARGET_USER_ID), "role_id": str(ROLE_ID)},
    )


# --- decision C: what a round is refused for ----------------------------------


def test_a_proposal_naming_an_unregistered_tool_is_rejected() -> None:
    """Catches a validator that looks the tool up with `.get()` and reads a
    miss as "no argument model, so nothing to check" — a name the model
    invented would then be handed on to 17d and fail mid-turn as `INTERNAL`.

    No kill claim for the registration check on its own: the intent's row can
    only ever contain catalogued names, so the row check refuses this case
    too. What it pins is the OUTCOME an invented
    tool name must get, which is a refused round and nothing validated."""
    state = _state(
        Intent.REQUEST_ACCESS,
        ProposedCall(tool="delete_all_the_things", arguments={}),
    )

    result = _validate(state)

    assert result.outcome is TurnOutcome.REFUSE
    assert result.validated == ()


def test_a_registered_tool_outside_the_intents_row_is_rejected() -> None:
    """Decision A, and the only case that kills the ROW check alone: the tool
    IS catalogued and its arguments are perfectly valid, so registration and
    argument validation both pass this proposal. Catches a validator
    that checks the catalogue and never `INTENT_TOOLS` — a password-reset turn
    could then offboard somebody, which is precisely the prompt-injection case
    decision A exists to remove."""
    state = _state(
        Intent.RESET_PASSWORD,
        ProposedCall(
            tool="offboard_user",
            arguments={"user_id": str(TARGET_USER_ID), "end_date": "2026-07-01"},
        ),
    )

    result = _validate(state)

    assert result.outcome is TurnOutcome.REFUSE
    assert result.validated == ()


def test_a_proposal_missing_a_required_argument_is_rejected() -> None:
    """The only case that kills ARGUMENT validation alone: `onboard_user` is
    catalogued and is in the `onboard` row, so both other guards pass it, and
    only the catalogue model can see that `manager_user_id` is absent. Catches
    a validator that checks names and never shapes — the call would reach the
    server and be refused there, after a round trip, as a `VALIDATION` the user
    reads as a broken assistant."""
    state = _state(
        Intent.ONBOARD,
        ProposedCall(
            tool="onboard_user",
            arguments={
                "email": "fake.joiner@fake-tenant.example.test",
                "display_name": "Fake Joiner",
                "department_id": str(DEPARTMENT_ID),
                "start_date": "2026-07-01",
            },
        ),
    )

    result = _validate(state)

    assert result.outcome is TurnOutcome.REFUSE
    assert result.validated == ()


def test_a_proposal_carrying_an_unknown_argument_is_rejected() -> None:
    """Catches a validator that validates with `extra="ignore"`: a `tenant_id`
    the model put on a mutation is a model that believes it is acting in
    another tenant, and dropping it silently runs the effect somewhere the
    proposal did not name. Every declared argument is present and legal here,
    so only the extra field can decide this case."""
    state = _state(
        Intent.OPEN_TICKET,
        ProposedCall(
            tool="create_ticket",
            arguments={
                "kind": "general",
                "title": "Fake ticket",
                "body": "Obviously fake ticket body.",
                "tenant_id": str(TENANT_ID),
            },
        ),
    )

    result = _validate(state)

    assert result.outcome is TurnOutcome.REFUSE
    assert result.validated == ()


def test_a_proposal_that_names_its_own_idempotency_key_is_rejected() -> None:
    """Decision D's hard edge, and plan §5's third risk. The planner-facing
    schema omits the field, so a model naming one is an UNKNOWN argument, not
    an override. Catches a validator that validates against the wire model
    instead: the model's own string would then be honoured, and a model
    reusing one key across two different requests is the
    `IDEMPOTENCY_CONFLICT` path taken by accident — or worse, a second request
    silently replayed as the first.

    Distinct from the unknown-argument case above because that one dies on a
    field no schema anywhere declares, while this one dies on a field the WIRE
    model does declare and requires."""
    state = _state(
        Intent.OPEN_TICKET,
        ProposedCall(
            tool="create_ticket",
            arguments={
                "kind": "general",
                "title": "Fake ticket",
                "body": "Obviously fake ticket body.",
                IDEMPOTENCY_KEY: "a-key-the-model-made-up",
            },
        ),
    )

    result = _validate(state)

    assert result.outcome is TurnOutcome.REFUSE
    assert result.validated == ()


def test_one_malformed_proposal_takes_the_whole_round_with_it() -> None:
    """Decision C as an exact count. Catches a validator that DROPS the bad
    proposal and runs the rest: the two valid calls here are a classification
    and a ticket, so a half-executed round would file a ticket about a request
    that was never created. The two survivors are legal on their own, which is
    what makes the zero the assertion — a filtering validator returns two."""
    state = _state(
        Intent.REQUEST_ACCESS,
        _classify(),
        _create_ticket(),
        ProposedCall(tool="create_access_request", arguments={"role_id": str(ROLE_ID)}),
    )

    result = _validate(state)

    assert result.outcome is TurnOutcome.REFUSE
    assert result.validated == ()


# --- the accepted round -------------------------------------------------------


def test_a_valid_round_of_two_yields_two_validated_calls_in_the_proposed_order() -> None:
    """Catches a validator that returns its results out of order — through a
    set, a dict keyed by tool name, or a comprehension over an unordered
    collection. Order is the plan: `classify_access_request` before
    `create_access_request` is a decision made before a request is filed, and
    the reverse files first and reasons afterwards. Two DIFFERENT tools, so a
    dict-keyed implementation loses nothing here and only the sequence can
    fail."""
    state = _state(Intent.REQUEST_ACCESS, _classify(), _create_ticket())

    result = _validate(state)

    assert result.outcome is TurnOutcome.CONTINUE
    assert [call.tool for call in result.validated] == ["classify_access_request", "create_ticket"]


def test_an_empty_round_is_accepted_and_validates_nothing() -> None:
    """The turn's ordinary end: a planner with nothing left to call proposes
    an empty round. Catches a validator that treats "no proposals" as a
    failure and refuses a turn that did everything right — every retrieving
    `request_access` turn would then end on the refusal template.

    An earlier change re-points the accepted branch: the empty round is still accepted
    and still validates nothing, but it now records `DONE` rather than
    `CONTINUE`, because the edge after this node has to tell "nothing left to
    call, compose" from "calls to execute" and a single value cannot. Recorded
    in an earlier change's Deviations."""
    state = _state(Intent.REQUEST_ACCESS)

    result = _validate(state)

    assert result.outcome is TurnOutcome.DONE
    assert result.validated == ()


def test_a_validated_mutating_call_carries_the_agents_derived_key() -> None:
    """Decision D. Catches a validator that leaves the key off entirely (the
    server refuses the call for a missing required field, after a round trip)
    and one that mints a fresh `uuid4` — the random key is the failure
    `idempotency_keys` exists to prevent, because an earlier change resumes the same
    turn in another process and would file the same ticket twice under two
    keys. Only the DERIVED value satisfies this assertion; any random one
    fails it."""
    state = _state(Intent.OPEN_TICKET, _create_ticket(), round_number=1)

    result = _validate(state)

    assert result.outcome is TurnOutcome.CONTINUE
    assert len(result.validated) == 1
    assert result.validated[0].arguments[IDEMPOTENCY_KEY] == derive_idempotency_key(
        session_id=SESSION_ID,
        user_message=MESSAGE,
        round_number=1,
        index=0,
        tool_name="create_ticket",
    )


def test_two_mutating_calls_in_one_round_get_two_different_keys() -> None:
    """Catches a derivation that leaves the proposal's INDEX out of the five
    values: two tickets planned in one round would share one key, and the
    second call would be answered from the first's stored response — one
    ticket where the plan asked for two, with a success the user believes.
    Same tool, same round, same message, so only the index differs."""
    state = _state(Intent.OPEN_TICKET, _create_ticket("First fake"), _create_ticket("Second fake"))

    result = _validate(state)

    assert result.outcome is TurnOutcome.CONTINUE
    keys = [call.arguments[IDEMPOTENCY_KEY] for call in result.validated]
    assert len(set(keys)) == 2


def test_a_validated_read_call_carries_no_idempotency_key() -> None:
    """Catches a validator that stamps a key onto EVERY call. A read tool's
    input ignores extra fields on the server, so the key would be dropped
    there in silence and nothing downstream would ever report it — while the
    agent had spent a derivation claiming a key that guards nothing."""
    state = _state(Intent.CHECK_MY_ACCESS, ProposedCall(tool="list_my_access", arguments={}))

    result = _validate(state)

    assert result.outcome is TurnOutcome.CONTINUE
    assert len(result.validated) == 1
    assert IDEMPOTENCY_KEY not in result.validated[0].arguments


# --- decision D: the derivation itself ----------------------------------------


def _key(**overrides: Any) -> str:
    fields: dict[str, Any] = {
        "session_id": SESSION_ID,
        "user_message": MESSAGE,
        "round_number": 1,
        "index": 0,
        "tool_name": "create_ticket",
    }
    fields.update(overrides)
    return derive_idempotency_key(**fields)


def test_the_same_five_values_derive_the_same_key_every_time() -> None:
    """Decision D's whole point. Catches a key built from a `uuid4`, a clock
    reading or an `id()` — anything that differs between two derivations.
    An earlier change resumes a checkpointed run in ANOTHER process, so a key that is
    not a pure function of the turn's own values files the same access request
    twice."""
    assert _key() == _key()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session_id", uuid.UUID("99999999-9999-5999-8999-999999999999")),
        ("user_message", "a different fake message"),
        ("round_number", 2),
        ("index", 1),
        ("tool_name", "create_access_request"),
    ],
)
def test_changing_any_one_of_the_five_values_changes_the_key(field: str, value: Any) -> None:
    """Five rows because a derivation that silently ignores ONE of them is the
    defect nobody sees: dropping the tool name makes a classification and a
    request share a key, dropping the round makes a replanned round replay the
    first round's answer, dropping the session makes two people's identical
    messages collide. Each row varies exactly one value, so it fails only when
    that value is missing from the digest."""
    assert _key(**{field: value}) != _key()


def test_the_derived_key_fits_the_column_it_is_stored_in() -> None:
    """a project rule on the value the agent itself produces: catches a
    derivation that returns something longer than the store can hold (a full
    digest, a joined string), which would abort AFTER the effect had run and
    surface as a database error rather than the design's typed code."""
    key = _key()
    assert 0 < len(key) <= KEY_LENGTH


def test_the_key_check_accepts_a_key_of_exactly_the_column_length() -> None:
    """The inclusive half of the boundary, a project rule and 2: catches a
    pre-check written `>= KEY_LENGTH`, which refuses the longest key the
    column can hold and would fail turns the store would have accepted."""
    key = "k" * KEY_LENGTH

    assert check_idempotency_key(key) == key
    assert KEY_LENGTH == 128


def test_a_key_one_character_over_the_column_length_is_refused_as_validation() -> None:
    """The exclusive half, and the typed code with it: catches a check that
    lets an oversized key through to the wire, where the guard's own
    length-check would refuse it after a round trip, and a check that raises
    a bare `ValueError` — free text across a boundary the design says carries
    a code."""
    with pytest.raises(ToolError) as excinfo:
        check_idempotency_key("k" * (KEY_LENGTH + 1))

    assert excinfo.value.code is ErrorCode.VALIDATION


def test_an_empty_key_is_refused_as_validation() -> None:
    """The other end of the same bound. Catches a check written as
    `len(value) > KEY_LENGTH` alone: an empty string is storable, so nothing
    downstream refuses it, and every call made in that turn would claim the
    same empty key — one replay for every mutation the turn plans."""
    with pytest.raises(ToolError) as excinfo:
        check_idempotency_key("")

    assert excinfo.value.code is ErrorCode.VALIDATION
