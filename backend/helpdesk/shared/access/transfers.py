"""Moving a user between departments. See `lifecycle.py` for the shared types this uses."""

import uuid
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.access.directory import DirectoryProvider
from helpdesk.shared.access.execute import (
    PolicyBlockedAtExecutionError,
    SodConflictAtExecutionError,
    execute_grant,
)
from helpdesk.shared.access.lifecycle import (
    TRANSFER_REVOKE_REASON,
    USER_TRANSFERRED_EVENT_TYPE,
    LeaverTransferError,
    SameDepartmentError,
    SkippedRole,
    TransferResult,
    _ACTOR_KIND,
    _SUBJECT_TYPE,
)
from helpdesk.shared.access.requests import lock_open_requests_for_target
from helpdesk.shared.access.revoke import revoke_role_access
from helpdesk.shared.audit import AuditEventInput, append_event
from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.core.errors import NotFoundError
from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.users import lock_user
from helpdesk.shared.models import User
from helpdesk.shared.policy.types import TargetStatus

__all__ = ["transfer_user"]


async def transfer_user(
    session: AsyncSession,
    directory: DirectoryProvider,
    *,
    user_id: uuid.UUID,
    new_department_id: uuid.UUID,
    effective_date: date,
    transferred_by_user_id: uuid.UUID,
    actor_ref: str | None = None,
    session_id: uuid.UUID | None = None,
    now: datetime,
) -> TransferResult:
    """Move one person between departments and diff the two templates.

    The caller has already resolved `new_department_id` inside its own tenant
    and refused an unknown one `NOT_FOUND`
    (`mcp_server.tools.lifecycle.transfer_user`); `lock_user` below answers
    None for a user id that is missing OR another tenant's, so the same
    `NOT_FOUND` covers both here (pinned by `test_mcp_transfer_user.py::
    test_a_missing_and_another_tenants_user_answer_the_same_not_found`).

    The lock comes FIRST, before either refusal, because both of them read
    state a concurrent transfer can change: the department the
    same-department check compares against is exactly what the winner of a
    race rewrites (decision D).

    Order of effects, and each part of it is a decision:

    1. revoke, then apply (decision B) — an SoD pair spanning the two
       departments then resolves in favour of the department the person is
       moving INTO, which is the point of a transfer;
    2. the department is written BEFORE the applications (decision A), because
       `build_request_snapshot` reads `role_in_department_template` off the
       target's CURRENT department: an application classified before the write
       sees every arriving role as out-of-template and caps its expiry as
       `elevated` (pinned by
       `test_access_transfer.py::test_the_applications_are_classified_against_the_new_department`,
       the only case that flips if the write moves);
    3. `user.transferred` lands after both sets of grant events (decision C),
       because every set it names is known only by then.

    `now` is keyword-only and supplied by the caller, so the four sets, every
    classification, each `revoked_at` and each `granted_at` answer for one
    instant; reading a clock here would let a grant be diffed against one
    moment and revoked at another.

    Everything runs in the caller's transaction. Decision B leaves an instant
    where the mover holds neither the departing role nor the arriving one; it
    is inside that single transaction, so no other reader ever sees it
    (pinned by `test_access_transfer.py::test_the_transfer_and_its_events_roll_back_together`).
    """
    user = await lock_user(session, user_id)
    if user is None:
        raise NotFoundError("no such user in this tenant")
    if user.status == TargetStatus.LEAVER.value:
        raise LeaverTransferError("a leaver cannot be transferred")
    if user.department_id == new_department_id:
        raise SameDepartmentError("that user is already in this department")

    from_department_id = user.department_id
    held = await directory.list_account_role_ids(user_id, now)
    old_template = (
        frozenset()
        if from_department_id is None
        else await directory.list_department_role_ids(from_department_id)
    )
    new_template = await directory.list_department_role_ids(new_department_id)

    # The four sets of §1, computed together at this one instant so no role can
    # fall into two of them or into none. Sorted, so two runs over the same
    # data write their events in the same order.
    to_revoke = sorted((held & old_template) - new_template)
    retained = sorted(held & new_template)
    to_apply = sorted(new_template - held)
    non_standard = sorted(held - old_template - new_template)

    for role_id in to_revoke:
        await revoke_role_access(
            session,
            directory,
            user_id=user_id,
            role_id=role_id,
            reason=TRANSFER_REVOKE_REASON,
            revoked_by_user_id=transferred_by_user_id,
            actor_ref=actor_ref if actor_ref is not None else str(transferred_by_user_id),
            session_id=session_id,
            now=now,
        )

    # Before the applications, never after (decision A).
    await Repository(session, User).update(user_id, department_id=new_department_id)

    applied: list[uuid.UUID] = []
    skipped: list[SkippedRole] = []
    for role_id in to_apply:
        try:
            await execute_grant(
                session,
                directory,
                request_id=None,
                requester_user_id=transferred_by_user_id,
                target_user_id=user_id,
                role_id=role_id,
                requested_expiry_days=None,
                granted_by_user_id=transferred_by_user_id,
                actor_ref=actor_ref if actor_ref is not None else str(transferred_by_user_id),
                session_id=session_id,
                now=now,
            )
        except (SodConflictAtExecutionError, PolicyBlockedAtExecutionError) as refusal:
            # A refused role costs that role, not the move (§1, criterion 5):
            # a broken template is a data defect somebody has to see, and
            # leaving a mover in their new department with nothing is worse.
            skipped.append(
                SkippedRole(role_id=role_id, reason_code=refusal.code, reason=str(refusal))
            )
            continue
        applied.append(role_id)

    await _append_transferred(
        session,
        user_id=user_id,
        from_department_id=from_department_id,
        to_department_id=new_department_id,
        effective_date=effective_date,
        revoked_role_ids=to_revoke,
        applied_role_ids=applied,
        skipped=skipped,
        retained_role_ids=retained,
        non_standard_role_ids=non_standard,
        actor_ref=actor_ref if actor_ref is not None else str(transferred_by_user_id),
        session_id=session_id,
        now=now,
    )
    return TransferResult(
        user=user,
        from_department_id=from_department_id,
        revoked_role_ids=tuple(to_revoke),
        applied_role_ids=tuple(applied),
        skipped=tuple(skipped),
        retained_role_ids=tuple(retained),
        non_standard_role_ids=tuple(non_standard),
    )


async def _append_transferred(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    from_department_id: uuid.UUID | None,
    to_department_id: uuid.UUID,
    effective_date: date,
    revoked_role_ids: list[uuid.UUID],
    applied_role_ids: list[uuid.UUID],
    skipped: list[SkippedRole],
    retained_role_ids: list[uuid.UUID],
    non_standard_role_ids: list[uuid.UUID],
    actor_ref: str,
    session_id: uuid.UUID | None,
    now: datetime,
) -> None:
    """the design's move record, subject the mover, nine keys exactly.

    `effective_date` is here and nowhere else (decision G): the design puts it
    in the signature, there is no column for it and no scheduler until row 21,
    so the transfer takes effect now and this payload is the only record that
    a date was asked for. `non_standard_role_ids` is likewise decision H's
    only record — nothing flags those grants until an access review runs.
    """
    await append_event(
        session,
        bound_tenant_id(session),
        AuditEventInput(
            occurred_at=now,
            actor_kind=_ACTOR_KIND,
            actor_ref=actor_ref,
            session_id=session_id,
            trace_id=None,
            event_type=USER_TRANSFERRED_EVENT_TYPE,
            subject_type=_SUBJECT_TYPE,
            subject_id=str(user_id),
            payload={
                "user_id": str(user_id),
                "from_department_id": (
                    None if from_department_id is None else str(from_department_id)
                ),
                "to_department_id": str(to_department_id),
                "effective_date": effective_date.isoformat(),
                "revoked_role_ids": [str(role_id) for role_id in revoked_role_ids],
                "applied_role_ids": [str(role_id) for role_id in applied_role_ids],
                "skipped_role_ids": [str(record.role_id) for record in skipped],
                "retained_role_ids": [str(role_id) for role_id in retained_role_ids],
                "non_standard_role_ids": [str(role_id) for role_id in non_standard_role_ids],
            },
        ),
    )
