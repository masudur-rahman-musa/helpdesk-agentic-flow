"""`build_request_snapshot`: repository state in, an earlier change's snapshot out.

The first and only consumer of the policy engine's `RequestSnapshot` shape.
Directory state (target user, template membership, live grants) comes
through `DirectoryReader`, the port's read half; catalog and request state (role,
system, SoD rules, manager, approved-pending set) through the repository
and `helpdesk.access.requests`. The engine itself stays pure — this module
does all the I/O so `policy/` never has to.

An unknown role or target raises the typed `NotFoundError` (the design); a
target in another tenant reads as unknown because the session guard filters
every query, so the same answer covers both and not-found cannot be used as
a cross-tenant existence oracle (pinned by `test_access_snapshot.py::
test_a_real_user_of_another_tenant_is_not_found`).

`policy_refs` stays the empty default until row 15 wires citations (plan
11a decision B); SoD conflicts still carry `sod_rules.policy_ref`, which
travels inside the snapshot's rule rows.
"""

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.access.requests import list_approved_role_ids_for_target
from helpdesk.shared.core.errors import NotFoundError
from helpdesk.shared.crud import Repository
from helpdesk.shared.models import Department, Role, SodRule, System
from helpdesk.shared.policy.types import (
    Privilege,
    RequestSnapshot,
    RoleSnapshot,
    Sensitivity,
    SodRuleSnapshot,
    SystemCategory,
    TargetSnapshot,
    TargetStatus,
)
from helpdesk.shared.access.directory import DirectoryReader


async def build_request_snapshot(
    session: AsyncSession,
    directory: DirectoryReader,
    *,
    requester_user_id: uuid.UUID,
    target_user_id: uuid.UUID,
    role_id: uuid.UUID,
    requested_expiry_days: int | None = None,
    now: datetime,
) -> RequestSnapshot:
    """Everything `policy.engine.classify` is allowed to see, loaded once.

    `now` is the caller's aware clock, keyword-only: tenure and grant
    liveness are both read against it, so one call answers for one instant.
    `requested_expiry_days` was validated at the boundary (gt 0, an earlier change
    decision C caps it there too) and passes through untouched.
    """
    target = await directory.lookup_user(target_user_id)
    if target is None:
        raise NotFoundError("no such target user in this tenant")

    role = await Repository(session, Role).get(role_id)
    if role is None:
        raise NotFoundError("no such role in this tenant")
    system = await Repository(session, System).get(role.system_id)
    if system is None:
        raise NotFoundError("the role's system is not in this tenant")

    manager_user_id: uuid.UUID | None = None
    if target.department_id is not None:
        department = await Repository(session, Department).get(target.department_id)
        if department is not None:
            manager_user_id = department.manager_user_id

    membership_role_ids = await directory.list_membership_role_ids(target_user_id)
    sod_rules = await Repository(session, SodRule).list()

    return RequestSnapshot(
        requester_user_id=requester_user_id,
        target=TargetSnapshot(
            user_id=target.id,
            department_manager_user_id=manager_user_id,
            status=TargetStatus(target.status),
            # Clamped at 0: a start date after `now` is zero tenure, not a
            # negative the snapshot type would refuse.
            tenure_days=max(0, (now.date() - target.start_date).days),
            active_grant_role_ids=await directory.list_account_role_ids(target_user_id, now),
            approved_pending_role_ids=await list_approved_role_ids_for_target(
                session, target_user_id
            ),
        ),
        role=RoleSnapshot(
            role_id=role.id,
            privilege=Privilege(role.privilege),
            default_expiry_days=role.default_expiry_days,
            sensitivity=Sensitivity(system.sensitivity),
            system_category=SystemCategory(system.category),
            system_owner_user_id=system.owner_user_id,
        ),
        role_in_department_template=role_id in membership_role_ids,
        requested_expiry_days=requested_expiry_days,
        sod_rules=tuple(
            SodRuleSnapshot(
                rule_id=rule.id,
                role_a_id=rule.role_a_id,
                role_b_id=rule.role_b_id,
                policy_ref=rule.policy_ref,
            )
            for rule in sod_rules
        ),
    )
