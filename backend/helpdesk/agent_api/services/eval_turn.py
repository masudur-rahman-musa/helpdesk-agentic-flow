"""One conversation case, played through the real chat path, answered per turn.

The console sends a seat name, not a login, so the seat is looked up in the
seeded data and a short-lived token is minted for that user. Every turn then
runs through `run_turn` exactly as a chat does, so each tool still reads tenant
and user from the token and never from the body. `run_conversation` answers one
record per user turn: the reply and that turn's tool calls with their results;
the prompt versions ride a header (`ConversationOutcome`). It scores nothing;
the console's eval engine does.

Not mounted in production: `EVAL_ROUTES_ENABLED` is off there.
"""

import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from helpdesk.agent_api.agent.graph import run_turn
from helpdesk.agent_api.agent.prompts import load_prompt
from helpdesk.agent_api.agent.state import TurnState
from helpdesk.agent_api.agent.turn_deps import TurnDeps
from helpdesk.agent_api.schemas.eval import (
    EvalConversationAnswer,
    EvalConversationCall,
    EvalConversationRequest,
    EvalConversationTurn,
)
from helpdesk.agent_api.services.mcp_client import ToolCaller
from helpdesk.agent_api.services.model_gateway.client import ModelClient
from helpdesk.agent_api.services.tool_proxy import build_tool_caller
from helpdesk.shared.auth.jwt import issue_token
from helpdesk.shared.core.config import Settings
from helpdesk.shared.core.db import tenant_session
from helpdesk.shared.core.errors import ToolError
from helpdesk.shared.crud.tenants import find_tenant_by_slug
from helpdesk.shared.models import Department, User

__all__ = [
    "ConversationOutcome",
    "Seat",
    "build_tool_caller",
    "resolve_seat",
    "run_conversation",
    "run_turn",
]

SEAT_PATTERN = re.compile(r"seed:([a-z]+)\.([a-z_]+)\.([0-9]{1,3})")
"""`seed:<department lowercased>.<principal>.<index>`, matched in full."""

# The seed gives user number `n` of a tenant the start date `HIRING_EPOCH +
# (n * 97) % 2900` days, and numbers a department's users in a row. 97 and 2900
# share no factor, so the date gives the number back, and with it the order
# the conversation cases' seat index counts in. Copied from `scripts/seed.py`, which
# the agent-api image does not ship; `test_eval_routes.py` pins the behaviour.
HIRING_EPOCH_ORDINAL = datetime(2018, 1, 1).toordinal()
START_DATE_STEP = 97
START_DATE_SPAN = 2900
_STEP_INVERSE = pow(START_DATE_STEP, -1, START_DATE_SPAN)

PROMPT_STEPS: tuple[tuple[str, str], ...] = (
    ("intent", "intent_extraction"),
    ("plan", "tool_planning"),
    ("compose", "reply_composition"),
    ("system", "system"),
)
"""The header's name for each step, and the prompt file that step reads."""


@dataclass(frozen=True)
class Seat:
    """A seeded user the case plays: the ids the token and the turn run under."""

    tenant_id: uuid.UUID
    tenant_slug: str
    user_id: uuid.UUID
    department_id: uuid.UUID
    principals: tuple[str, ...]


async def resolve_seat(
    factory: async_sessionmaker[AsyncSession], *, tenant_slug: str, principal_user: str
) -> Seat | None:
    """The seat a case plays, or `None` for an unknown tenant, department, index
    or principal."""
    matched = SEAT_PATTERN.fullmatch(principal_user)
    if matched is None:
        return None
    department_part, principal, index_part = matched.groups()
    async with factory() as session:
        tenant = await find_tenant_by_slug(session, tenant_slug)
    if tenant is None:
        return None
    async with tenant_session(factory, tenant.id) as session:
        result = await session.execute(
            select(User)
            .join(Department, Department.id == User.department_id)
            .where(
                User.tenant_id == tenant.id,
                func.lower(Department.name) == department_part,
            )
        )
        users = sorted(result.scalars().all(), key=_seed_number)
    index = int(index_part)
    if index >= len(users) or principal not in users[index].principals:
        return None
    user = users[index]
    if user.department_id is None:
        return None
    return Seat(
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        user_id=user.id,
        department_id=user.department_id,
        principals=tuple(user.principals),
    )


def _seed_number(user: User) -> int:
    """The user's place in the tenant's seed order, read back from the start date."""
    days = user.start_date.toordinal() - HIRING_EPOCH_ORDINAL
    return (days * _STEP_INVERSE) % START_DATE_SPAN


def prompt_versions() -> str:
    """`intent=<sha256>,plan=...,compose=...,system=...`, the digests the traces record."""
    return ",".join(f"{step}={load_prompt(name).sha256}" for step, name in PROMPT_STEPS)


class _ResultTools:
    """A tool caller that keeps each call's name, arguments and answer, a refused
    call included (`ok` false, the typed code), and lets the refusal go on."""

    def __init__(self, inner: ToolCaller, calls: list["_Call"]) -> None:
        self._inner = inner
        self._calls = calls

    async def __aenter__(self) -> "_ResultTools":
        await self._inner.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._inner.__aexit__(exc_type, exc, tb)

    async def call_tool(self, name: str, arguments: BaseModel) -> Mapping[str, Any]:
        args = arguments.model_dump(mode="json")
        try:
            result = await self._inner.call_tool(name, arguments)
        except ToolError as error:
            self._calls.append(_Call(name, args, False, {}, error.code.value))
            raise
        self._calls.append(_Call(name, args, True, dict(result), None))
        return result


@dataclass
class _Call:
    name: str
    args: dict[str, Any]
    ok: bool
    result: dict[str, Any]
    error_code: str | None


@dataclass
class ConversationOutcome:
    """The per-turn records and the prompt-version header that goes with them."""

    answer: EvalConversationAnswer
    prompt_versions: str


@dataclass
class _Case:
    """What one case has seen so far, across its turns."""

    role_names: dict[str, str] = field(default_factory=dict)


async def run_conversation(
    *,
    body: EvalConversationRequest,
    seat: Seat,
    settings: Settings,
    factory: async_sessionmaker[AsyncSession],
    model: ModelClient,
    checkpointer: Any,
) -> ConversationOutcome:
    """Play the person's turns in one new chat and answer for every turn."""
    chat_id = uuid.uuid4()
    case = _Case()
    records: list[EvalConversationTurn] = []
    for number, message in enumerate(body.turns, start=1):
        now = datetime.now(UTC)
        calls: list[_Call] = []
        tools = _ResultTools(
            build_tool_caller(settings, _seat_token(seat, chat_id, settings, now)), calls
        )
        async with tenant_session(factory, seat.tenant_id) as db:
            state = await run_turn(
                deps=TurnDeps(
                    db=db,
                    model=model,
                    tools=tools,
                    now=_clock(now),
                ),
                tenant_id=seat.tenant_id,
                user_id=seat.user_id,
                session_id=chat_id,
                tenant_slug=seat.tenant_slug,
                message=message,
                checkpointer=checkpointer,
            )
        _remember_roles(case, state)
        records.append(
            EvalConversationTurn(
                turn=number,
                reply=state.reply or "",
                tool_calls=[
                    EvalConversationCall(
                        name=call.name,
                        args=_call_args(case, call.args),
                        ok=call.ok,
                        result=call.result,
                        error_code=call.error_code,
                    )
                    for call in calls
                ],
            )
        )
    return ConversationOutcome(
        answer=EvalConversationAnswer(case_id=body.case_id, turns=records),
        prompt_versions=prompt_versions(),
    )


def _seat_token(seat: Seat, chat_id: uuid.UUID, settings: Settings, now: datetime) -> str:
    """The short-lived token one message runs under, minted for the seat."""
    return issue_token(
        {
            "sub": str(seat.user_id),
            "tenant_id": str(seat.tenant_id),
            "department_id": str(seat.department_id),
            "principals": list(seat.principals),
            "sid": str(chat_id),
        },
        settings.jwt_secret,
        now,
    )


def _clock(at: datetime) -> Callable[[], datetime]:
    """A clock that stands still for the one message it is given to."""
    return lambda: at


def _remember_roles(case: _Case, state: TurnState) -> None:
    """Keep the role ids this turn resolved, so a call's `role_id` reads back as a name."""
    resolved = state.resolved
    if resolved is None or resolved.role_id is None:
        return
    if resolved.system_slug is None or resolved.role_slug is None:
        return
    case.role_names[str(resolved.role_id)] = f"{resolved.system_slug}.{resolved.role_slug}"


def _call_args(case: _Case, args: Mapping[str, Any]) -> dict[str, Any]:
    """The call's arguments, with a `role_id` written as `<system slug>.<role slug>`."""
    shown = dict(args)
    role_id = shown.pop("role_id", None)
    if role_id is not None:
        shown["role"] = case.role_names.get(str(role_id), str(role_id))
    return shown
