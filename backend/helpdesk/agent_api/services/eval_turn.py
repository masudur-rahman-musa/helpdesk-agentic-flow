"""One turn of a conversation case, played through the real chat path.

The console sends a seat name, not a login, so the seat is looked up in the
seeded data and a short-lived token is minted for that user. The turn runs
through `run_loop_turn` exactly as a chat does, so each tool still reads
tenant and user from the token and never from the body. The console plays a
simulated person, so it sends one message per call: the first starts a chat,
later ones name it, and a chat the seat did not start is refused.
`run_conversation` answers the reply, the turn's tool calls with their
results, and the tenant's world before and after the turn; the engine and
prompt versions ride headers (`ConversationOutcome`). It scores nothing; the
console's eval engine does.

Not mounted in production: `EVAL_ROUTES_ENABLED` is off there.
"""

import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from helpdesk.agent_api.agent.loop import LOOP_PROMPT, DbLoopStore, run_loop_turn
from helpdesk.agent_api.agent.prompts import load_prompt
from helpdesk.agent_api.agent.turn_deps import TurnDeps
from helpdesk.agent_api.schemas.eval import (
    EvalConversationAnswer,
    EvalConversationCall,
    EvalConversationRequest,
    EvalWorld,
)
from helpdesk.agent_api.services import eval_world
from helpdesk.agent_api.services.eval_world import seed_number
from helpdesk.agent_api.services.mcp_client import ToolCaller
from helpdesk.agent_api.services.model_gateway.client import ModelClient
from helpdesk.agent_api.services.tool_proxy import build_tool_caller
from helpdesk.shared.auth.jwt import issue_token
from helpdesk.shared.core.config import Settings
from helpdesk.shared.core.db import tenant_session
from helpdesk.shared.core.errors import CODE_REASONS, ToolError
from helpdesk.shared.crud.sessions import get_owned_chat
from helpdesk.shared.crud.tenants import find_tenant_by_slug
from helpdesk.shared.crud.users import list_users_in_department_named
from helpdesk.shared.models import User

__all__ = [
    "ConversationOutcome",
    "Seat",
    "build_tool_caller",
    "owns_chat",
    "read_world",
    "resolve_seat",
    "run_conversation",
    "run_loop_turn",
]

SEAT_PATTERN = re.compile(r"seed:([a-z]+)\.([a-z_]+)\.([0-9]{1,3})")
"""`seed:<department lowercased>.<principal>.<index>`, matched in full."""

ENGINE = "loop"
"""What the engine header says. The loop is the only chat engine."""


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
        found = await list_users_in_department_named(session, tenant.id, department_part)
        users = sorted(found, key=_seed_number)
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
    return seed_number(user.start_date)


def prompt_versions() -> str:
    """`loop=<sha256>`: the digest of the loop's one prompt, as traces record it."""
    return f"loop={load_prompt(LOOP_PROMPT).sha256}"


class _ResultTools:
    """A tool caller that keeps each call's name, arguments and answer, a refused
    call included (`ok` false, the typed code and its fixed reason), and lets the
    refusal go on."""

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
            # The same fixed reason the model reads, never the error's own message.
            reason = error.reason or CODE_REASONS[error.code]
            self._calls.append(_Call(name, args, False, {"reason": reason}, error.code.value))
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
    """The turn's answer, and the engine and prompt versions the headers carry."""

    answer: EvalConversationAnswer
    prompt_versions: str
    engine: str


async def owns_chat(db: AsyncSession, seat: Seat, chat_id: uuid.UUID) -> bool:
    """Whether the chat is one this seat started, in its own tenant."""
    return await get_owned_chat(db, chat_id=chat_id, user_id=seat.user_id) is not None


async def read_world(db: AsyncSession, seat: Seat) -> EvalWorld:
    """The seat's tenant world, as the answer carries it."""
    return await eval_world.read_world(db, seat.tenant_slug)


async def run_conversation(
    *,
    body: EvalConversationRequest,
    seat: Seat,
    settings: Settings,
    factory: async_sessionmaker[AsyncSession],
    model: ModelClient,
) -> ConversationOutcome | None:
    """Play one turn of the case and answer it with the world before and after.

    `None` when the body names a chat this seat did not start: one case must
    never write into another person's chat, so nothing is played."""
    now = datetime.now(UTC)
    chat_id = body.chat_id
    if chat_id is not None:
        async with tenant_session(factory, seat.tenant_id) as db:
            if not await owns_chat(db, seat, chat_id):
                return None
    else:
        chat_id = uuid.uuid4()
    calls: list[_Call] = []
    tools = _ResultTools(
        build_tool_caller(settings, _seat_token(seat, chat_id, settings, now)), calls
    )
    async with tenant_session(factory, seat.tenant_id) as db:
        role_names = await _role_names(db, seat, chat_id)
        before = await read_world(db, seat)
    async with tenant_session(factory, seat.tenant_id) as db:
        state = await run_loop_turn(
            deps=TurnDeps(db=db, model=model, tools=tools, now=_clock(now)),
            tenant_id=seat.tenant_id,
            user_id=seat.user_id,
            session_id=chat_id,
            tenant_slug=seat.tenant_slug,
            message=body.message,
        )
    async with tenant_session(factory, seat.tenant_id) as db:
        after = await read_world(db, seat)
    return ConversationOutcome(
        answer=EvalConversationAnswer(
            chat_id=str(chat_id),
            reply=state.reply or "",
            tool_calls=[
                EvalConversationCall(
                    name=call.name,
                    args=_call_args(role_names, call.args),
                    ok=call.ok,
                    result=call.result,
                    error_code=call.error_code,
                )
                for call in calls
            ],
            world_before=before,
            world_after=after,
        ),
        prompt_versions=prompt_versions(),
        engine=ENGINE,
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


async def _role_names(db: AsyncSession, seat: Seat, chat_id: uuid.UUID) -> dict[str, str]:
    """Every tenant role id as `<system slug>.<role slug>`, so a call's
    `role_id` reads back as a name."""
    store = DbLoopStore(db, session_id=chat_id, user_id=seat.user_id)
    roles = await store.role_ids()
    return {str(role_id): f"{system}.{role}" for (system, role), role_id in roles.items()}


def _call_args(role_names: Mapping[str, str], args: Mapping[str, Any]) -> dict[str, Any]:
    """The call's arguments, with a `role_id` written as `<system slug>.<role slug>`."""
    shown = dict(args)
    role_id = shown.pop("role_id", None)
    if role_id is not None:
        shown["role"] = role_names.get(str(role_id), str(role_id))
    return shown
