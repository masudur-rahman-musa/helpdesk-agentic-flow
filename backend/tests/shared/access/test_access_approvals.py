"""The approval slot state machine and the queue.

Surface under test (written before it exists):

``helpdesk.access.approvals``
    ``SlotDecision(StrEnum)`` — ``APPROVE = "approve"``, ``REJECT = "reject"``.
    ``SlotState(BaseModel)`` — frozen, ``extra="forbid"``: ``slot``,
    ``user_id``, ``decision``, ``decided_by_user_id``, ``decided_at``,
    ``comment``, the last five defaulting to ``None``.
    ``GROUP_SLOTS: frozenset[ApproverSlot]`` — exactly ``{it_admin, hr}``.
    ``RequestNotPendingError(ToolError)`` — ``POLICY_BLOCKED``.
    ``NotAnApproverError(ToolError)`` — ``FORBIDDEN``.
    ``parse_slots`` / ``dump_slots`` — the ``access_requests.approvals`` JSONB
    the request flow writes, read back and written out; ``may_fill``,
    ``first_fillable_index``, ``chain_is_complete``, ``is_rejected``,
    ``decide_slot`` — the pure state machine; ``lock_request`` (``SELECT …
    FOR UPDATE``, tenant-scoped) and ``list_pending_for_approver`` (
    pending, in scope, AND carrying an open slot this caller may fill).

``helpdesk.access.requests``
    ``set_request_status(session, request_id, *, status, approvals=None)`` —
    flushed, not committed, through the tenant-scoped repository.

The seeded database is ``test_access_snapshot.py``'s
session-scoped ``access_db_url`` fixture, imported rather than rebuilt.
Per-test isolation inside it is a namespace: each case owns seeded users
nothing else approves for, and every planted row carries a unique
justification. Identities are recomputed from ``scripts.seed.generate_tenant``
— deterministic uuid5 ids, never hardcoded UUIDs. The pure state-machine
cases touch no database at all.

No pytest-asyncio: each case is a sync function driving one ``asyncio.run``.
"""

import asyncio
import contextlib
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import pytest
from pydantic import ValidationError

from helpdesk.shared.access.approvals import (
    GROUP_SLOTS,
    NotAnApproverError,
    RequestNotPendingError,
    SlotDecision,
    SlotState,
    chain_is_complete,
    decide_slot,
    dump_slots,
    first_fillable_index,
    is_rejected,
    list_pending_for_approver,
    lock_request,
    may_fill,
    parse_slots,
    request_in_scope,
)
from helpdesk.shared.access.directory import SimulatedDirectory
from helpdesk.shared.access.requests import set_request_status
from helpdesk.shared.auth.rbac import Principal, Scope
from helpdesk.shared.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.shared.core.errors import ErrorCode
from helpdesk.shared.crud import Repository
from helpdesk.shared.models import AccessRequest, RequestStatus
from helpdesk.shared.policy.types import ApproverSlot

# Imported, not redefined: one seeded database for the whole session, shared
# with `test_access_snapshot.py` and `test_access_execute.py`.
from tests.shared.access.test_access_snapshot import (
    NOW,
    OTHER_TENANT_ROWS,
    ROWS,
    access_db_url,  # noqa: F401
)

DECIDED_AT = NOW

# --- identities for the pure cases -------------------------------------------
#
# Pure state-machine cases have no directory and no tenant, so these are plain
# opaque ids generated for this run: nothing here is seed data.
CALLER = uuid.uuid4()
OTHER_PERSON = uuid.uuid4()
REQUESTER_ID = uuid.uuid4()
TARGET_ID = uuid.uuid4()

NAMED_SLOTS: tuple[ApproverSlot, ...] = (
    ApproverSlot.DEPARTMENT_MANAGER,
    ApproverSlot.SYSTEM_OWNER,
)
GROUP_SLOT_MEMBERS: tuple[ApproverSlot, ...] = (ApproverSlot.IT_ADMIN, ApproverSlot.HR)
ALL_SLOTS: tuple[ApproverSlot, ...] = (*NAMED_SLOTS, *GROUP_SLOT_MEMBERS)

# The principal a group seat names, per slot: `it_admin` fills `it_admin` and
# nothing else.
PRINCIPAL_FOR_SLOT: dict[ApproverSlot, Principal] = {
    ApproverSlot.IT_ADMIN: Principal.IT_ADMIN,
    ApproverSlot.HR: Principal.HR,
}

# --- seeded identities for the database cases --------------------------------
#
# Production indexes 0-3 belong to `test_access_snapshot.py` and 10-14 to
# `test_access_execute.py`; this module owns 20 and up, so no case here can
# alter what another module reads.
QUEUE_APPROVER = ROWS.users_by_department["Production"][20]
QUEUE_TARGET_ONE = ROWS.users_by_department["Production"][21]
QUEUE_TARGET_TWO = ROWS.users_by_department["Production"][22]
DEPARTMENT_TARGET = ROWS.users_by_department["Production"][23]
GROUP_TARGET = ROWS.users_by_department["Production"][24]
STATUS_TARGET = ROWS.users_by_department["Production"][25]
LOCK_TARGET = ROWS.users_by_department["Production"][26]
PARTY_TARGET = ROWS.users_by_department["Production"][27]
SYSTEM_TARGET = ROWS.users_by_department["Production"][28]
DECIDED_TARGET = ROWS.users_by_department["Production"][29]
NON_PENDING_TARGET = ROWS.users_by_department["Production"][30]
OUTSIDER_TARGET = ROWS.users_by_department["Sales"][10]
REQUESTER = ROWS.users_by_department["Production"][31]

PRODUCTION_MANAGER = ROWS.users_by_department["Production"][0]
NOT_THE_MANAGER = ROWS.users_by_department["Production"][32]

# The `prod-k8s` owner is an IT user with the `system_owner` principal, so the
# union case has one caller whose two scopes really do reach different rows.
PROD_K8S_OWNER = ROWS.system_owners["prod-k8s"]
IT_ADMIN_USER = ROWS.users_by_department["IT"][19]
IT_TARGET = ROWS.users_by_department["IT"][20]

OWNED_SYSTEM_ROLE = ROWS.role_ids[("prod-k8s", "deployer")]
# Owned by a different IT user, so an `own_systems` filter cannot produce it.
UNOWNED_ROLE = ROWS.role_ids[("crm", "editor")]
PLAIN_ROLE = ROWS.role_ids[("badge", "holder")]
OTHER_ROLE = ROWS.role_ids[("m365", "user")]

# The second seeded tenant has its own department names ("Operations", not
# "Sales"): the point of these two is only that they belong to a tenant this
# session is not bound to.
OTHER_TENANT_REQUESTER = OTHER_TENANT_ROWS.users_by_department["Operations"][2]
OTHER_TENANT_TARGET = OTHER_TENANT_ROWS.users_by_department["Operations"][3]
OTHER_TENANT_ROLE = next(iter(OTHER_TENANT_ROWS.role_ids.values()))


def _open(slot: ApproverSlot, user_id: uuid.UUID | None = None) -> SlotState:
    """An undecided seat, exactly as the request flow writes it."""
    return SlotState(slot=slot, user_id=user_id)


def _decided(
    slot: ApproverSlot,
    user_id: uuid.UUID | None = None,
    decision: SlotDecision = SlotDecision.APPROVE,
) -> SlotState:
    return SlotState(
        slot=slot,
        user_id=user_id,
        decision=decision,
        decided_by_user_id=OTHER_PERSON,
        decided_at=DECIDED_AT,
        comment=None,
    )


def _fillable(
    slot: SlotState,
    *,
    caller_user_id: uuid.UUID = CALLER,
    caller_principals: frozenset[Principal] = frozenset(),
    requester_user_id: uuid.UUID = REQUESTER_ID,
    target_user_id: uuid.UUID = TARGET_ID,
) -> bool:
    return may_fill(
        slot,
        caller_user_id=caller_user_id,
        caller_principals=caller_principals,
        requester_user_id=requester_user_id,
        target_user_id=target_user_id,
    )


# --- the constants and the typed codes ----------------------------------------


def test_the_group_slots_are_exactly_it_admin_and_hr() -> None:
    """Catches a set that has drifted from the approval chain's two role-wide seats.

    `department_manager` sliding into it would let ANY caller holding some
    principal fill a seat that names one person; `hr` sliding out would make
    the HRIS seat a named slot with no id, fillable by nobody in a different
    way than the design records. Asserted as the exact set,
    not by membership, so both directions fail here.
    """
    assert frozenset({ApproverSlot.IT_ADMIN, ApproverSlot.HR}) == GROUP_SLOTS


def test_the_slot_decision_values_are_exactly_approve_and_reject() -> None:
    """Catches a renamed or extra member: these strings land in the
    `approvals` JSONB, so a third value would be a stored decision the
    chain-complete predicate has never seen."""
    assert {member.value for member in SlotDecision} == {"approve", "reject"}


def test_the_approval_errors_carry_their_spec_error_codes() -> None:
    """Pins both codes exactly.

    `POLICY_BLOCKED` on a row that is no longer pending is the whole point of
    the chosen error: `FORBIDDEN` would tell an approver who simply arrived second
    that they lack permission, and the audit log would file a race as a
    denial. Inheriting `ToolError` without setting `code` yields `INTERNAL`,
    which is what this catches.
    """
    assert RequestNotPendingError.code is ErrorCode.POLICY_BLOCKED
    assert NotAnApproverError.code is ErrorCode.FORBIDDEN


# --- reading and writing the stored chain --------------------------------------


def test_the_chain_plan_11b_wrote_parses_with_every_decision_field_unset() -> None:
    """Catches a parser that cannot read the rows already in the database:
    the request flow writes `{"slot", "user_id"}` and nothing else, so a model
    requiring `decision` or `decided_at` would fail on every existing
    pending request rather than reading it as an open seat."""
    raw: list[dict[str, Any]] = [
        {"slot": "department_manager", "user_id": str(CALLER)},
        {"slot": "it_admin", "user_id": None},
    ]

    slots = parse_slots(raw)

    assert slots == (
        SlotState(slot=ApproverSlot.DEPARTMENT_MANAGER, user_id=CALLER),
        SlotState(slot=ApproverSlot.IT_ADMIN, user_id=None),
    )
    assert all(slot.decision is None for slot in slots)
    assert all(slot.decided_by_user_id is None for slot in slots)
    assert all(slot.decided_at is None for slot in slots)
    assert all(slot.comment is None for slot in slots)


def test_an_unknown_key_in_a_stored_slot_is_refused() -> None:
    """Catches `extra="ignore"` on the stored shape: a key nobody reads is a
    decision (or a forged approver) silently dropped on the next write-back,
    and the chain would round-trip smaller than it went in."""
    with pytest.raises(ValidationError):
        parse_slots([{"slot": "it_admin", "user_id": None, "approved": True}])


def test_dumped_slots_parse_back_to_the_same_chain() -> None:
    """Catches a dump that is not JSON-mode — a raw `uuid.UUID` or a
    `datetime` in the value would either fail the JSONB write or come back as
    something `parse_slots` cannot read, which is a decided chain that reads
    as open on the next approval."""
    slots = (
        SlotState(
            slot=ApproverSlot.DEPARTMENT_MANAGER,
            user_id=CALLER,
            decision=SlotDecision.APPROVE,
            decided_by_user_id=CALLER,
            decided_at=DECIDED_AT,
            comment="fake approval note",
        ),
        _open(ApproverSlot.IT_ADMIN),
    )

    dumped = dump_slots(slots)

    assert all(isinstance(entry, dict) for entry in dumped)
    assert isinstance(dumped[0]["user_id"], str)
    assert isinstance(dumped[0]["decided_at"], str)
    assert parse_slots(dumped) == slots


def test_a_slot_state_is_frozen() -> None:
    """Catches a mutable slot: `decide_slot` returning the same objects
    mutated in place would leave a caller holding what looks like the
    before-state and is the after-state."""
    slot = _open(ApproverSlot.HR)
    with pytest.raises(Exception):  # noqa: B017 - frozen models raise a validation error
        setattr(slot, "decision", SlotDecision.APPROVE)  # noqa: B010 - static assignment is rejected


# --- eligibility: the named seats ----------------------------------------------


@pytest.mark.parametrize("slot", NAMED_SLOTS)
def test_a_named_seat_is_fillable_only_by_the_person_it_names(slot: ApproverSlot) -> None:
    """Catches a named seat treated as a group seat — anyone with the right
    principal filling a `department_manager` slot that names one person — and
    a seat matched on the slot kind alone, which would let any approver fill
    any named seat."""
    assert _fillable(_open(slot, CALLER)) is True
    assert _fillable(_open(slot, OTHER_PERSON)) is False


@pytest.mark.parametrize("slot", NAMED_SLOTS)
def test_a_named_seat_with_no_resolved_person_is_fillable_by_nobody(slot: ApproverSlot) -> None:
    """Catches `None == None` eligibility: the engine leaves an unfillable
    named seat (a department with no manager) with a null id, and a caller
    whose own id is compared against it loosely would inherit the seat.

    The caller here holds every principal, so a group-seat rule leaking onto
    a named slot is caught too.
    """
    assert _fillable(_open(slot, None), caller_principals=frozenset(Principal)) is False


@pytest.mark.parametrize("slot", NAMED_SLOTS)
def test_a_named_seat_needs_no_principal_at_all(slot: ApproverSlot) -> None:
    """Catches an eligibility rule that requires a group principal for every
    seat: the person the chain NAMES is the approver, and demanding
    `it_admin` of them would make named seats unfillable in practice."""
    assert _fillable(_open(slot, CALLER), caller_principals=frozenset()) is True


# --- eligibility: the group seats ----------------------------------------------


@pytest.mark.parametrize("slot", GROUP_SLOT_MEMBERS)
def test_a_group_seat_is_fillable_only_by_a_holder_of_its_own_principal(
    slot: ApproverSlot,
) -> None:
    """Catches a group seat matched against the wrong principal or against
    "any group principal": an `it_admin` filling an `hr` seat defeats the
    HRIS control the approval chain adds the seat for, and the reverse defeats the
    prod-infra one. Both directions are asserted, so neither can pass by
    being the only case."""
    own = PRINCIPAL_FOR_SLOT[slot]
    other = PRINCIPAL_FOR_SLOT[
        ApproverSlot.HR if slot is ApproverSlot.IT_ADMIN else ApproverSlot.IT_ADMIN
    ]

    assert _fillable(_open(slot), caller_principals=frozenset({own})) is True
    assert _fillable(_open(slot), caller_principals=frozenset({other})) is False


@pytest.mark.parametrize("slot", GROUP_SLOT_MEMBERS)
def test_a_platform_admin_holding_neither_group_principal_fills_neither_seat(
    slot: ApproverSlot,
) -> None:
    """Catches "the widest principal may do anything": the permission matrix gives
    `platform_admin` the `any` SCOPE on `approve_request`, which says which
    requests they may reach, not which SEATS they may occupy. A
    platform_admin filling an `hr` seat would be the approval chain silently
    shortened for the one principal that most needs auditing."""
    assert _fillable(_open(slot), caller_principals=frozenset({Principal.PLATFORM_ADMIN})) is False


@pytest.mark.parametrize("slot", GROUP_SLOT_MEMBERS)
def test_a_group_seat_ignores_a_resolved_id_it_never_carries(slot: ApproverSlot) -> None:
    """Catches a group seat that falls through to the named-seat rule when it
    happens to carry an id: the seat stands for the group, so a caller
    holding the principal fills it and a caller named on it without the
    principal does not."""
    assert (
        _fillable(
            _open(slot, OTHER_PERSON), caller_principals=frozenset({PRINCIPAL_FOR_SLOT[slot]})
        )
        is True
    )
    assert _fillable(_open(slot, CALLER), caller_principals=frozenset()) is False


# --- eligibility: nobody approves their own request (the owed marker) -----


@pytest.mark.parametrize("slot", ALL_SLOTS)
@pytest.mark.parametrize("role", ["requester", "target"])
def test_a_party_to_the_request_fills_no_seat_of_any_kind(slot: ApproverSlot, role: str) -> None:
    """Catches the party exclusion being applied to named seats only, which
    is exactly the hole `policy/decision.py::_ROLE_WIDE_SLOTS` records: the
    engine cannot see whether the requester is an `it_admin`, so if this
    resolver does not exclude them, an it_admin requests access for
    themselves and signs their own `critical` chain.

    Every slot kind is crossed with both parties rather than sampled: an
    exclusion that covers `department_manager` and forgets `hr` would pass a
    single representative case. The caller is given the seat's own principal
    and is named on the seat, so eligibility would otherwise be True and only
    the party rule can refuse.
    """
    caller_principals = frozenset(PRINCIPAL_FOR_SLOT.values())
    requester_user_id = CALLER if role == "requester" else REQUESTER_ID
    target_user_id = CALLER if role == "target" else TARGET_ID

    assert (
        _fillable(
            _open(slot, CALLER),
            caller_principals=caller_principals,
            requester_user_id=requester_user_id,
            target_user_id=target_user_id,
        )
        is False
    )


@pytest.mark.parametrize("slot", ALL_SLOTS)
def test_a_caller_who_is_neither_party_still_fills_the_seat(slot: ApproverSlot) -> None:
    """The other half of the pair above: catches a party rule so broad it
    refuses everybody (an exclusion comparing the wrong ids would make every
    request unapprovable, and every refusal test would still pass)."""
    caller_principals = frozenset(PRINCIPAL_FOR_SLOT.values())

    assert _fillable(_open(slot, CALLER), caller_principals=caller_principals) is True


# --- eligibility: a decided seat -----------------------------------------------


@pytest.mark.parametrize("slot", ALL_SLOTS)
@pytest.mark.parametrize("decision", list(SlotDecision))
def test_a_decided_seat_is_never_fillable_again(slot: ApproverSlot, decision: SlotDecision) -> None:
    """Catches eligibility that looks only at who the caller is: without the
    decided check, the same approver could be recorded twice on one seat, and
    a chain of two seats would complete on one person's two clicks."""
    seat = SlotState(
        slot=slot,
        user_id=CALLER,
        decision=decision,
        decided_by_user_id=CALLER,
        decided_at=DECIDED_AT,
        comment=None,
    )

    assert _fillable(seat, caller_principals=frozenset(PRINCIPAL_FOR_SLOT.values())) is False


# --- picking the seat ----------------------------------------------------------


def test_the_first_fillable_seat_is_the_first_open_one_the_caller_may_fill() -> None:
    """Catches an index taken from the first OPEN seat rather than the first
    open seat this caller may fill: here seat 0 is open and belongs to
    somebody else, so an implementation ignoring eligibility records the
    caller against a seat that names another person."""
    slots = (
        _open(ApproverSlot.DEPARTMENT_MANAGER, OTHER_PERSON),
        _open(ApproverSlot.SYSTEM_OWNER, CALLER),
        _open(ApproverSlot.IT_ADMIN),
    )

    index = first_fillable_index(
        slots,
        caller_user_id=CALLER,
        caller_principals=frozenset(),
        requester_user_id=REQUESTER_ID,
        target_user_id=TARGET_ID,
    )

    assert index == 1


def test_a_decided_seat_is_skipped_when_picking_the_next_one() -> None:
    """Catches a picker that stops at the first seat matching the caller
    regardless of its decision: the caller already decided seat 0, and
    returning it again would overwrite a recorded decision."""
    slots = (
        _decided(ApproverSlot.DEPARTMENT_MANAGER, CALLER),
        _open(ApproverSlot.SYSTEM_OWNER, CALLER),
    )

    index = first_fillable_index(
        slots,
        caller_user_id=CALLER,
        caller_principals=frozenset(),
        requester_user_id=REQUESTER_ID,
        target_user_id=TARGET_ID,
    )

    assert index == 1


def test_a_caller_matching_no_open_seat_gets_no_index() -> None:
    """Catches a picker that falls back to 0 (or to the last seat) when
    nothing matches: `None` is what the handler turns into `FORBIDDEN`, and a
    fallback index would record a stranger against somebody else's seat."""
    slots = (
        _open(ApproverSlot.DEPARTMENT_MANAGER, OTHER_PERSON),
        _open(ApproverSlot.HR),
    )

    index = first_fillable_index(
        slots,
        caller_user_id=CALLER,
        caller_principals=frozenset({Principal.IT_ADMIN}),
        requester_user_id=REQUESTER_ID,
        target_user_id=TARGET_ID,
    )

    assert index is None


# --- the chain predicates ------------------------------------------------------


def test_a_chain_with_every_seat_approved_is_complete() -> None:
    """Catches a predicate that never returns True (nothing would ever
    execute) or one keyed on the seat count rather than the decisions."""
    slots = (
        _decided(ApproverSlot.DEPARTMENT_MANAGER, CALLER),
        _decided(ApproverSlot.IT_ADMIN),
    )

    assert chain_is_complete(slots) is True
    assert is_rejected(slots) is False


def test_a_chain_with_one_open_seat_is_not_complete() -> None:
    """Catches a predicate reading only the seat just decided: the grant must
    not execute while a seat is still open, which is the rule being tested."""
    slots = (
        _decided(ApproverSlot.DEPARTMENT_MANAGER, CALLER),
        _open(ApproverSlot.IT_ADMIN),
    )

    assert chain_is_complete(slots) is False


def test_a_rejected_chain_is_never_complete_even_with_no_open_seat() -> None:
    """Catches "complete = no open seats", which would execute the grant for
    a chain whose last decision was a REJECTION. One rejection ends the
    chain, so this is the difference between a refused request
    and a granted one."""
    slots = (
        _decided(ApproverSlot.DEPARTMENT_MANAGER, CALLER, SlotDecision.APPROVE),
        _decided(ApproverSlot.IT_ADMIN, None, SlotDecision.REJECT),
    )

    assert is_rejected(slots) is True
    assert chain_is_complete(slots) is False


def test_an_untouched_chain_is_neither_complete_nor_rejected() -> None:
    """Catches predicates inverted on the empty-decision case, which would
    make a freshly queued request read as already decided."""
    slots = (_open(ApproverSlot.DEPARTMENT_MANAGER, CALLER), _open(ApproverSlot.HR))

    assert chain_is_complete(slots) is False
    assert is_rejected(slots) is False


# --- recording one decision -----------------------------------------------------


@pytest.mark.parametrize("decision", list(SlotDecision))
def test_deciding_one_seat_leaves_every_other_seat_untouched(decision: SlotDecision) -> None:
    """Catches a writer that rewrites the whole chain (stamping the caller on
    seats they never filled, which is a forged approval in the audit record)
    and one that decides the wrong index. The before-tuple is asserted
    unchanged as well, so an in-place mutation is caught too."""
    before = (
        _open(ApproverSlot.DEPARTMENT_MANAGER, OTHER_PERSON),
        _open(ApproverSlot.SYSTEM_OWNER, CALLER),
        _open(ApproverSlot.IT_ADMIN),
    )

    after = decide_slot(
        before,
        1,
        decision=decision,
        decided_by_user_id=CALLER,
        decided_at=DECIDED_AT,
        comment="fake decision note",
    )

    assert after[1] == SlotState(
        slot=ApproverSlot.SYSTEM_OWNER,
        user_id=CALLER,
        decision=decision,
        decided_by_user_id=CALLER,
        decided_at=DECIDED_AT,
        comment="fake decision note",
    )
    assert after[0] == before[0]
    assert after[2] == before[2]
    assert before[1].decision is None, "the input chain must not be mutated"


def test_a_decision_records_the_person_who_acted_not_the_person_the_seat_names() -> None:
    """Catches `decided_by_user_id` copied from `slot.user_id`.

    On a GROUP seat the seat names nobody, so an implementation copying the
    id would store `None` and the audit trail would say a critical request
    was approved by no one. The seat's own `user_id` must stay as it was.
    """
    before = (_open(ApproverSlot.IT_ADMIN),)

    after = decide_slot(
        before,
        0,
        decision=SlotDecision.APPROVE,
        decided_by_user_id=CALLER,
        decided_at=DECIDED_AT,
        comment=None,
    )

    assert after[0].decided_by_user_id == CALLER
    assert after[0].user_id is None


# --- database cases: planting rows around the surface ---------------------------


def _insert_request(
    url: str,
    *,
    requester_user_id: uuid.UUID,
    target_user_id: uuid.UUID,
    role_id: uuid.UUID,
    approvals: Sequence[dict[str, Any]],
    status: RequestStatus = RequestStatus.PENDING,
    tenant_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """One committed `access_requests` row with the chain this case needs."""

    async def scenario() -> uuid.UUID:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant_id or ROWS.tenant_id) as session:
                row = await Repository(session, AccessRequest).create(
                    requester_user_id=requester_user_id,
                    target_user_id=target_user_id,
                    role_id=role_id,
                    justification=f"fake approvals fixture {uuid.uuid4().hex}",
                    requested_expiry_days=None,
                    risk_class="critical",
                    decision="approval",
                    decision_reasons=[],
                    citations=[],
                    status=status.value,
                    approver_user_ids=[],
                    approvals=list(approvals),
                    session_id=None,
                    trace_id=None,
                    idempotency_key=None,
                )
                return row.id
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _seat(slot: ApproverSlot, user_id: uuid.UUID | None = None) -> dict[str, Any]:
    """One stored open seat, exactly the JSONB shape the request flow writes."""
    return {"slot": slot.value, "user_id": None if user_id is None else str(user_id)}


def _queue(
    url: str,
    *,
    approver_user_id: uuid.UUID,
    scopes: frozenset[Scope],
    principals: frozenset[Principal],
    limit: int = 50,
) -> list[tuple[uuid.UUID, ApproverSlot]]:
    """The caller's queue as (request id, the seat they may fill), in order."""

    async def scenario() -> list[tuple[uuid.UUID, ApproverSlot]]:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                rows = await list_pending_for_approver(
                    session,
                    SimulatedDirectory(session),
                    approver_user_id=approver_user_id,
                    scopes=scopes,
                    principals=principals,
                    limit=limit,
                )
                return [(request.id, slot.slot) for request, slot in rows]
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _read_row(url: str, request_id: uuid.UUID) -> tuple[str, list[Any]]:
    async def scenario() -> tuple[str, list[Any]]:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                row = await Repository(session, AccessRequest).get(request_id)
                assert row is not None
                return row.status, list(row.approvals)
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


# --- the scoped queue ---------------------------------------------


def test_a_manager_sees_a_pending_row_in_their_department_that_names_them(
    access_db_url: str,  # noqa: F811
) -> None:
    """The allow direction: catches a queue that returns nothing at all (every
    refusal case below would still pass), and one that loses the seat — the
    caller has to be told WHICH seat is theirs, or `approve_request` and the
    queue can disagree about it."""
    request_id = _insert_request(
        access_db_url,
        requester_user_id=REQUESTER.id,
        target_user_id=DEPARTMENT_TARGET.id,
        role_id=PLAIN_ROLE,
        approvals=[_seat(ApproverSlot.DEPARTMENT_MANAGER, PRODUCTION_MANAGER.id)],
    )

    queue = _queue(
        access_db_url,
        approver_user_id=PRODUCTION_MANAGER.id,
        scopes=frozenset({Scope.OWN_DEPARTMENT}),
        principals=frozenset({Principal.MANAGER}),
    )

    assert (request_id, ApproverSlot.DEPARTMENT_MANAGER) in queue


def test_the_scope_filter_alone_hides_a_row_whose_seat_names_the_caller(
    access_db_url: str,  # noqa: F811
) -> None:
    """The seat NAMES the caller, so the eligibility filter
    would return this row. Only the `own_department` scope refuses it — the
    target sits in Sales while the caller manages Production — so deleting
    the scope filter is the one change that flips this case."""
    request_id = _insert_request(
        access_db_url,
        requester_user_id=REQUESTER.id,
        target_user_id=OUTSIDER_TARGET.id,
        role_id=PLAIN_ROLE,
        approvals=[_seat(ApproverSlot.DEPARTMENT_MANAGER, PRODUCTION_MANAGER.id)],
    )

    queue = _queue(
        access_db_url,
        approver_user_id=PRODUCTION_MANAGER.id,
        scopes=frozenset({Scope.OWN_DEPARTMENT}),
        principals=frozenset({Principal.MANAGER}),
    )

    assert request_id not in [entry[0] for entry in queue]


def test_the_seat_filter_alone_hides_a_row_in_the_callers_own_department(
    access_db_url: str,  # noqa: F811
) -> None:
    """The other half: the target IS in the caller's
    department, so the scope filter admits this row and only the eligibility
    filter refuses it — the open seat names somebody else. Without this case
    a queue with no eligibility filter passes every other queue test."""
    request_id = _insert_request(
        access_db_url,
        requester_user_id=REQUESTER.id,
        target_user_id=QUEUE_TARGET_ONE.id,
        role_id=PLAIN_ROLE,
        approvals=[_seat(ApproverSlot.DEPARTMENT_MANAGER, NOT_THE_MANAGER.id)],
    )

    queue = _queue(
        access_db_url,
        approver_user_id=PRODUCTION_MANAGER.id,
        scopes=frozenset({Scope.OWN_DEPARTMENT}),
        principals=frozenset({Principal.MANAGER}),
    )

    assert request_id not in [entry[0] for entry in queue]


def test_a_system_owner_sees_a_row_on_their_own_system_in_another_department(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches `own_systems` implemented as `own_department` (or dropped
    entirely): the target is a Production user and the caller is in IT, so
    only the system-ownership predicate can return this row."""
    request_id = _insert_request(
        access_db_url,
        requester_user_id=REQUESTER.id,
        target_user_id=SYSTEM_TARGET.id,
        role_id=OWNED_SYSTEM_ROLE,
        approvals=[_seat(ApproverSlot.SYSTEM_OWNER, PROD_K8S_OWNER.id)],
    )

    queue = _queue(
        access_db_url,
        approver_user_id=PROD_K8S_OWNER.id,
        scopes=frozenset({Scope.OWN_SYSTEMS}),
        principals=frozenset({Principal.SYSTEM_OWNER}),
    )

    assert (request_id, ApproverSlot.SYSTEM_OWNER) in queue


def test_two_scopes_return_the_union_and_one_scope_returns_only_its_half(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches the queue consuming a single widest scope instead of the whole
    set — the narrowing the earlier design recorded.

    `own_department` is wider than `own_systems` in the total order, so a
    manager+system_owner caller collapsed to one scope loses the systems row
    entirely. The second half of the case asserts the same call with only
    `own_department` DOES lose it, so the union assertion cannot pass by the
    filter being absent altogether.
    """
    department_request = _insert_request(
        access_db_url,
        requester_user_id=REQUESTER.id,
        target_user_id=IT_TARGET.id,
        role_id=UNOWNED_ROLE,
        approvals=[_seat(ApproverSlot.DEPARTMENT_MANAGER, PROD_K8S_OWNER.id)],
    )
    systems_request = _insert_request(
        access_db_url,
        requester_user_id=REQUESTER.id,
        target_user_id=QUEUE_TARGET_TWO.id,
        role_id=OWNED_SYSTEM_ROLE,
        approvals=[_seat(ApproverSlot.SYSTEM_OWNER, PROD_K8S_OWNER.id)],
    )

    both = _queue(
        access_db_url,
        approver_user_id=PROD_K8S_OWNER.id,
        scopes=frozenset({Scope.OWN_DEPARTMENT, Scope.OWN_SYSTEMS}),
        principals=frozenset({Principal.MANAGER, Principal.SYSTEM_OWNER}),
    )
    department_only = _queue(
        access_db_url,
        approver_user_id=PROD_K8S_OWNER.id,
        scopes=frozenset({Scope.OWN_DEPARTMENT}),
        principals=frozenset({Principal.MANAGER}),
    )

    both_ids = [entry[0] for entry in both]
    assert department_request in both_ids
    assert systems_request in both_ids

    department_only_ids = [entry[0] for entry in department_only]
    assert department_request in department_only_ids
    assert systems_request not in department_only_ids


def test_the_any_scope_reaches_a_group_seat_in_a_department_the_caller_is_not_in(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches `Scope.ANY` narrowed to the caller's own department (the permission matrix
    gives `it_admin` and `platform_admin` the whole tenant) and a group seat
    the queue cannot match — an `it_admin` seat is exactly the seat no named
    id can find."""
    request_id = _insert_request(
        access_db_url,
        requester_user_id=REQUESTER.id,
        target_user_id=GROUP_TARGET.id,
        role_id=OTHER_ROLE,
        approvals=[_seat(ApproverSlot.IT_ADMIN)],
    )

    queue = _queue(
        access_db_url,
        approver_user_id=IT_ADMIN_USER.id,
        scopes=frozenset({Scope.ANY}),
        principals=frozenset({Principal.IT_ADMIN}),
    )

    assert (request_id, ApproverSlot.IT_ADMIN) in queue


def test_a_row_that_is_not_pending_is_never_queued(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches a queue filtered by seat and scope but not by status: an
    already-approved row whose chain still carries an open seat would sit in
    an approver's queue for ever, and approving it would re-execute a
    decision that was taken."""
    request_id = _insert_request(
        access_db_url,
        requester_user_id=REQUESTER.id,
        target_user_id=NON_PENDING_TARGET.id,
        role_id=PLAIN_ROLE,
        approvals=[_seat(ApproverSlot.DEPARTMENT_MANAGER, PRODUCTION_MANAGER.id)],
        status=RequestStatus.APPROVED,
    )

    queue = _queue(
        access_db_url,
        approver_user_id=PRODUCTION_MANAGER.id,
        scopes=frozenset({Scope.OWN_DEPARTMENT}),
        principals=frozenset({Principal.MANAGER}),
    )

    assert request_id not in [entry[0] for entry in queue]


def test_a_party_to_the_request_never_sees_it_in_their_queue(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches the party exclusion living only in `approve_request`: a queue
    that offers a manager their own request invites a refusal at the moment
    they act, and shows the requester a seat they can never fill.

    The seat NAMES the caller and the target is in their department, so scope
    and eligibility both admit the row; only the party rule refuses it.
    """
    request_id = _insert_request(
        access_db_url,
        requester_user_id=PRODUCTION_MANAGER.id,
        target_user_id=PARTY_TARGET.id,
        role_id=PLAIN_ROLE,
        approvals=[_seat(ApproverSlot.DEPARTMENT_MANAGER, PRODUCTION_MANAGER.id)],
    )

    queue = _queue(
        access_db_url,
        approver_user_id=PRODUCTION_MANAGER.id,
        scopes=frozenset({Scope.OWN_DEPARTMENT}),
        principals=frozenset({Principal.MANAGER}),
    )

    assert request_id not in [entry[0] for entry in queue]


def test_a_chain_whose_seats_are_all_decided_is_not_queued(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches a queue matching the caller against ANY seat rather than an
    OPEN one: the caller already decided this seat, so offering it again
    would invite a second decision on one seat."""
    decided = {
        "slot": ApproverSlot.DEPARTMENT_MANAGER.value,
        "user_id": str(PRODUCTION_MANAGER.id),
        "decision": SlotDecision.APPROVE.value,
        "decided_by_user_id": str(PRODUCTION_MANAGER.id),
        "decided_at": DECIDED_AT.isoformat(),
        "comment": None,
    }
    request_id = _insert_request(
        access_db_url,
        requester_user_id=REQUESTER.id,
        target_user_id=DECIDED_TARGET.id,
        role_id=PLAIN_ROLE,
        approvals=[decided],
    )

    queue = _queue(
        access_db_url,
        approver_user_id=PRODUCTION_MANAGER.id,
        scopes=frozenset({Scope.OWN_DEPARTMENT}),
        principals=frozenset({Principal.MANAGER}),
    )

    assert request_id not in [entry[0] for entry in queue]


def test_the_queue_is_oldest_first_and_stops_at_the_limit(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches an unordered queue (an approver would work a random order and
    the oldest request would starve) and a `limit` accepted but ignored,
    which is the unbounded response over the wire.

    The approver here is named on no other case's seat, so this queue holds
    exactly these two rows and the order is asserted as an exact list.
    """
    first = _insert_request(
        access_db_url,
        requester_user_id=REQUESTER.id,
        target_user_id=QUEUE_TARGET_ONE.id,
        role_id=PLAIN_ROLE,
        approvals=[_seat(ApproverSlot.DEPARTMENT_MANAGER, QUEUE_APPROVER.id)],
    )
    second = _insert_request(
        access_db_url,
        requester_user_id=REQUESTER.id,
        target_user_id=QUEUE_TARGET_TWO.id,
        role_id=PLAIN_ROLE,
        approvals=[_seat(ApproverSlot.DEPARTMENT_MANAGER, QUEUE_APPROVER.id)],
    )

    everything = _queue(
        access_db_url,
        approver_user_id=QUEUE_APPROVER.id,
        scopes=frozenset({Scope.ANY}),
        principals=frozenset(),
    )
    capped = _queue(
        access_db_url,
        approver_user_id=QUEUE_APPROVER.id,
        scopes=frozenset({Scope.ANY}),
        principals=frozenset(),
        limit=1,
    )

    assert [entry[0] for entry in everything] == [first, second]
    assert [entry[0] for entry in capped] == [first]


# --- the row lock and the status write ------------------------------------------


def _lock(url: str, request_id: uuid.UUID, tenant_id: uuid.UUID | None = None) -> uuid.UUID | None:
    async def scenario() -> uuid.UUID | None:
        engine = create_engine(url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, tenant_id or ROWS.tenant_id) as session:
                row = await lock_request(session, request_id)
                return None if row is None else row.id
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def test_locking_answers_the_same_way_for_another_tenants_row_and_a_missing_one(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches a lookup that escapes the tenant guard — `FOR UPDATE` written
    as raw SQL is the easy way to lose it — and one that distinguishes the
    two absences. A row that exists in another tenant must read exactly like
    a row that does not exist, or the id becomes an oracle: an approver could
    probe which request ids are live in a tenant they cannot see.

    The real row is locked and returned first, so this cannot pass by the
    lookup returning `None` for everything.
    """
    mine = _insert_request(
        access_db_url,
        requester_user_id=REQUESTER.id,
        target_user_id=LOCK_TARGET.id,
        role_id=PLAIN_ROLE,
        approvals=[_seat(ApproverSlot.DEPARTMENT_MANAGER, PRODUCTION_MANAGER.id)],
    )
    theirs = _insert_request(
        access_db_url,
        requester_user_id=OTHER_TENANT_REQUESTER.id,
        target_user_id=OTHER_TENANT_TARGET.id,
        role_id=OTHER_TENANT_ROLE,
        approvals=[],
        tenant_id=OTHER_TENANT_ROWS.tenant_id,
    )

    assert _lock(access_db_url, mine) == mine
    assert _lock(access_db_url, theirs) is None
    assert _lock(access_db_url, uuid.uuid4()) is None


class _Boom(Exception):
    """Raised by the caller AFTER `set_request_status` returned, to roll back."""


def test_the_status_write_is_flushed_not_committed(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches a status write that commits (or opens its own session): the
    approve path writes the status, the chain and the grant in ONE
    transaction, so a self-committing write leaves a request marked
    `approved` whose grant was rolled back — access granted on paper and
    never in the directory.

    The committed half runs first and asserts the write really happens, so
    this cannot pass by `set_request_status` writing nothing at all.
    """
    request_id = _insert_request(
        access_db_url,
        requester_user_id=REQUESTER.id,
        target_user_id=STATUS_TARGET.id,
        role_id=PLAIN_ROLE,
        approvals=[_seat(ApproverSlot.DEPARTMENT_MANAGER, PRODUCTION_MANAGER.id)],
    )
    decided = dump_slots(
        (
            SlotState(
                slot=ApproverSlot.DEPARTMENT_MANAGER,
                user_id=PRODUCTION_MANAGER.id,
                decision=SlotDecision.APPROVE,
                decided_by_user_id=PRODUCTION_MANAGER.id,
                decided_at=DECIDED_AT,
                comment=None,
            ),
        )
    )

    async def commit_scenario() -> None:
        engine = create_engine(access_db_url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                await set_request_status(
                    session, request_id, status=RequestStatus.APPROVED, approvals=decided
                )
        finally:
            await engine.dispose()

    asyncio.run(commit_scenario())

    status, approvals = _read_row(access_db_url, request_id)
    assert status == RequestStatus.APPROVED.value
    assert parse_slots(approvals)[0].decision is SlotDecision.APPROVE

    async def rollback_scenario() -> None:
        engine = create_engine(access_db_url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                await set_request_status(session, request_id, status=RequestStatus.FAILED)
                raise _Boom("the caller failed after the status was written")
        finally:
            await engine.dispose()

    with contextlib.suppress(_Boom):
        asyncio.run(rollback_scenario())

    status_after, _ = _read_row(access_db_url, request_id)
    assert status_after == RequestStatus.APPROVED.value


def test_the_status_write_leaves_the_chain_alone_when_none_is_given(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches `approvals=None` written through as an empty list, which would
    erase every recorded decision on the reject and fail paths — the audit
    trail's only record of who decided what."""
    seats = [_seat(ApproverSlot.DEPARTMENT_MANAGER, PRODUCTION_MANAGER.id)]
    request_id = _insert_request(
        access_db_url,
        requester_user_id=REQUESTER.id,
        target_user_id=QUEUE_TARGET_ONE.id,
        role_id=OTHER_ROLE,
        approvals=seats,
    )

    async def scenario() -> None:
        engine = create_engine(access_db_url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                await set_request_status(session, request_id, status=RequestStatus.FAILED)
        finally:
            await engine.dispose()

    asyncio.run(scenario())

    status, approvals = _read_row(access_db_url, request_id)
    assert status == RequestStatus.FAILED.value
    assert parse_slots(approvals) == parse_slots(seats)


def test_the_decided_at_written_here_is_the_instant_it_is_given(
    access_db_url: str,  # noqa: F811
) -> None:
    """Catches a chain round trip that loses the decision timestamp through
    JSONB (a `datetime` dropped, or reparsed naive): `decided_at` is the only
    record of WHEN an approval happened, and a naive value would compare
    wrong against every aware instant in the audit trail."""
    request_id = _insert_request(
        access_db_url,
        requester_user_id=REQUESTER.id,
        target_user_id=STATUS_TARGET.id,
        role_id=OTHER_ROLE,
        approvals=[_seat(ApproverSlot.HR)],
    )
    decided = dump_slots(
        decide_slot(
            (SlotState(slot=ApproverSlot.HR),),
            0,
            decision=SlotDecision.APPROVE,
            decided_by_user_id=PRODUCTION_MANAGER.id,
            decided_at=DECIDED_AT,
            comment="fake hr note",
        )
    )

    async def scenario() -> None:
        engine = create_engine(access_db_url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, ROWS.tenant_id) as session:
                await set_request_status(
                    session, request_id, status=RequestStatus.APPROVED, approvals=decided
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())

    _, approvals = _read_row(access_db_url, request_id)
    stored = parse_slots(approvals)[0]
    assert stored.decided_at == DECIDED_AT
    assert isinstance(stored.decided_at, datetime)
    assert stored.comment == "fake hr note"


# --- one person, one seat per chain ---------------------------------------------


@pytest.mark.parametrize("decision", list(SlotDecision))
def test_a_caller_who_already_decided_a_seat_gets_no_second_one(
    decision: SlotDecision,
) -> None:
    """Catches the four-eyes control collapsing onto one person.

    A manager who also holds `it_admin`, or who also owns the system, is
    named on (or eligible for) two seats of the same chain. Without this rule
    they fill both over two calls with two idempotency keys and a `critical`
    request is approved almost single-handed — exactly what
    `policy/decision.py::_is_usable_fallback` refuses to let the ENGINE build
    ("that would collapse two approvers into one person and quietly halve the
    control"), inherited here because the approval flow fills seats by whoever acts.

    Seat 1 names the caller and is open, so `may_fill` says yes to it; only
    the chain-level rule can refuse. Both decisions are crossed: a rejection
    ends the chain anyway, but the caller must not be able to reject one seat
    and approve another either.
    """
    slots = (
        SlotState(
            slot=ApproverSlot.IT_ADMIN,
            decision=decision,
            decided_by_user_id=CALLER,
            decided_at=DECIDED_AT,
            comment=None,
        ),
        _open(ApproverSlot.SYSTEM_OWNER, CALLER),
    )

    assert _fillable(slots[1], caller_principals=frozenset({Principal.IT_ADMIN})) is True, (
        "premise: the seat itself is fillable, so only the chain rule can refuse"
    )
    assert (
        first_fillable_index(
            slots,
            caller_user_id=CALLER,
            caller_principals=frozenset({Principal.IT_ADMIN}),
            requester_user_id=REQUESTER_ID,
            target_user_id=TARGET_ID,
        )
        is None
    )


def test_a_second_approver_is_still_offered_the_seat_after_someone_else_decided() -> None:
    """The allow direction of the rule above: catches a chain rule keyed on
    "any decided seat" rather than "a seat decided by THIS caller", which
    would freeze every multi-seat chain after its first approval and make
    the rule unreachable."""
    slots = (
        _decided(ApproverSlot.DEPARTMENT_MANAGER, OTHER_PERSON),
        _open(ApproverSlot.SYSTEM_OWNER, CALLER),
    )

    assert (
        first_fillable_index(
            slots,
            caller_user_id=CALLER,
            caller_principals=frozenset(),
            requester_user_id=REQUESTER_ID,
            target_user_id=TARGET_ID,
        )
        == 1
    )


# --- the scope predicate both paths share ---------------------------------------

_DEPARTMENT = uuid.uuid4()
_OTHER_DEPARTMENT = uuid.uuid4()


def _reaches(
    scopes: frozenset[Scope],
    *,
    target_department_id: uuid.UUID | None = _OTHER_DEPARTMENT,
    system_owner_user_id: uuid.UUID | None = OTHER_PERSON,
    approver_department_id: uuid.UUID | None = _DEPARTMENT,
) -> bool:
    return request_in_scope(
        scopes,
        approver_user_id=CALLER,
        approver_department_id=approver_department_id,
        target_department_id=target_department_id,
        system_owner_user_id=system_owner_user_id,
    )


def test_the_any_scope_reaches_every_request() -> None:
    """`it_admin` and `platform_admin` hold the whole tenant;
    catches `any` narrowed to the caller's own department, which would hide
    every cross-department request from the two principals that exist to
    unblock them."""
    assert _reaches(frozenset({Scope.ANY})) is True


def test_own_department_reaches_only_the_callers_own_department() -> None:
    """Catches a comparison that is absent (every department reachable) or
    inverted. The `None` case is the one a transferred-out caller hits: a
    caller with no department must reach nothing, not everything, because
    `None == None` on an unassigned target would otherwise pair them up."""
    assert _reaches(frozenset({Scope.OWN_DEPARTMENT}), target_department_id=_DEPARTMENT) is True
    assert _reaches(frozenset({Scope.OWN_DEPARTMENT})) is False
    assert (
        _reaches(
            frozenset({Scope.OWN_DEPARTMENT}),
            target_department_id=None,
            approver_department_id=None,
        )
        is False
    )


def test_own_systems_reaches_only_systems_the_caller_owns() -> None:
    """Catches an ownerless system (`owner_user_id is None`) reading as
    reachable, which would hand every unowned system's requests to any
    system_owner in the tenant."""
    assert _reaches(frozenset({Scope.OWN_SYSTEMS}), system_owner_user_id=CALLER) is True
    assert _reaches(frozenset({Scope.OWN_SYSTEMS})) is False
    assert _reaches(frozenset({Scope.OWN_SYSTEMS}), system_owner_user_id=None) is False


def test_two_scopes_reach_what_either_one_reaches() -> None:
    """The union, at the predicate level: catches an `all` where an `any`
    belongs, which would make a manager+system_owner reach only requests
    satisfying BOTH scopes — nearly nothing."""
    both = frozenset({Scope.OWN_DEPARTMENT, Scope.OWN_SYSTEMS})

    assert _reaches(both, system_owner_user_id=CALLER) is True
    assert _reaches(both, target_department_id=_DEPARTMENT) is True
    assert _reaches(both) is False


@pytest.mark.parametrize("scope", [Scope.SELF_ONLY, Scope.SYSTEM_ONLY])
def test_a_scope_this_row_never_carries_reaches_nothing(scope: Scope) -> None:
    """A scope no approval row uses must
    narrow to nothing rather than fall through to a permissive default, so a
    later matrix edit cannot widen these tools by accident."""
    assert _reaches(frozenset({scope}), target_department_id=_DEPARTMENT) is False
    assert _reaches(frozenset(), target_department_id=_DEPARTMENT) is False
