"""One golden case, run through the real chat path, answered in the console's format.

The console sends a seat name, not a login, so the seat is looked up in the
seeded data and a short-lived token is minted for that user. Every message then
runs through `run_turn` exactly as a chat does, so each tool still reads tenant
and user from the token and never from the body. The answer is the console's
envelope and nothing else; the prompt versions ride a header (`EvalOutcome`).

Not mounted in production: `EVAL_ROUTES_ENABLED` is off there.
"""

import contextlib
import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from types import TracebackType
from typing import Any, cast

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from helpdesk.agent_api.agent.graph import run_turn
from helpdesk.agent_api.agent.prompts import load_prompt
from helpdesk.agent_api.agent.state import TurnOutcome, TurnState
from helpdesk.agent_api.agent.turn_deps import TurnDeps
from helpdesk.agent_api.agent.turn_metrics import turn_risk_class
from helpdesk.agent_api.schemas.eval import EvalEnvelope, EvalToolCall, EvalTurnRequest
from helpdesk.agent_api.services.mcp_client import ToolCaller
from helpdesk.agent_api.services.model_gateway.client import ModelAnswer, ModelClient
from helpdesk.agent_api.services.tool_proxy import build_tool_caller
from helpdesk.shared.auth.jwt import issue_token
from helpdesk.shared.core.config import Settings
from helpdesk.shared.core.db import tenant_session
from helpdesk.shared.crud.tenants import find_tenant_by_slug
from helpdesk.shared.models import Department, User

__all__ = ["EvalOutcome", "Seat", "build_tool_caller", "resolve_seat", "run_case", "run_turn"]

SEAT_PATTERN = re.compile(r"seed:([a-z]+)\.([a-z_]+)\.([0-9]{1,3})")
"""`seed:<department lowercased>.<principal>.<index>`, matched in full."""

# The seed gives user number `n` of a tenant the start date `HIRING_EPOCH +
# (n * 97) % 2900` days, and numbers a department's users in a row. 97 and 2900
# share no factor, so the date gives the number back, and with it the order
# the golden set's seat index counts in. Copied from `scripts/seed.py`, which
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


class _WatchedModel:
    """The model client, keeping what the gateway said about each call.

    Cost is summed; provider and region are the last call's. Typed as the client
    to the turn, which only calls `complete` and `complete_json`.
    """

    def __init__(self, inner: ModelClient) -> None:
        self._inner = inner
        self.cost = Decimal(0)
        self.provider = ""
        self.region = ""

    def _note(self, answer: ModelAnswer) -> None:
        # No usable cost header counts as nothing.
        with contextlib.suppress(InvalidOperation):
            self.cost += Decimal(answer.gateway_cost_usd)
        self.provider = answer.gateway_provider or self.provider
        self.region = answer.gateway_region or self.region

    async def complete(self, *args: Any, **kwargs: Any) -> ModelAnswer:
        answer: ModelAnswer = await self._inner.complete(*args, **kwargs)
        self._note(answer)
        return answer

    async def complete_json(self, *args: Any, **kwargs: Any) -> Any:
        answer = await self._inner.complete_json(*args, **kwargs)
        self._note(answer.usage)
        return answer


class _RecordingTools:
    """A tool caller that keeps each call's name and arguments."""

    def __init__(self, inner: ToolCaller, calls: list[tuple[str, dict[str, Any]]]) -> None:
        self._inner = inner
        self._calls = calls

    async def __aenter__(self) -> "_RecordingTools":
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
        self._calls.append((name, arguments.model_dump(mode="json")))
        return await self._inner.call_tool(name, arguments)


@dataclass
class EvalOutcome:
    """The envelope and the prompt-version header that goes with it."""

    envelope: EvalEnvelope
    prompt_versions: str


@dataclass
class _Case:
    """What one case has seen so far, across its messages."""

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    role_names: dict[str, str] = field(default_factory=dict)


async def run_case(
    *,
    body: EvalTurnRequest,
    seat: Seat,
    settings: Settings,
    factory: async_sessionmaker[AsyncSession],
    model: ModelClient,
    checkpointer: Any,
) -> EvalOutcome:
    """Replay the case's user messages in one new chat and answer for the last."""
    chat_id = uuid.uuid4()
    watched = _WatchedModel(model)
    case = _Case()
    state: TurnState | None = None
    started = time.perf_counter()
    for message in body.messages:
        if message.role != "user":
            continue
        now = datetime.now(UTC)
        token = issue_token(
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
        tools = _RecordingTools(build_tool_caller(settings, token), case.calls)
        async with tenant_session(factory, seat.tenant_id) as db:
            state = await run_turn(
                deps=TurnDeps(
                    db=db,
                    model=cast(ModelClient, watched),
                    tools=tools,
                    now=_clock(now),
                ),
                tenant_id=seat.tenant_id,
                user_id=seat.user_id,
                session_id=chat_id,
                tenant_slug=seat.tenant_slug,
                message=message.content,
                checkpointer=checkpointer,
            )
        _remember_roles(case, state)
    latency_ms = int((time.perf_counter() - started) * 1000)
    return EvalOutcome(
        envelope=_envelope(case, state, watched, latency_ms),
        prompt_versions=prompt_versions(),
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


def _decision(state: TurnState) -> str | None:
    """The decision of the last tool answer that carried one."""
    for result in reversed(state.tool_results):
        named = result.payload.get("decision")
        if isinstance(named, str):
            return named
    return None


def _envelope(
    case: _Case, state: TurnState | None, watched: _WatchedModel, latency_ms: int
) -> EvalEnvelope:
    last = None if state is None else state.outcome
    return EvalEnvelope(
        intent=None if state is None or state.extraction is None else state.extraction.intent.value,
        tool_calls=[
            EvalToolCall(name=name, args=_call_args(case, args)) for name, args in case.calls
        ],
        decision=None if state is None else _decision(state),
        risk_class=None if state is None else turn_risk_class(state),
        refused=last is TurnOutcome.REFUSE,
        clarified=last in (TurnOutcome.CLARIFY, TurnOutcome.DISAMBIGUATE),
        citations=[] if state is None else _documents(state),
        reply="" if state is None else state.reply or "",
        latency_ms=latency_ms,
        cost_usd=f"{watched.cost:.4f}",
        provider=watched.provider,
        region=watched.region,
    )


def _documents(state: TurnState) -> list[str]:
    """Each cited document once, in first-seen order."""
    seen: dict[str, None] = {}
    for citation in state.citations:
        seen.setdefault(citation.document)
    return list(seen)
