"""Moving a user between departments and fixing up their access."""

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
    _ACTOR_KIND,
    _SUBJECT_TYPE,
    TRANSFER_REVOKE_REASON,
    USER_TRANSFERRED_EVENT_TYPE,
    LeaverTransferError,
    SameDepartmentError,
    SkippedRole,
    TransferResult,
)
from helpdesk.shared.access.revoke import revoke_role_access
from helpdesk.shared.audit import AuditEventInput, append_event
from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.core.errors import NotFoundError
from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.users import lock_user
from helpdesk.shared.models import User
from helpdesk.shared.policy.types import TargetStatus

__all__ = ["TRANSFER_REVOKE_REASON", "USER_TRANSFERRED_EVENT_TYPE", "transfer_user"]


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
    """Move one person between departments and compare the two templates.

    A missing user and another tenant's user give the same `NOT_FOUND`. The
    user row is locked before either refusal, since a racing transfer can
    change the department being compared.

    Order: revoke first, then apply, so a conflict that spans both
    departments resolves in favour of the new one. The department is
    written before the new roles are granted; checked earlier, every new
    role would look out-of-template and get a shorter expiry. The move event
    comes last, since it needs every list. For a moment the person holds
    neither old nor new role, but that is inside one transaction, so no
    other reader ever sees it.
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

    # The four sets, worked out together so no role lands in two or none.
    # Sorted, so events come out in a steady order.
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

    # Before granting, so new roles are checked against the new department.
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
            # A refused role costs that role, not the move: a broken
            # template must show up, and leaving the person with nothing is
            # worse.
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
    """The move event, with the mover as subject.

    It is the only record of `effective_date` (the move takes effect now)
    and of the non-standard roles, until a review flags them.
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
