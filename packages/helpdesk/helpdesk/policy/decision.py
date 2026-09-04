"""Decision and approver chain (the design).

Two rules bend the plain table. A self-requested admin role escalates one
class rather than gaining an extra approver: one lever,
and the expiry cap and the chain then follow from the class on their own.
And nobody approves their own request.

That second rule is an invariant over the whole chain, not a rule about the
manager: the design names the manager because that is the common case, but a
system owner who is the requester would be approving their own request just
as much. So every slot resolving to the requester or to the person the
access is for is replaced, falling through system owner to `it_admin`. The
test that matters is the property, not the case list — no approver's
`user_id` is ever the requester or the target.

A slot is only replaced when it resolves to one of those two people. An
unfilled seat is not a conflict of interest, so a department with no manager
keeps the slot with no id and lets the caller resolve it. The reverse does
not hold: an unfilled seat is never *chosen* as a replacement, because a
chain nobody can approve is worse than a shorter one.

Approvers are slots, optionally carrying a resolved user id (decision D).
The engine is pure and cannot look up "an it_admin"; keeping the slot
explicit is also what makes the substitution explainable downstream.
"""

import uuid

from helpdesk.policy.types import (
    ApproverSlot,
    Decision,
    Privilege,
    RequestSnapshot,
    RequiredApprover,
    RiskClass,
    SystemCategory,
)

# Total by contract, though `admin` is `privileged` at minimum, so only the
# last two entries are reachable from `escalate_self_admin`.
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
"""Slots that stand for a group, not a person, so they are always fillable.

They are NOT verified against the requester. Whether the requester belongs to
the group is not in the snapshot and the engine could not act on it anyway:
one member being a party does not disqualify the rest, and the engine cannot
see how many members there are. So the self-approval guard stops at the group
boundary, and the caller resolving the seat must exclude the requester and the
target from it. Recorded in an earlier change §5 as owed to row 11; do not read the
absence of a substitution here as a statement that the seat is safe."""


def escalate_self_admin(snapshot: RequestSnapshot, risk_class: RiskClass) -> RiskClass:
    """Bump one class when someone asks for admin on themselves.

    Admin is already `privileged` at minimum, so this moves `privileged` to
    `critical` and leaves `critical` where it is — a self-requested admin
    role can never be `auto`.
    """
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
    """A replacement must add an approver, not repeat or erase one.

    So it must not already be in the chain (that would collapse two approvers
    into one person and quietly halve the control), must not be a party, and
    must actually be fillable — a `system_owner` slot on an ownerless system
    would leave a request nobody can approve.
    """
    if slot in others:
        return False
    if slot in _ROLE_WIDE_SLOTS:
        return True
    user_id = _slot_user_id(snapshot, slot)
    return user_id is not None and not _is_party_to_request(snapshot, user_id)


def _substitute_parties(
    snapshot: RequestSnapshot, slots: tuple[ApproverSlot, ...]
) -> tuple[ApproverSlot, ...]:
    """Replace, in place, every slot filled by the requester or the target.

    If no fallback is usable the slot is dropped rather than filled with
    someone disqualified. The floor is one approver, not two: a requester who
    both manages the target and owns the system leaves only `it_admin` on a
    `critical` request. That is the honest answer for a pure engine — every
    other seat is filled by a party — and row 11 owns resolving a delegate.
    Pinned by `test_a_colliding_critical_request_bottoms_out_at_one_approver`.
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
