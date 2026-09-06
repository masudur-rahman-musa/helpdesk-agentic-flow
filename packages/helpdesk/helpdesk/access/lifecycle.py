"""`onboard_user`: the joiner half of the design's lifecycle (requirement #15).

A joiner is two facts, not one: a person exists, and that person holds what
their department's template says they should. The second is access, so it is
not written here — every role goes through `helpdesk.access.execute_grant`, which re-runs the whole policy engine per role. That
is what makes onboarding obey the design like every other path to a grant:
the expiry a joiner gets is the one the engine computed, never the role's raw
`default_expiry_days`, and a template naming two roles that cannot be held
together is refused rather than granted (pinned by
`test_access_lifecycle.py::test_the_whole_template_is_applied_with_the_engines_expiry_not_the_roles`
and `::test_a_template_role_in_sod_conflict_is_skipped_and_the_rest_stand`).

A refused template role costs that role, not the hire (decision B): it comes
back in `skipped` carrying its the design code, and the joiner keeps the rest.
A broken template is a data defect somebody has to see, and leaving a new
hire with nothing on their first day is the worse of the two failures.

Two passes over the template, which the audit order forces. the design's
`user.onboarded` has to land BEFORE the grants — access recorded against a
person the log has not hired yet reads backwards — and its payload names
which roles were applied and which were skipped, neither of which is known
until the engine has been asked. So the first pass DECIDES (classify only, no
writes) and the second EXECUTES. The two cannot disagree: the deciding pass
runs the same `reclassify` the executing pass runs, in the same sorted role
order, over a directory view that reports the roles already decided as held —
which is exactly what the executing pass will read from the grants it has
written by then. Pinned end to end by
`test_mcp_onboard_user.py::test_the_audit_sequence_is_onboarded_then_the_grants_then_tool_succeeded`.

Everything runs in the caller's transaction: the person, their grants and
every event are flushed, never committed, so a failure afterwards takes all
of them (pinned by `test_access_lifecycle.py::
test_a_duplicate_email_is_refused_before_any_write`).
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.access.directory import DirectoryProvider, DirectoryReader
from helpdesk.access.execute import (
    PolicyBlockedAtExecutionError,
    SodConflictAtExecutionError,
    execute_grant,
    reclassify,
)
from helpdesk.audit import AuditEventInput, append_event
from helpdesk.auth.rbac import Principal
from helpdesk.core.db import bound_tenant_id
from helpdesk.core.repository import find_user_by_email
from helpdesk.errors import ErrorCode, ToolError
from helpdesk.models import Grant, User
from helpdesk.policy.types import TargetStatus

USER_ONBOARDED_EVENT_TYPE = "user.onboarded"
"""the design's own name for "this person was hired".

Appended by this module with the NEW USER as its subject (decision F), not
declared as the calling tool's registry event: the pipeline's event carries
the TOOL as subject and a fixed payload, so it would record that somebody
called a tool and not who was hired. The subject and the
exact payload key set are pinned by `test_access_lifecycle.py::
test_user_onboarded_names_the_new_user_and_carries_exactly_six_keys` and
again through the transport by `test_mcp_onboard_user.py::
test_the_audit_sequence_is_onboarded_then_the_grants_then_tool_succeeded`."""

UNUSABLE_PASSWORD_HASH = "!no-password-set:onboarded-user-has-no-credential"
"""What a joiner's NOT NULL `password_hash` holds until they set one.

Deliberately not a valid argon2 encoding (decision C): `verify_password`
answers False for a malformed hash, so NO input can match this value, and the
constant is greppable in the table — a random real hash would be unmatchable
too, but indistinguishable from a genuine credential. Pinned by
`test_access_lifecycle.py::test_the_unusable_password_hash_verifies_against_nothing`
and, end to end, by `test_mcp_onboard_user.py::
test_login_refuses_an_onboarded_user_exactly_like_an_unknown_one`, which
sends this very string as the password."""

JOINER_PRINCIPALS: tuple[str, ...] = (Principal.EMPLOYEE.value,)
"""What every joiner gets, fixed rather than taken from the input (decision D).

the design's signature has no principals field, and an HR caller able to name
them could mint an `it_admin` through a lifecycle tool — a privilege
escalation path around the design entirely. Pinned as the exact tuple by
`test_access_lifecycle.py::test_the_onboard_constants_and_error_code_are_exactly_the_spec_values`
and read off the created row by `test_mcp_onboard_user.py::
test_the_three_permitted_principals_each_onboard_a_joiner`."""

JOINER_STATUS = TargetStatus.ACTIVE.value
"""A joiner is `active`; `leaver` is an earlier change's direction."""

_ACTOR_KIND = "user"
"""Onboarding is always somebody's call: the design gives the row three
principals, unlike `execute_grant`, which is system-only."""

_SUBJECT_TYPE = "user"


class DuplicateEmailError(ToolError):
    """This tenant already has a user with that address (decision G).

    `POLICY_BLOCKED`, and raised from a read taken BEFORE any write: letting
    the unique index raise instead surfaces an `IntegrityError`, which the
    pipeline maps to `INTERNAL` — a caller-caused condition reported as a
    server fault, telling the caller to retry the one thing that cannot
    work. The code is pinned by `test_access_lifecycle.py::
    test_the_onboard_constants_and_error_code_are_exactly_the_spec_values`
    and the "before any write" half by
    `::test_a_duplicate_email_is_refused_before_any_write`.
    """

    code = ErrorCode.POLICY_BLOCKED


@dataclass(frozen=True)
class SkippedRole:
    """One template role the engine refused, and why (decision B)."""

    role_id: uuid.UUID
    reason_code: ErrorCode
    reason: str


@dataclass(frozen=True)
class OnboardResult:
    """The person, what they hold, and what their template failed to give them."""

    user: User
    applied: tuple[Grant, ...]
    skipped: tuple[SkippedRole, ...]


class _WithDecidedRoles:
    """The directory, answering as if this onboarding's decided roles were live.

    The deciding pass exists because `user.onboarded` must name the applied
    and skipped roles before either grant is written. A pass that classified
    every template role against untouched state would see the joiner holding
    nothing and find no conflict at all — the conflict in a broken template is
    between two roles OF that template, and only appears once the first of
    them is held. This wrapper supplies exactly what the executing pass will
    have written by the same point, so the two passes cannot disagree.

    Read-only by construction: it forwards the three read methods the snapshot
    builder uses and implements no mutation half, so the deciding pass cannot
    write through it even by mistake.
    """

    def __init__(self, inner: DirectoryReader, user_id: uuid.UUID) -> None:
        self._inner = inner
        self._user_id = user_id
        self._decided: set[uuid.UUID] = set()

    def add(self, role_id: uuid.UUID) -> None:
        self._decided.add(role_id)

    async def lookup_user(self, user_id: uuid.UUID) -> User | None:
        return await self._inner.lookup_user(user_id)

    async def list_membership_role_ids(self, user_id: uuid.UUID) -> frozenset[uuid.UUID]:
        return await self._inner.list_membership_role_ids(user_id)

    async def list_account_role_ids(
        self, user_id: uuid.UUID, now: datetime
    ) -> frozenset[uuid.UUID]:
        held = await self._inner.list_account_role_ids(user_id, now)
        if user_id != self._user_id:
            return held
        return held | self._decided


async def onboard_user(
    session: AsyncSession,
    directory: DirectoryProvider,
    *,
    email: str,
    display_name: str,
    department_id: uuid.UUID,
    start_date: date,
    manager_user_id: uuid.UUID,
    created_by_user_id: uuid.UUID,
    actor_ref: str | None = None,
    session_id: uuid.UUID | None = None,
    now: datetime,
) -> OnboardResult:
    """Create the person, apply their department's template, record the hire.

    The caller has already resolved `department_id` and `manager_user_id`
    inside its own tenant and refused either absence `NOT_FOUND`
    (`mcp_server.tools.lifecycle.onboard_user`); what is left here is the
    domain decision. `manager_user_id` is carried into the event and nowhere
    else: the design models management on `departments`, so `users` has no
    manager column to write it to (decision E).

    `now` is keyword-only and supplied by the caller, so the tenure the engine
    reads, every classification and every `granted_at` answer for one instant;
    reading a clock here would let two template roles be classified against
    two different moments.
    """
    if await find_user_by_email(session, email) is not None:
        raise DuplicateEmailError("a user with that email already exists in this tenant")

    user = await directory.create_user(
        email=email,
        display_name=display_name,
        department_id=department_id,
        start_date=start_date,
        principals=JOINER_PRINCIPALS,
        password_hash=UNUSABLE_PASSWORD_HASH,
        status=JOINER_STATUS,
    )

    # Sorted, so the deciding pass and the executing pass walk the template in
    # the same order. Which half of a conflicting pair loses is then a
    # property of the data rather than of the order a set happened to iterate.
    template_role_ids = sorted(await directory.list_department_role_ids(department_id))
    to_apply, skipped = await _decide(
        session,
        directory,
        user_id=user.id,
        created_by_user_id=created_by_user_id,
        template_role_ids=template_role_ids,
        now=now,
    )

    await _append_onboarded(
        session,
        user=user,
        email=email,
        department_id=department_id,
        manager_user_id=manager_user_id,
        applied_role_ids=to_apply,
        skipped=skipped,
        actor_ref=actor_ref if actor_ref is not None else str(created_by_user_id),
        session_id=session_id,
        now=now,
    )

    applied = [
        await execute_grant(
            session,
            directory,
            request_id=None,
            requester_user_id=created_by_user_id,
            target_user_id=user.id,
            role_id=role_id,
            requested_expiry_days=None,
            granted_by_user_id=created_by_user_id,
            actor_ref=actor_ref if actor_ref is not None else str(created_by_user_id),
            session_id=session_id,
            now=now,
        )
        for role_id in to_apply
    ]
    return OnboardResult(user=user, applied=tuple(applied), skipped=tuple(skipped))


async def _decide(
    session: AsyncSession,
    directory: DirectoryProvider,
    *,
    user_id: uuid.UUID,
    created_by_user_id: uuid.UUID,
    template_role_ids: Iterable[uuid.UUID],
    now: datetime,
) -> tuple[list[uuid.UUID], list[SkippedRole]]:
    """Which template roles the engine will take, and why it refuses the rest.

    Classification only — no grant is written and no event is appended, so
    running the engine here costs queries and nothing else. `reclassify` is
    imported from `execute.py` rather than reimplemented: which of the two
    the design codes a refusal carries is decided by the order that function
    tests conflicts and decisions in, and a second copy of that order here
    would be free to drift from the one that actually writes the grants.
    """
    view = _WithDecidedRoles(directory, user_id)
    to_apply: list[uuid.UUID] = []
    skipped: list[SkippedRole] = []
    for role_id in template_role_ids:
        try:
            await reclassify(
                session,
                view,
                requester_user_id=created_by_user_id,
                target_user_id=user_id,
                role_id=role_id,
                requested_expiry_days=None,
                now=now,
            )
        except (SodConflictAtExecutionError, PolicyBlockedAtExecutionError) as refusal:
            skipped.append(
                SkippedRole(role_id=role_id, reason_code=refusal.code, reason=str(refusal))
            )
            continue
        to_apply.append(role_id)
        view.add(role_id)
    return to_apply, skipped


async def _append_onboarded(
    session: AsyncSession,
    *,
    user: User,
    email: str,
    department_id: uuid.UUID,
    manager_user_id: uuid.UUID,
    applied_role_ids: list[uuid.UUID],
    skipped: list[SkippedRole],
    actor_ref: str,
    session_id: uuid.UUID | None,
    now: datetime,
) -> None:
    """the design's hire record, subject the new user, six keys exactly."""
    await append_event(
        session,
        bound_tenant_id(session),
        AuditEventInput(
            occurred_at=now,
            actor_kind=_ACTOR_KIND,
            actor_ref=actor_ref,
            session_id=session_id,
            trace_id=None,
            event_type=USER_ONBOARDED_EVENT_TYPE,
            subject_type=_SUBJECT_TYPE,
            subject_id=str(user.id),
            payload={
                "user_id": str(user.id),
                "email": email,
                "department_id": str(department_id),
                "manager_user_id": str(manager_user_id),
                "applied_role_ids": [str(role_id) for role_id in applied_role_ids],
                "skipped_role_ids": [str(record.role_id) for record in skipped],
            },
        ),
    )


__all__ = [
    "JOINER_PRINCIPALS",
    "JOINER_STATUS",
    "UNUSABLE_PASSWORD_HASH",
    "USER_ONBOARDED_EVENT_TYPE",
    "DuplicateEmailError",
    "OnboardResult",
    "SkippedRole",
    "onboard_user",
]
