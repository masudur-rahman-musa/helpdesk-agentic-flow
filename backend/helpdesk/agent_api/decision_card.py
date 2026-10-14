"""The decision card one turn's tool results carry to a client (the design).

Pure: a `TurnState` in, a `DecisionCard | None` out. No session, no I/O, no
clock and no directory read, so the card can be built wherever the finished
state is — `chat_routes._rendered` and `graph/build.py::_reply_data` both do,
which is what keeps their two shapes one shape (19c decision G1).

The engine has already decided every field. `create_access_request` answers
the request it filed; `classify_access_request` answers the same verdict with
nothing filed, which is exactly the turn a card explains best (an earlier change
decision B) — so a classification projects a card whose `request_id` and
`status` are `None`, and a filed request wins wherever a turn holds both.

A tool result crossing the MCP wire is DATA, never a shape to trust: every
payload is validated against the tool's own field types before anything is
rendered, and a payload missing a projected key or carrying a value outside
its enum answers `None` rather than a half card. Nothing here raises —
`test_decision_card.py::test_a_payload_missing_a_key_answers_none` and
`::test_a_value_outside_its_enum_answers_none` drive both, one row per key
and one per enum.

Reasons stay MACHINE CODES (the design, decision E): turning `SOD_CONFLICT:{id}`
into a sentence is the web app's job, in the language its labels are written
in. No approver's user id reaches the wire either (decision C) — a raw uuid
renders nothing a person can read, and `resolved` is the only thing a card
needs to say "an it_admin" against a named person, pinned by
`::test_an_approver_is_resolved_exactly_when_the_engine_filled_an_id`.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, ValidationError

from helpdesk.agent_api.graph.gates import CREATE_ACCESS_REQUEST
from helpdesk.agent_api.graph.state import ToolResult, TurnState
from helpdesk.shared.models.access import RequestStatus
from helpdesk.shared.policy.types import ApproverSlot, Decision, RequiredApprover, RiskClass

CLASSIFY_ACCESS_REQUEST = "classify_access_request"
"""The tool that answers the engine's verdict without filing anything.

The sibling of `gates.CREATE_ACCESS_REQUEST`, which is imported rather than
respelled here: the two names are what this projection matches on, and a
second spelling of either would be a tool name nothing holds true.
"""

_IGNORE_EXTRAS = ConfigDict(extra="ignore", frozen=True)
"""The tool answers more than the card shows (`policy_refs`, `grant_id`,
`approver_user_ids`, `sod_conflicts`). Ignoring them is decision D: those
belong to `/approvals`' full trace, not to a chat card."""


class CardApprover(BaseModel):
    """One seat in the chain, as a client may see it: the slot and nothing else."""

    model_config = ConfigDict(frozen=True)

    slot: ApproverSlot
    resolved: bool
    """True exactly when the engine filled a user id for this seat. A group
    seat (`it_admin`, `hr`) names nobody and reads false, and so does a
    person-shaped seat the directory could not resolve."""


class DecisionCard(BaseModel):
    """the design's six card fields, plus the two that identify a filed request.

    `request_id` and `status` are `None` for a classification, which filed
    nothing; the other six are answered by both tools. Citations are NOT here
    — they already ride beside this object on both shapes.
    """

    model_config = ConfigDict(frozen=True)

    request_id: uuid.UUID | None
    status: RequestStatus | None
    decision: Decision
    risk_class: RiskClass
    reasons: list[str]
    approvers: list[CardApprover]
    effective_expiry_days: int
    expires_at: datetime | None


class _Classified(BaseModel):
    """`ClassifyAccessRequestOutput`'s five projected fields, field for field.

    Every one is required, so a renamed or dropped key is a `None` card rather
    than a card with a blank row in it.
    """

    model_config = _IGNORE_EXTRAS

    decision: Decision
    risk_class: RiskClass
    reasons: list[str]
    required_approvers: list[RequiredApprover]
    effective_expiry_days: int


class _Created(_Classified):
    """`CreateAccessRequestOutput`'s eight projected fields.

    `expires_at` is required and nullable: an auto-granted request carries the
    grant's expiry and a pending one carries nothing yet, but a payload with no
    such key at all is a shape this projection does not know.
    """

    request_id: uuid.UUID
    status: RequestStatus
    expires_at: datetime | None


def _card(projected: _Classified) -> DecisionCard:
    """The validated payload as the card, with the approvers narrowed."""
    created = projected if isinstance(projected, _Created) else None
    return DecisionCard(
        request_id=None if created is None else created.request_id,
        status=None if created is None else created.status,
        decision=projected.decision,
        risk_class=projected.risk_class,
        reasons=list(projected.reasons),
        approvers=[
            CardApprover(slot=approver.slot, resolved=approver.user_id is not None)
            for approver in projected.required_approvers
        ],
        effective_expiry_days=projected.effective_expiry_days,
        expires_at=None if created is None else created.expires_at,
    )


def _projected(
    results: tuple[ToolResult, ...], tool: str, model: type[_Classified]
) -> _Classified | None:
    """The LAST successful answer from `tool`, validated, or `None`.

    Backwards, not forwards: a turn may call the same tool twice, and the card
    belongs to the last thing that happened — a card naming a superseded
    request sends the user somewhere that no longer describes them
    (`test_decision_card.py::test_the_last_created_request_of_several_is_the_card`).

    `ok` is read before `payload`: a refused call carries no decision the
    engine ever made, so rendering whatever its payload holds would state a
    verdict as fact (`::test_a_failed_tool_result_contributes_no_card`).
    """
    for result in reversed(results):
        if result.tool != tool or not result.ok:
            continue
        try:
            return model.model_validate(result.payload)
        except ValidationError:
            return None
    return None


def decision_card(state: TurnState) -> DecisionCard | None:
    """This turn's card, or `None` where nothing decided anything about access.

    A filed request wins over a classification whatever their order in
    `tool_results` — the match is on the TOOL NAME, not on a position, because
    the two answer the same `decision` field and only one of them carries a
    request id
    (`test_decision_card.py::test_a_created_request_wins_over_a_classification`,
    driven over both tuple orders). A turn that called neither answers `None`,
    which both routes render as `decision: null`.
    """
    created = _projected(state.tool_results, CREATE_ACCESS_REQUEST, _Created)
    if created is not None:
        return _card(created)
    classified = _projected(state.tool_results, CLASSIFY_ACCESS_REQUEST, _Classified)
    return None if classified is None else _card(classified)
