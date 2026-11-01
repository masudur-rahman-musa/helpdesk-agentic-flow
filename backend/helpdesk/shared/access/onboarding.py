"""Creating a new user and giving them their department's template roles."""

import re
import uuid
from datetime import date, datetime

from sqlalchemy import String
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.shared.access.directory import DirectoryProvider
from helpdesk.shared.access.execute import execute_grant
from helpdesk.shared.access.lifecycle import (
    _ACTOR_KIND,
    _SUBJECT_TYPE,
    JOINER_PRINCIPALS,
    JOINER_STATUS,
    UNUSABLE_PASSWORD_HASH,
    USER_ONBOARDED_EVENT_TYPE,
    DuplicateEmailError,
    OnboardResult,
    SkippedRole,
    _decide,
)
from helpdesk.shared.audit import AuditEventInput, append_event
from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.crud.users import find_user_by_email
from helpdesk.shared.models import User

__all__ = [
    "EMAIL_MAX_LENGTH",
    "EMAIL_PATTERN",
    "USER_ONBOARDED_EVENT_TYPE",
    "emails_in",
    "onboard_user",
]

# Read off the column, so the input check and the storage never disagree.
_EMAIL_TYPE = User.__table__.c.email.type
assert isinstance(_EMAIL_TYPE, String), _EMAIL_TYPE
assert _EMAIL_TYPE.length is not None, "users.email must be a sized column"
EMAIL_MAX_LENGTH: int = _EMAIL_TYPE.length

EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
r"""Narrow on purpose: one line, one `@`, a dotted domain, no whitespace.

Used with `fullmatch`, so a trailing newline cannot ride into the directory.
"""

_AROUND_AN_ADDRESS = "\"'()<>[]{},;:.!?"
"""Marks a typed address may sit between in a sentence, never part of it."""


def emails_in(text: str) -> list[str]:
    """Every address in `text`, in order. Split on whitespace, so no address
    carries a space or a line end, then stripped of the punctuation around it
    ("mara@x.example," gives "mara@x.example"). A masked `<EMAIL_1>` holds no
    `@`, so it is never one."""
    found = (word.strip(_AROUND_AN_ADDRESS) for word in text.split())
    return [word for word in found if EMAIL_PATTERN.fullmatch(word) is not None]


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

    The caller has already found the department and manager in its own
    tenant. The manager goes only into the event: `users` has no manager
    column, since managers belong to departments. `now` comes from the
    caller so every role is checked against the same moment.
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

    # Sorted, so which half of a conflicting pair loses depends on the data,
    # not on set order.
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
    """The hire event, with the new user as subject."""
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
