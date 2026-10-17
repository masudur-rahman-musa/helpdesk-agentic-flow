"""Decision and approver chain.

A self-requested admin role moves up one risk class instead of gaining an
extra approver, so the expiry cap and the chain follow from the class.

Nobody approves their own request. Any person seat that resolves to the
requester or the target is swapped, falling back to system owner, then
`it_admin`. An empty seat is not a conflict, so it is kept for the caller to
fill; but an empty seat is never picked as a replacement, since a chain
nobody can approve is worse than a shorter one.
"""

import uuid

from helpdesk.shared.policy.types import (
    ApproverSlot,
    Decision,
    Privilege,
    RequestSnapshot,
    RequiredApprover,
    RiskClass,
    SystemCategory,
)

_ESCALATION: dict[RiskClass, RiskClass] = {
    RiskClass.LOW: RiskClass.ELEVATED,
    RiskClass.ELEVATED: RiskClass.PRIVILEGED,
    RiskClass.PRIVILEGED: RiskClass.CRITICAL,
    RiskClass.CRITICAL: RiskClass.CRITICAL,
}

_THIRD_APPROVER: dict[SystemCategory, ApproverSlot | None] = {
    SystemCategory.PROD_INFRA: ApproverSlot.IT_ADMIN,
    SystemCategory.HRIS: ApproverSlot.HR,
    SystemCategory.OTHER: None,
}

_FALLBACK_SLOTS = (ApproverSlot.SYSTEM_OWNER, ApproverSlot.IT_ADMIN)

_ROLE_WIDE_SLOTS = frozenset({ApproverSlot.IT_ADMIN, ApproverSlot.HR})
"""Seats that stand for a group, not a person, so the engine never resolves
or swaps them. It cannot tell whether a party belongs to the group. Whoever
fills the seat later must refuse the requester and the target."""


def escalate_self_admin(snapshot: RequestSnapshot, risk_class: RiskClass) -> RiskClass:
    """Bump one class when someone asks for admin on themselves, so it is never `auto`."""
    if snapshot.is_self_request and snapshot.role.privilege is Privilege.ADMIN:
        return _ESCALATION[risk_class]
    return risk_class


def decide(risk_class: RiskClass) -> Decision:
    return Decision.AUTO if risk_class is RiskClass.LOW else Decision.APPROVAL


def _slot_user_id(snapshot: RequestSnapshot, slot: ApproverSlot) -> uuid.UUID | None:
    if slot is ApproverSlot.DEPARTMENT_MANAGER:
        return snapshot.target.department_manager_user_id
    if slot is ApproverSlot.SYSTEM_OWNER:
        return snapshot.role.system_owner_user_id
    return None


def _is_party_to_request(snapshot: RequestSnapshot, user_id: uuid.UUID | None) -> bool:
    return user_id is not None and user_id in {
        snapshot.requester_user_id,
        snapshot.target.user_id,
    }


def _base_chain(risk_class: RiskClass) -> tuple[ApproverSlot, ...]:
    if risk_class is RiskClass.LOW:
        return ()
    if risk_class is RiskClass.ELEVATED:
        return (ApproverSlot.DEPARTMENT_MANAGER,)
    return (ApproverSlot.DEPARTMENT_MANAGER, ApproverSlot.SYSTEM_OWNER)


def _chain_slots(snapshot: RequestSnapshot, risk_class: RiskClass) -> tuple[ApproverSlot, ...]:
    slots = _base_chain(risk_class)
    if risk_class is RiskClass.CRITICAL:
        third = _THIRD_APPROVER[snapshot.role.system_category]
        if third is not None:
            slots = (*slots, third)
    return slots


def _slot_is_a_party(snapshot: RequestSnapshot, slot: ApproverSlot) -> bool:
    """True when this slot is filled by the requester or by the target."""
    if slot in _ROLE_WIDE_SLOTS:
        return False
    return _is_party_to_request(snapshot, _slot_user_id(snapshot, slot))


def _is_usable_fallback(
    snapshot: RequestSnapshot, slot: ApproverSlot, others: list[ApproverSlot]
) -> bool:
    """A replacement must add a real approver: not already in the chain, not
    a party, and fillable (an ownerless system's owner seat is not)."""
    if slot in others:
        return False
    if slot in _ROLE_WIDE_SLOTS:
        return True
    user_id = _slot_user_id(snapshot, slot)
    return user_id is not None and not _is_party_to_request(snapshot, user_id)


def _substitute_parties(
    snapshot: RequestSnapshot, slots: tuple[ApproverSlot, ...]
) -> tuple[ApproverSlot, ...]:
    """Replace every slot filled by the requester or the target.

    With no usable fallback the slot is dropped, so a requester who manages
    the target and owns the system leaves only `it_admin` on a critical
    request.
    """
    chain: list[ApproverSlot | None] = list(slots)
    for index, slot in enumerate(slots):
        if not _slot_is_a_party(snapshot, slot):
            continue
        others = [other for other in chain if other is not None and other is not slot]
        chain[index] = next(
            (
                fallback
                for fallback in _FALLBACK_SLOTS
                if _is_usable_fallback(snapshot, fallback, others)
            ),
            None,
        )
    return tuple(slot for slot in chain if slot is not None)


def required_approvers(
    snapshot: RequestSnapshot, risk_class: RiskClass
) -> tuple[RequiredApprover, ...]:
    slots = _substitute_parties(snapshot, _chain_slots(snapshot, risk_class))
    return tuple(
        RequiredApprover(slot=slot, user_id=_slot_user_id(snapshot, slot)) for slot in slots
    )
