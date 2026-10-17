"""The decision card a turn's tool results show the client.

Pure, so both reply shapes build the same card. A payload missing a key gives
no card, never half a card. Reasons stay machine codes for the web app.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, ValidationError

from helpdesk.agent_api.agent.gates import CREATE_ACCESS_REQUEST
from helpdesk.agent_api.agent.state import ToolResult, TurnState
from helpdesk.shared.models.access import RequestStatus
from helpdesk.shared.policy.types import ApproverSlot, Decision, RequiredApprover, RiskClass

CLASSIFY_ACCESS_REQUEST = "classify_access_request"
"""The tool that gives the engine's verdict without filing anything."""

_IGNORE_EXTRAS = ConfigDict(extra="ignore", frozen=True)
"""Tools answer more than a card shows; the rest belongs to the approvals page."""


class CardApprover(BaseModel):
    """One seat in the chain, as a client may see it: the slot and nothing else."""

    model_config = ConfigDict(frozen=True)

    slot: ApproverSlot
    resolved: bool
    """True when the engine named a person for this seat. Group seats read false."""


class DecisionCard(BaseModel):
    """The card. `request_id` and `status` are `None` when nothing was filed."""

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
    """The classify tool's fields the card uses. All required, so a dropped key gives no card."""

    model_config = _IGNORE_EXTRAS

    decision: Decision
    risk_class: RiskClass
    reasons: list[str]
    required_approvers: list[RequiredApprover]
    effective_expiry_days: int


class _Created(_Classified):
    """The create tool's fields the card uses. `expires_at` is null while pending."""

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
    """The last successful answer from `tool`, checked, or `None`."""
    for result in reversed(results):
        if result.tool != tool or not result.ok:
            continue
        try:
            return model.model_validate(result.payload)
        except ValidationError:
            return None
    return None


def decision_card(state: TurnState) -> DecisionCard | None:
    """This turn's card, or `None`. A filed request wins over a classification."""
    created = _projected(state.tool_results, CREATE_ACCESS_REQUEST, _Created)
    if created is not None:
        return _card(created)
    classified = _projected(state.tool_results, CLASSIFY_ACCESS_REQUEST, _Classified)
    return None if classified is None else _card(classified)
