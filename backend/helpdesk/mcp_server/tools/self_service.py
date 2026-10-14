"""The three helpdesk mutating tools an earlier change registers (the design).

All three write on behalf of the token's user and nobody else.
Their inputs inherit `MutatingToolInput` from `tools/base.py`, which is
where the extra-fields contract and the idempotency key are documented; it
moved out of this module in an earlier change so a second tool module need not import
this one.

The reset and unlock are simulated (the design): each stamps a timestamp
column on the token's own `users` row and no real
credential or directory action exists anywhere. The reset's "one-time token"
is a constant placeholder that announces itself as fake.

Handlers never see the transport or the token: the pipeline binds the
session and resolves identity before anything here executes. None of these
three handlers reads its idempotency key — they have nowhere to store one —
but that is a fact about them, not a rule about handlers (an earlier change decision
A corrects the claim this docstring used to make).
"""

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field
from sqlalchemy import String
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.mcp_server.core.identity import Identity
from helpdesk.mcp_server.tools.base import MutatingToolInput
from helpdesk.shared.core.errors import NotFoundError
from helpdesk.shared.crud import Repository
from helpdesk.shared.models import Ticket, User

RESET_EVENT_TYPE = "user.password_reset"
"""Declared success event for `reset_own_password` (per-action, an earlier change
decision C; the design is a minimum list, not a closed one)."""

UNLOCK_EVENT_TYPE = "user.account_unlocked"
"""Declared success event for `unlock_own_account`."""

TICKET_EVENT_TYPE = "ticket.created"
"""Declared success event for `create_ticket` — the design's own name."""

RESET_TOKEN_PLACEHOLDER = "FAKE-RESET-TOKEN-0000-simulated-nothing-was-issued"
"""What `reset_own_password` displays instead of a credential.

Obviously fake by construction (the design "simulated reset"): pinned by
`test_reset_own_password_flags_the_token_user_and_returns_a_fake_placeholder`,
which fails on any placeholder that stops announcing itself as fake."""

BODY_MAX_LENGTH = 10_000
"""Cap on `create_ticket.body`. The column is unbounded
`Text`, so this Pydantic bound is the only backstop; the
inclusive boundary is pinned by
`test_mcp_helpdesk_tools.py::test_the_body_boundary_is_inclusive_at_10000`."""

# Read off the column, never retyped: the check and the
# storage are the same number by construction.
_TITLE_TYPE = Ticket.__table__.c.title.type
assert isinstance(_TITLE_TYPE, String), _TITLE_TYPE
assert _TITLE_TYPE.length is not None, "tickets.title must be a sized column"
TITLE_MAX_LENGTH: int = _TITLE_TYPE.length

_OPEN_STATUS = "open"


class TicketKind(StrEnum):
    """The four the design ticket kinds, matched by exact enum membership.

    A StrEnum admits exactly its own values: `"general\\n"`, `"general "` and
    friends are refused (a project rule's shape without a regex), pinned by
    `test_a_kind_outside_the_enum_is_refused_with_no_row`.
    """

    PASSWORD_RESET = "password_reset"
    UNLOCK = "unlock"
    GENERAL = "general"
    ESCALATION = "escalation"


class ResetOwnPasswordInput(MutatingToolInput):
    """No fields beyond the key: the user reset is the one on the token."""


class ResetOwnPasswordOutput(BaseModel):
    reset_token: str
    requested_at: datetime


class UnlockOwnAccountInput(MutatingToolInput):
    """No fields beyond the key: the account is the token user's own."""


class UnlockOwnAccountOutput(BaseModel):
    unlocked_at: datetime


class CreateTicketInput(MutatingToolInput):
    kind: TicketKind
    title: str = Field(max_length=TITLE_MAX_LENGTH)
    body: str = Field(max_length=BODY_MAX_LENGTH)


class CreateTicketOutput(BaseModel):
    ticket_id: uuid.UUID
    kind: TicketKind
    title: str
    status: str


async def _stamp_user(session: AsyncSession, identity: Identity, **field: datetime) -> datetime:
    """Set one timestamp flag on the token's own user row."""
    (when,) = field.values()
    user = await Repository(session, User).update(identity.user_id, **field)
    if user is None:
        raise NotFoundError("no such user in this tenant")
    return when


async def reset_own_password(
    session: AsyncSession, identity: Identity, args: ResetOwnPasswordInput
) -> ResetOwnPasswordOutput:
    """Simulated password reset: stamp the flag, display the fake token."""
    when = await _stamp_user(session, identity, password_reset_requested_at=datetime.now(UTC))
    return ResetOwnPasswordOutput(reset_token=RESET_TOKEN_PLACEHOLDER, requested_at=when)


async def unlock_own_account(
    session: AsyncSession, identity: Identity, args: UnlockOwnAccountInput
) -> UnlockOwnAccountOutput:
    """Simulated account unlock: stamp the flag on the token's user."""
    when = await _stamp_user(session, identity, account_unlocked_at=datetime.now(UTC))
    return UnlockOwnAccountOutput(unlocked_at=when)


async def create_ticket(
    session: AsyncSession, identity: Identity, args: CreateTicketInput
) -> CreateTicketOutput:
    """File a helpdesk ticket for the token's user, status `open`.

    The tenant comes from the bound session (`Repository.create` stamps it)
    and the user from the token; neither is an argument.
    """
    ticket = await Repository(session, Ticket).create(
        user_id=identity.user_id,
        kind=args.kind.value,
        title=args.title,
        body=args.body,
        status=_OPEN_STATUS,
        session_id=None,
    )
    return CreateTicketOutput(
        ticket_id=ticket.id, kind=args.kind, title=ticket.title, status=ticket.status
    )
