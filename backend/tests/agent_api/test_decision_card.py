"""An earlier change §6: the decision card projected off one turn's tool results.

Expected implementation surface (the implementer must provide exactly this):

``agent_api.decision_card`` (PURE: no session, no I/O, no clock)
    ``class CardApprover(BaseModel)`` — exactly ``slot`` (an
        ``ApproverSlot``) and ``resolved`` (a bool). No user id: a raw uuid
        renders nothing a person can read (decision C).
    ``class DecisionCard(BaseModel)`` — exactly the eight fields
        ``request_id``, ``status``, ``decision``, ``risk_class``, ``reasons``,
        ``approvers``, ``effective_expiry_days``, ``expires_at``.
    ``def decision_card(state: TurnState) -> DecisionCard | None`` — the LAST
        successful ``create_access_request`` result in ``state.tool_results``,
        falling back to ``classify_access_request`` (decision B), validated
        against the ``helpdesk.policy.types`` enums; ``None`` for anything it
        cannot read in full, and it raises nothing.

The wire shape these cases assert against is the card's own
``model_dump(mode="json")`` — the object ``chat_routes.ChatResponse`` and
``graph/build.py::_reply_data`` both carry under their fifth key ``decision``. The Python types the model uses are left to the
implementer; what is fixed here is the JSON a client reads.

Two facts about the CREATE path are fixed by §1 and worth stating once:
every one of the eight projected keys must be PRESENT in the payload (a
missing key is a ``None`` card, not a partial one), and ``expires_at`` may be
``null`` but may not be absent. The CLASSIFY path has no ``expires_at`` at all
in ``ClassifyAccessRequestOutput``, so its card carries ``None`` there, the
same as its ``request_id`` and its ``status``.

Cost and isolation. Nothing backing: no app, no Postgres, no Redis, no MCP
server and no gateway. Every case builds a frozen ``TurnState`` in memory and
calls one pure function, so the whole file runs in milliseconds and nothing
here needs a namespace.
"""

import json
import re
import uuid
from typing import Any

import pytest

from helpdesk.agent_api.agent.state import ToolResult, TurnState
from helpdesk.agent_api.agent.tool_args import SEARCH_POLICY
from helpdesk.agent_api.services.decision_card import CardApprover, DecisionCard, decision_card
from helpdesk.mcp_server.tools.access import CreateAccessRequestOutput
from helpdesk.shared.models.access import RequestStatus
from helpdesk.shared.policy.types import ApproverSlot, Decision, ReasonCode, RiskClass

# The MCP wire names, spelled the way `test_chat_route.py` spells its own.
CREATE_REQUEST = "create_access_request"
CLASSIFY_REQUEST = "classify_access_request"

CARD_FIELDS = {
    "request_id",
    "status",
    "decision",
    "risk_class",
    "reasons",
    "approvers",
    "effective_expiry_days",
    "expires_at",
}
"""§1's exact field set for `DecisionCard`, asserted as a set."""

PROJECTED_KEYS = frozenset(
    {
        "request_id",
        "status",
        "decision",
        "risk_class",
        "reasons",
        "required_approvers",
        "effective_expiry_days",
        "expires_at",
    }
)
"""The eight payload keys the projection reads.

`approvers` is built from `required_approvers` alone — `RequiredApprover`
already carries the `user_id` the engine either filled or did not — so
`approver_user_ids` is not read and no id crosses into the card.
"""

# Obviously fake ids, tests only.
REQUEST_ID = uuid.UUID("77777777-7777-5777-8777-777777777777")
SECOND_REQUEST_ID = uuid.UUID("88888888-8888-5888-8888-888888888888")
MANAGER_ID = uuid.UUID("99999999-9999-5999-8999-999999999999")
RULE_ID = uuid.UUID("11111111-1111-5111-8111-111111111111")
GRANT_ID = uuid.UUID("22222222-2222-5222-8222-222222222222")

TENANT_ID = uuid.UUID("33333333-3333-5333-8333-333333333333")
USER_ID = uuid.UUID("44444444-4444-5444-8444-444444444444")
SESSION_ID = uuid.UUID("55555555-5555-5555-8555-555555555555")

EXPIRES_AT = "2026-07-02T10:15:00Z"
"""Pydantic renders a UTC datetime back in exactly this spelling, so the
payload value and the card's JSON value compare directly."""

EXPIRY_DAYS = 30

REASONS = [ReasonCode.PRIVILEGE_ADMIN.value, f"{ReasonCode.SOD_CONFLICT.value}:{RULE_ID}"]

RESOLVED_APPROVERS: list[dict[str, str | None]] = [
    {"slot": ApproverSlot.DEPARTMENT_MANAGER.value, "user_id": str(MANAGER_ID)},
    {"slot": ApproverSlot.IT_ADMIN.value, "user_id": None},
]
"""One seat the engine filled and one group seat it never can (`it_admin`)."""

CARD_APPROVERS = [
    {"slot": ApproverSlot.DEPARTMENT_MANAGER.value, "resolved": True},
    {"slot": ApproverSlot.IT_ADMIN.value, "resolved": False},
]

_UUID_ANYWHERE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _create_payload(**overrides: Any) -> dict[str, Any]:
    """A full `create_access_request` answer, every field the tool emits."""
    payload: dict[str, Any] = {
        "request_id": str(REQUEST_ID),
        "status": RequestStatus.PENDING.value,
        "risk_class": RiskClass.PRIVILEGED.value,
        "decision": Decision.APPROVAL.value,
        "reasons": list(REASONS),
        "required_approvers": [dict(row) for row in RESOLVED_APPROVERS],
        "approver_user_ids": [str(MANAGER_ID)],
        "effective_expiry_days": EXPIRY_DAYS,
        "sod_conflicts": [],
        "policy_refs": ["FAKE-POL-001 §4"],
        "grant_id": str(GRANT_ID),
        "expires_at": EXPIRES_AT,
    }
    payload.update(overrides)
    return payload


def _classify_payload(**overrides: Any) -> dict[str, Any]:
    """A full `classify_access_request` answer: the engine's verdict, nothing filed."""
    payload: dict[str, Any] = {
        "risk_class": RiskClass.CRITICAL.value,
        "decision": Decision.BLOCKED.value,
        "reasons": [ReasonCode.CRITICAL_SYSTEM.value],
        "required_approvers": [dict(row) for row in RESOLVED_APPROVERS],
        "effective_expiry_days": 7,
        "sod_conflicts": [],
        "policy_refs": ["FAKE-POL-001 §9"],
    }
    payload.update(overrides)
    return payload


def _ok(tool: str, payload: dict[str, Any]) -> ToolResult:
    return ToolResult(tool=tool, ok=True, payload=payload)


def _page_payload() -> dict[str, Any]:
    """One `search_policy` answer: a turn that retrieved and decided nothing."""
    return {"citations": [], "refused": False}


def _state(*results: ToolResult) -> TurnState:
    """One turn carrying exactly these results and nothing else that matters."""
    return TurnState(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        session_id=SESSION_ID,
        tenant_slug="fake-tenant",
        user_message="Please give me read access to fake payroll.",
        tool_results=results,
    )


# --- the created request ------------------------------------------------------


def test_a_created_request_projects_its_eight_fields() -> None:
    """Catches a card that carries something other than §1's eight fields —
    a ninth key such as `approver_user_ids` or `policy_refs` reaching the wire,
    or one of the six spec fields missing — and one that renders a value the
    tool never said: every value is compared against the payload it came from,
    so a projection reading the wrong key answers the wrong string here.

    The field set is asserted exactly, so an addition is as red as a removal.
    """
    payload = _create_payload()

    card = decision_card(_state(_ok(CREATE_REQUEST, payload)))

    assert card is not None
    assert set(DecisionCard.model_fields) == CARD_FIELDS
    assert card.model_dump(mode="json") == {
        "request_id": payload["request_id"],
        "status": payload["status"],
        "decision": payload["decision"],
        "risk_class": payload["risk_class"],
        "reasons": payload["reasons"],
        "approvers": CARD_APPROVERS,
        "effective_expiry_days": payload["effective_expiry_days"],
        "expires_at": payload["expires_at"],
    }


def test_the_projected_keys_are_the_tools_own_output_fields() -> None:
    """Catches the plan's first risk — a tool-side rename of an output field
    emptying the card silently: the eight names are checked against
    `CreateAccessRequestOutput.model_fields` read off the model, so the rename
    goes red HERE rather than as a blank card in a browser.

    The second half catches the other direction: a projection that reads a key
    outside those eight (`approver_user_ids`, `grant_id`) refuses a payload
    carrying exactly the eight and nothing else.
    """
    assert len(PROJECTED_KEYS) == 8
    assert set(CreateAccessRequestOutput.model_fields) >= PROJECTED_KEYS

    trimmed = {key: value for key, value in _create_payload().items() if key in PROJECTED_KEYS}
    assert set(trimmed) == PROJECTED_KEYS, (
        "premise: the trimmed payload must carry every projected key, or this "
        "case would prove nothing about what the projection reads"
    )

    card = decision_card(_state(_ok(CREATE_REQUEST, trimmed)))

    assert card is not None
    assert card.model_dump(mode="json")["request_id"] == trimmed["request_id"]


def test_the_last_created_request_of_several_is_the_card() -> None:
    """Catches a projection that scans forward and stops at the first create
    result: a turn that filed twice has one current request, and a card naming
    the earlier one sends the user to a request that has been superseded.

    Both results are `ok` and both are complete, so nothing but the ORDER can
    decide between them.
    """
    first = _create_payload(request_id=str(REQUEST_ID), effective_expiry_days=7)
    last = _create_payload(request_id=str(SECOND_REQUEST_ID), effective_expiry_days=EXPIRY_DAYS)

    card = decision_card(
        _state(_ok(CREATE_REQUEST, first), _ok(CREATE_REQUEST, last)),
    )

    assert card is not None
    dumped = card.model_dump(mode="json")
    assert dumped["request_id"] == str(SECOND_REQUEST_ID)
    assert dumped["effective_expiry_days"] == EXPIRY_DAYS


# --- the classification, and which of the two wins ----------------------------


def test_a_classified_request_projects_a_card_with_no_request_id_and_no_status() -> None:
    """Catches a projection that refuses every turn which filed nothing
    (decision B's rejected alternative): a blocked classification is exactly
    the case a card explains best, and rendering nothing for it leaves the user
    with a refusal and no reason.

    Also catches a projection that INVENTS the two fields a classification
    cannot have — a fabricated request id, or a `pending` status for a request
    that was never filed.
    """
    payload = _classify_payload()

    card = decision_card(_state(_ok(CLASSIFY_REQUEST, payload)))

    assert card is not None
    assert card.model_dump(mode="json") == {
        "request_id": None,
        "status": None,
        "decision": payload["decision"],
        "risk_class": payload["risk_class"],
        "reasons": payload["reasons"],
        "approvers": CARD_APPROVERS,
        "effective_expiry_days": payload["effective_expiry_days"],
        "expires_at": None,
    }


@pytest.mark.parametrize(
    "create_first",
    [True, False],
    ids=["create-then-classify", "classify-then-create"],
)
def test_a_created_request_wins_over_a_classification(create_first: bool) -> None:
    """Catches a projection whose preference is really its POSITION — "last
    result wins" or "first result wins" — rather than the tool name: the filed
    request is the only one of the two carrying a request id, so a card built
    from the classification loses the id a client needs.

    Both tuple orders are driven, so neither positional rule passes both rows.
    """
    created = _ok(CREATE_REQUEST, _create_payload())
    classified = _ok(CLASSIFY_REQUEST, _classify_payload())
    results = (created, classified) if create_first else (classified, created)

    card = decision_card(_state(*results))

    assert card is not None
    dumped = card.model_dump(mode="json")
    assert dumped["request_id"] == str(REQUEST_ID)
    assert dumped["status"] == RequestStatus.PENDING.value
    assert dumped["risk_class"] == RiskClass.PRIVILEGED.value
    assert dumped["decision"] == Decision.APPROVAL.value


# --- the approvers ------------------------------------------------------------


def test_an_approver_is_resolved_exactly_when_the_engine_filled_an_id() -> None:
    """Catches a `resolved` flag that is really a constant — true for every
    seat, or true only where the slot is a person-shaped one — and, the half
    decision C exists for, a card that carries the approver's uuid onto the
    wire: the two seats differ ONLY in whether `user_id` was filled, so
    nothing but reading that field can tell them apart.

    The filled id is searched for in the rendered JSON, and so is any uuid at
    all, so a projection that keeps the id under another name is caught too.
    """
    payload = _create_payload()

    card = decision_card(_state(_ok(CREATE_REQUEST, payload)))

    assert card is not None
    assert set(CardApprover.model_fields) == {"slot", "resolved"}
    approvers = card.model_dump(mode="json")["approvers"]
    assert approvers == CARD_APPROVERS

    rendered = json.dumps(approvers)
    assert str(MANAGER_ID) not in rendered
    assert _UUID_ANYWHERE.search(rendered) is None


# --- the unhappy paths --------------------------------------------------------


def test_a_failed_tool_result_contributes_no_card() -> None:
    """Catches a projection that reads `payload` before it reads `ok`: a
    refused call carries no decision the engine ever made, so a card built
    from whatever its payload happens to hold states a verdict as fact.

    The payload is COMPLETE and valid, so `ok` is the only field that can
    decide this case.
    """
    result = ToolResult(tool=CREATE_REQUEST, ok=False, payload=_create_payload())

    assert decision_card(_state(result)) is None


def test_a_turn_with_no_access_result_has_no_card() -> None:
    """Catches a projection that matches on the SHAPE of a payload rather than
    on the tool name, and one that answers a card for every turn: a retrieval
    turn decided nothing about access, and a card rendered under it would be a
    verdict with no decision behind it."""
    assert decision_card(_state(_ok(SEARCH_POLICY, _page_payload()))) is None


@pytest.mark.parametrize("missing", sorted(PROJECTED_KEYS))
def test_a_payload_missing_a_key_answers_none(missing: str) -> None:
    """Catches a projection that trusts the payload it was handed — `.get()`
    with a default, or a model whose fields are all optional — and answers a
    HALF card: a card with no risk class, or with no expiry, renders a blank
    row the user reads as a fact about their request.

    One row per projected key, so a projection that guards seven of the eight
    is still red. Nothing may be raised either: a tool result is data crossing
    the MCP wire, and a turn must not die because one key moved.
    """
    payload = _create_payload()
    del payload[missing]

    assert decision_card(_state(_ok(CREATE_REQUEST, payload))) is None


@pytest.mark.parametrize(
    "payload",
    [
        _create_payload(decision="maybe"),
        _create_payload(risk_class="severe"),
        _create_payload(
            required_approvers=[{"slot": "chief_of_staff", "user_id": str(MANAGER_ID)}]
        ),
    ],
    ids=["decision", "risk_class", "approver-slot"],
)
def test_a_value_outside_its_enum_answers_none(payload: dict[str, Any]) -> None:
    """Catches a projection that copies the payload's strings through without
    validating them: a `decision` no client's switch handles, or an approver
    slot no label exists for, renders as an empty card field rather than as the
    missing answer it is.

    Every other field of each row is valid and present, so the key named in the
    row id is the only thing that can decide it — the
    missing-key guard cannot, because nothing is missing.
    """
    assert decision_card(_state(_ok(CREATE_REQUEST, payload))) is None
