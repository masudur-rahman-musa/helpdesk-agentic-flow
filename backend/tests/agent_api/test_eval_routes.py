"""`POST /admin/test/reset` and `POST /v1/eval/conversation`, the routes the
companion console's eval runner calls.

Written before the implementation exists. Surface under test, and the seams
the cases below use. The implementer must provide exactly these names:

``helpdesk.shared.core.config.Settings.eval_routes_enabled`` (env
``EVAL_ROUTES_ENABLED``, default false). Both routes answer only when it is
true, and are a typed 404 ``{"code": "NOT_FOUND"}`` otherwise. Both join the
open paths only when it is true; when it is false they still need a token.

``helpdesk.agent_api.services.eval_turn`` holds the turn. Six names in it
are looked up at call time, so a test can swap them and no database, tool
server or model is needed:

``resolve_seat(factory, *, tenant_slug, principal_user)``
    async; the seat's user or ``None`` for an unknown tenant or seat. The
    result is read only through the attributes ``tenant_id``, ``tenant_slug``,
    ``user_id``, ``department_id`` and ``principals``.
``run_loop_turn(**kwargs)``
    the loop's own ``run_loop_turn`` (same keyword-only signature), called once
    per request, with ``deps.db`` bound to the seat's tenant, a
    ``deps.tools`` that records the calls made, and ``deps.model`` the app's
    model client (watched, so the headers of every call are kept).
``build_tool_caller(settings, bearer_token)``
    the same seam ``routes/chat.py`` has: called with the token minted for
    the seat.
``DbLoopStore(db, *, session_id, user_id)``
    read once per request for ``role_ids()``, the tenant's roles, so a
    call's ``role_id`` reads back as ``<system slug>.<role slug>``.
``owns_chat(db, seat, chat_id) -> bool``
    async; whether the chat is the seat's own. Asked only on a later turn
    (``chat_id`` given), never on the first.
``read_world(db, seat) -> EvalWorld``
    async; the seat's tenant world. Called once before and once after the
    loop turn, so a fake that changes between calls shows only in the world
    after.

The route plays ONE turn per call. The answer is the chat id, the reply, the
tool calls and the world before and after, and the loop's prompt version rides
the ``X-Prompt-Versions`` header.

Cost: no backing service, no socket. The database is a recording session
factory over an empty directory, the loop a scripted function, the tool server a recording stub and
the gateway an ``httpx.MockTransport``. Each case sets its own settings and
``app.state`` through ``monkeypatch``, so nothing is shared between cases.
"""

import asyncio
import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import pytest
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.agent_api.agent.loop_tools import DepartmentRow
from helpdesk.agent_api.agent.prompts import PROMPTS_DIR
from helpdesk.agent_api.agent.state import TurnOutcome, TurnState
from helpdesk.agent_api.api.router import app
from helpdesk.agent_api.schemas import eval as eval_schemas
from helpdesk.agent_api.services import eval_turn
from helpdesk.agent_api.services.mcp_client import RemoteNotFoundError
from helpdesk.agent_api.services.model_gateway.client import ModelAlias, ModelClient
from helpdesk.shared.auth.jwt import issue_token, verify_token
from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.core.errors import ErrorCode, ToolError
from helpdesk.shared.models.conversation import MAX_MESSAGE_CHARS

RESET_PATH = "/admin/test/reset"

# Obviously fake, tests only: none of these is a credential.
TEST_JWT_SIGNING_PHRASE = "eval-routes-test-jwt-secret-0123456789abcdef"
DATABASE_URL = "postgresql+asyncpg://fake:fake@db.invalid:5432/fake_eval"
REDIS_URL = "redis://redis.invalid:6379/0"
QDRANT_URL = "http://qdrant.invalid:6333"
MCP_URL = "http://mcp.invalid:8081/mcp"
GATEWAY_URL = "http://gateway.invalid:8080/v1"

TENANT_SLUG = "nordlicht"
PRINCIPAL_USER = "seed:production.employee.5"

SEAT_TENANT_ID = uuid.UUID("a1111111-1111-5111-8111-111111111111")
SEAT_USER_ID = uuid.UUID("a2222222-2222-5222-8222-222222222222")
SEAT_DEPARTMENT_ID = uuid.UUID("a3333333-3333-5333-8333-333333333333")
ROLE_ID = uuid.UUID("a4444444-4444-5444-8444-444444444444")

# --- the fakes ----------------------------------------------------------------


@dataclass(frozen=True)
class FakeSeat:
    """What `resolve_seat` hands back: the five attributes the turn reads."""

    tenant_id: uuid.UUID = SEAT_TENANT_ID
    tenant_slug: str = TENANT_SLUG
    user_id: uuid.UUID = SEAT_USER_ID
    department_id: uuid.UUID = SEAT_DEPARTMENT_ID
    principals: tuple[str, ...] = ("employee",)


class _NoRows:
    """A query result with no rows, as an empty directory answers."""

    def scalars(self) -> "_NoRows":
        return self

    def all(self) -> list[Any]:
        return []


class RecordingSession:
    """Stands in for an `AsyncSession`: it keeps the tenant bound to it, and
    every query it is asked finds no rows."""

    def __init__(self) -> None:
        self.info: dict[str, Any] = {}
        self.commits = 0
        self.statements: list[Any] = []

    async def commit(self) -> None:
        self.commits += 1

    async def execute(self, statement: Any) -> _NoRows:
        self.statements.append(statement)
        return _NoRows()


class RecordingFactory:
    """Stands in for the session factory; opens a `RecordingSession`."""

    def __init__(self) -> None:
        self.sessions: list[RecordingSession] = []

    def __call__(self) -> AbstractAsyncContextManager[RecordingSession]:
        return self._open()

    @asynccontextmanager
    async def _open(self) -> AsyncIterator[RecordingSession]:
        session = RecordingSession()
        self.sessions.append(session)
        yield session


class RoleArgs(BaseModel):
    """The one argument the scripted call carries: the role, by id."""

    role_id: uuid.UUID


class RecordingCaller:
    """A tool caller that answers a fixed payload and keeps what it was asked."""

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self._payload = payload
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> "RecordingCaller":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def call_tool(self, name: str, arguments: BaseModel) -> Mapping[str, Any]:
        self.calls.append((name, arguments.model_dump(mode="json")))
        return self._payload


@dataclass(frozen=True)
class Step:
    """What one scripted turn does: the state it ends in, the tool it calls
    and the gateway calls it makes."""

    update: Mapping[str, Any]
    tool_call: str | None = None
    model_calls: int = 0


class ScriptedTurns:
    """A `run_loop_turn` that plays one `Step` per message and records every call."""

    def __init__(self, steps: Sequence[Step]) -> None:
        self._steps = list(steps)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> TurnState:
        index = len(self.calls)
        self.calls.append(kwargs)
        if index >= len(self._steps):
            raise AssertionError(f"turn {index + 1} ran; only {len(self._steps)} were scripted")
        step = self._steps[index]
        deps = kwargs["deps"]
        async with deps.tools:
            if step.tool_call is not None:
                await deps.tools.call_tool(step.tool_call, RoleArgs(role_id=ROLE_ID))
        for _ in range(step.model_calls):
            await deps.model.complete(
                [{"role": "user", "content": "fake"}],
                alias=ModelAlias.AGENT_DEFAULT,
                tenant_slug=kwargs["tenant_slug"],
            )
        return TurnState(
            tenant_id=kwargs["tenant_id"],
            user_id=kwargs["user_id"],
            session_id=kwargs["session_id"],
            tenant_slug=kwargs["tenant_slug"],
            user_message=kwargs["message"],
            **step.update,
        )


@dataclass
class Harness:
    """Everything one case watches."""

    turns: ScriptedTurns
    factory: RecordingFactory
    caller: RecordingCaller
    tokens: list[str]
    seat_lookups: list[tuple[str, str]]


def _plain(outcome: TurnOutcome = TurnOutcome.DONE, **more: Any) -> Mapping[str, Any]:
    return {"outcome": outcome, "reply": f"reply after {outcome.value}", **more}


def _gateway_handler(
    seen: list[httpx.Request],
    *,
    headers: Sequence[Mapping[str, str]] = (),
) -> Callable[[httpx.Request], httpx.Response]:
    """A gateway answering each call with the next header set, then the last."""

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        index = min(len(seen) - 1, len(headers) - 1) if headers else -1
        return httpx.Response(
            200,
            headers=dict(headers[index]) if index >= 0 else {},
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    return handle


@pytest.fixture
def eval_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Obviously fake settings. Nothing here is dialled: no lifespan is entered."""
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setenv("QDRANT_URL", QDRANT_URL)
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SIGNING_PHRASE)
    monkeypatch.setenv("MCP_URL", MCP_URL)
    monkeypatch.setenv("EVAL_ROUTES_ENABLED", "true")


@pytest.fixture
def gateway_requests() -> list[httpx.Request]:
    return []


@pytest.fixture
def install(
    eval_env: None,
    monkeypatch: pytest.MonkeyPatch,
    gateway_requests: list[httpx.Request],
) -> Iterator[Callable[..., Harness]]:
    """Swap in the fakes; call it with the steps and, optionally, gateway headers."""

    def build(
        steps: Sequence[Step],
        *,
        gateway_headers: Sequence[Mapping[str, str]] = (),
        seats: Mapping[tuple[str, str], FakeSeat] | None = None,
    ) -> Harness:
        known = {(TENANT_SLUG, PRINCIPAL_USER): FakeSeat()} if seats is None else dict(seats)
        turns = ScriptedTurns(steps)
        factory = RecordingFactory()
        caller = RecordingCaller({"ok": True})
        tokens: list[str] = []
        lookups: list[tuple[str, str]] = []

        async def resolve(
            _factory: object, *, tenant_slug: str, principal_user: str
        ) -> FakeSeat | None:
            lookups.append((tenant_slug, principal_user))
            return known.get((tenant_slug, principal_user))

        def build_caller(_settings: object, bearer_token: str) -> RecordingCaller:
            tokens.append(bearer_token)
            return caller

        monkeypatch.setattr(eval_turn, "resolve_seat", resolve)
        monkeypatch.setattr(eval_turn, "run_loop_turn", turns)
        monkeypatch.setattr(eval_turn, "build_tool_caller", build_caller)
        model = ModelClient(
            base_url=GATEWAY_URL,
            api_keys={TENANT_SLUG: "fake-eval-key"},
            transport=httpx.MockTransport(
                _gateway_handler(gateway_requests, headers=gateway_headers)
            ),
        )
        monkeypatch.setattr(app.state, "session_factory", factory, raising=False)
        monkeypatch.setattr(app.state, "model", model, raising=False)
        return Harness(turns, factory, caller, tokens, lookups)

    yield build


def _drive(scenario: Callable[[httpx.AsyncClient], Awaitable[httpx.Response]]) -> httpx.Response:
    async def runner() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await scenario(client)

    return asyncio.run(runner())


def _login_token() -> str:
    return issue_token(
        {
            "sub": str(SEAT_USER_ID),
            "tenant_id": str(SEAT_TENANT_ID),
            "department_id": str(SEAT_DEPARTMENT_ID),
            "principals": ["employee"],
            "sid": str(uuid.UUID(int=7)),
        },
        TEST_JWT_SIGNING_PHRASE,
        datetime.now(UTC),
    )


def _post_reset(*, token: str | None = None) -> httpx.Response:
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}

    async def scenario(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post(RESET_PATH, headers=headers)

    return _drive(scenario)


# --- the switch ---------------------------------------------------------------


def test_eval_routes_are_404_when_switched_off(
    install: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a reset that answers in production: an implementation that
    ignores `EVAL_ROUTES_ENABLED` (always on, or on whenever a token is present)
    would answer 204 here instead of 404, and a route that is simply not mounted
    would answer FastAPI's own `{"detail": ...}`, not the typed `NOT_FOUND`. It
    also catches an open-path list that skips the token check while the setting
    is false: the reset must still turn away a request with no bearer.

    The fakes are installed so the ON path would succeed (204): the 404 can only
    come from the switch.
    """
    harness = install([Step(update=_plain())])
    monkeypatch.setenv("EVAL_ROUTES_ENABLED", "false")

    reset = _post_reset(token=_login_token())

    assert (reset.status_code, reset.json()) == (404, {"code": ErrorCode.NOT_FOUND.value})
    assert harness.turns.calls == []
    assert harness.seat_lookups == []

    unauthenticated = _post_reset()
    assert unauthenticated.status_code == 401
    assert unauthenticated.json() == {"code": ErrorCode.FORBIDDEN.value}


def _once_valid_turn_body() -> dict[str, Any]:
    """A body the retired `/v1/eval/turn` route used to accept."""
    return {
        "case_id": "aut-001",
        "tenant": TENANT_SLUG,
        "principal_user": PRINCIPAL_USER,
        "messages": [{"role": "user", "content": "hello"}],
    }


def test_the_single_reply_route_is_gone(install: Callable[..., Harness]) -> None:
    """Catches an implementation that leaves `POST /v1/eval/turn` mounted (or
    answers it with anything but 404): the old route ran the old pipeline and
    nothing reads it any more.

    The switch is on, a valid bearer is sent, the seat is known and a turn is
    scripted, so a surviving route would answer 200; the 404 can only come
    from the route being gone. No seat lookup and no turn may run.
    """
    harness = install([Step(update=_plain())])

    async def scenario(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post(
            "/v1/eval/turn",
            json=_once_valid_turn_body(),
            headers={"Authorization": f"Bearer {_login_token()}"},
        )

    response = _drive(scenario)

    assert response.status_code == 404
    assert harness.turns.calls == []
    assert harness.seat_lookups == []


def test_the_eval_modules_hold_only_the_conversation_names() -> None:
    """Catches a retirement that leaves the old route's code behind (its models
    in `schemas/eval.py`, its runner and outcome in `eval_turn`), or one that
    cuts too far and takes a name the conversation route still needs.

    Both halves are asserted: the four old models and two old names are absent,
    and the three shared names are still there.
    """
    for gone in ("EvalMessage", "EvalTurnRequest", "EvalToolCall", "EvalEnvelope"):
        assert not hasattr(eval_schemas, gone), gone
    for gone in ("run_case", "EvalOutcome"):
        assert not hasattr(eval_turn, gone), gone
    assert hasattr(eval_turn, "run_conversation")
    assert hasattr(eval_turn, "resolve_seat")
    assert hasattr(eval_schemas, "EvalConversationRequest")


def test_reset_answers_204_when_switched_on(install: Callable[..., Harness]) -> None:
    """Catches a reset that wants a body or a token (the console sends neither),
    or that answers 200 with a body: the console accepts any 2xx, but the contract
    is 204, an empty answer, because the route clears nothing.

    No bearer is sent, so an implementation that left the path off the open
    list would answer 401 here.
    """
    install([])

    response = _post_reset()

    assert response.status_code == 204
    assert response.content == b""


# --- the conversation route ---------------------------------------
#
# `POST /v1/eval/conversation` plays one turn. The same seams are swapped
# (`resolve_seat`, `run_loop_turn`, `build_tool_caller`, `DbLoopStore`), plus
# `owns_chat` and `read_world`, so no database, tool server or model is needed.

CONVERSATION_PATH = "/v1/eval/conversation"
DW_ANALYST = "data-warehouse.analyst"
DW_ROLE_ID = uuid.UUID("a6666666-6666-5666-8666-666666666666")
OTHER_USER_ID = uuid.UUID("a7777777-7777-5777-8777-777777777777")
CHAT_ID = uuid.UUID("c1111111-1111-4111-8111-111111111111")
WORLD_LISTS = {"requests", "grants", "tickets", "people", "reviews"}


def empty_world() -> "eval_schemas.EvalWorld":
    return eval_schemas.EvalWorld(requests=[], grants=[], tickets=[], people=[], reviews=[])


def _world_before() -> "eval_schemas.EvalWorld":
    return eval_schemas.EvalWorld(
        requests=[
            eval_schemas.WorldRequest(
                number=1,
                requester="sales.3",
                target="sales.3",
                role="wiki.viewer",
                status="pending",
            )
        ],
        grants=[],
        tickets=[],
        people=[
            eval_schemas.WorldPerson(
                person="sales.3",
                department="Sales",
                status="active",
                reset_asked=False,
                unlocked=False,
            )
        ],
        reviews=[],
    )


def _world_after() -> "eval_schemas.EvalWorld":
    return eval_schemas.EvalWorld(
        requests=[
            eval_schemas.WorldRequest(
                number=1,
                requester="sales.3",
                target="sales.3",
                role="wiki.viewer",
                status="executed",
            )
        ],
        grants=[
            eval_schemas.WorldGrant(
                person="sales.3", role="wiki.viewer", status="active", expires_at=None
            )
        ],
        tickets=[
            eval_schemas.WorldTicket(number=4, person="sales.3", kind="general", status="open")
        ],
        people=[
            eval_schemas.WorldPerson(
                person="sales.3",
                department="Sales",
                status="active",
                reset_asked=True,
                unlocked=False,
            )
        ],
        reviews=[eval_schemas.WorldReview(status="open", items_decided=2)],
    )


async def read_empty_world(db: object, seat: object) -> "eval_schemas.EvalWorld":
    """A `read_world` that finds nothing, for a test that does not look at the world."""
    return empty_world()


@dataclass(frozen=True)
class ConvStep:
    """One scripted turn: the state it ends in and the tool calls it makes, in order.

    A call that raises is caught here, as the real loop turns a tool error
    into a failed tool result, so the turn still answers.
    """

    update: Mapping[str, Any]
    calls: tuple[tuple[str, uuid.UUID], ...] = ()


class ConvTurns:
    """A `run_loop_turn` that plays one `ConvStep` per message."""

    def __init__(self, steps: Sequence[ConvStep]) -> None:
        self._steps = list(steps)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> TurnState:
        index = len(self.calls)
        self.calls.append(kwargs)
        if index >= len(self._steps):
            raise AssertionError(f"turn {index + 1} ran; only {len(self._steps)} were scripted")
        step = self._steps[index]
        async with kwargs["deps"].tools:
            for name, role_id in step.calls:
                try:
                    await kwargs["deps"].tools.call_tool(name, RoleArgs(role_id=role_id))
                except ToolError:
                    continue
        return TurnState(
            tenant_id=kwargs["tenant_id"],
            user_id=kwargs["user_id"],
            session_id=kwargs["session_id"],
            tenant_slug=kwargs["tenant_slug"],
            user_message=kwargs["message"],
            **step.update,
        )


class StubCaller:
    """A tool caller answering a fixed mapping per tool, or raising the error named."""

    def __init__(
        self, answers: Mapping[str, Mapping[str, Any]], failures: Mapping[str, ToolError]
    ) -> None:
        self._answers = answers
        self._failures = failures

    async def __aenter__(self) -> "StubCaller":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def call_tool(self, name: str, arguments: BaseModel) -> Mapping[str, Any]:
        if name in self._failures:
            raise self._failures[name]
        return self._answers[name]


def _about(system: str, role: str) -> Mapping[str, Any]:
    return {"outcome": TurnOutcome.DONE, "reply": f"reply about {system}.{role}"}


TENANT_ROLES: Mapping[tuple[str, str], uuid.UUID] = {
    ("data-warehouse", "analyst"): DW_ROLE_ID,
    ("wiki", "viewer"): ROLE_ID,
}
"""The tenant's role list the fake directory answers."""


class FakeDirectory:
    """Stands in for `DbLoopStore`: only `role_ids` is read, and it is counted."""

    reads = 0

    def __init__(self, db: object, *, session_id: uuid.UUID, user_id: uuid.UUID) -> None:
        self._user_id = user_id

    async def role_ids(self) -> Mapping[tuple[str, str], uuid.UUID]:
        FakeDirectory.reads += 1
        return TENANT_ROLES

    async def departments(self) -> Sequence[DepartmentRow]:
        return []


class ChatBook:
    """Stands in for `owns_chat`: the (user, chat) pairs that exist, and who asked."""

    def __init__(self, owned: Sequence[tuple[uuid.UUID, uuid.UUID]]) -> None:
        self.owned = set(owned)
        self.asked: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def __call__(self, db: object, seat: Any, chat_id: Any) -> bool:
        pair = (seat.user_id, uuid.UUID(str(chat_id)))
        self.asked.append(pair)
        return pair in self.owned


class WorldReads:
    """Stands in for `read_world`: the world before the loop turn has been played
    and the world after, told apart by how many turns have run when it is read."""

    def __init__(
        self,
        turns: ConvTurns,
        before: "eval_schemas.EvalWorld",
        after: "eval_schemas.EvalWorld",
    ) -> None:
        self._turns = turns
        self._before = before
        self._after = after
        self.reads: list[tuple[int, Any, Any]] = []

    async def __call__(self, db: Any, seat: Any) -> "eval_schemas.EvalWorld":
        played = len(self._turns.calls)
        self.reads.append((played, db, seat))
        return self._before if played == 0 else self._after


@dataclass
class ConvEnv:
    """Everything one conversation case watches."""

    harness: Harness
    turns: ConvTurns
    chats: ChatBook
    worlds: WorldReads


@pytest.fixture
def install_conversation(
    install: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> Callable[..., ConvEnv]:
    """`install`, then the turn's own scripted turns, stub caller, chat book and world."""

    def build(
        steps: Sequence[ConvStep],
        *,
        answers: Mapping[str, Mapping[str, Any]] | None = None,
        failures: Mapping[str, ToolError] | None = None,
        owned: Sequence[tuple[uuid.UUID, uuid.UUID]] = (),
        world_before: "eval_schemas.EvalWorld | None" = None,
        world_after: "eval_schemas.EvalWorld | None" = None,
    ) -> ConvEnv:
        harness = install([])
        turns = ConvTurns(steps)
        tokens = harness.tokens
        stub = StubCaller(answers or {}, failures or {})
        chats = ChatBook(owned)
        worlds = WorldReads(turns, world_before or empty_world(), world_after or empty_world())

        def build_caller(_settings: object, bearer_token: str) -> StubCaller:
            tokens.append(bearer_token)
            return stub

        monkeypatch.setattr(eval_turn, "run_loop_turn", turns)
        monkeypatch.setattr(eval_turn, "build_tool_caller", build_caller)
        monkeypatch.setattr(eval_turn, "DbLoopStore", FakeDirectory)
        monkeypatch.setattr(eval_turn, "owns_chat", chats)
        monkeypatch.setattr(eval_turn, "read_world", worlds)
        monkeypatch.setattr(FakeDirectory, "reads", 0)
        return ConvEnv(harness, turns, chats, worlds)

    return build


def _conv_body(
    *,
    message: str = "hello",
    chat_id: str | None = None,
    case_id: str = "case-001",
    tenant: str = TENANT_SLUG,
    principal_user: str = PRINCIPAL_USER,
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "tenant": tenant,
        "principal_user": principal_user,
        "chat_id": chat_id,
        "message": message,
    }


def _post_conversation(body: Mapping[str, Any], *, token: str | None = None) -> httpx.Response:
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}

    async def scenario(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post(CONVERSATION_PATH, json=dict(body), headers=headers)

    return _drive(scenario)


def test_the_conversation_route_is_404_when_switched_off(
    install_conversation: Callable[..., ConvEnv], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a conversation route that answers in production: one that ignores
    `EVAL_ROUTES_ENABLED`, or is not mounted (FastAPI's `{"detail"}` instead of
    the typed `NOT_FOUND`).

    The fakes would make the switched-on route answer 200, so the 404 can only
    come from the switch.
    """
    env = install_conversation([ConvStep(update=_plain())])
    monkeypatch.setenv("EVAL_ROUTES_ENABLED", "false")

    response = _post_conversation(_conv_body(), token=_login_token())

    assert (response.status_code, response.json()) == (404, {"code": ErrorCode.NOT_FOUND.value})
    assert env.turns.calls == []
    assert env.harness.seat_lookups == []
    assert env.worlds.reads == []


def test_a_first_turn_starts_a_chat_and_answers_its_id(
    install_conversation: Callable[..., ConvEnv],
) -> None:
    """Catches a first turn that answers no chat id, a fixed or reused id, an id
    that is not a new random uuid, an id the loop did not run under, a first
    turn that asks whether the chat is owned (there is no chat yet), and an
    answer that carries any field beyond the five the console reads.

    Two first turns are played: their ids must differ, so a constant id fails.
    The loop's own session id is compared with the answered one, so an id made
    for the answer alone fails.
    """
    env = install_conversation(
        [
            ConvStep(update={"outcome": TurnOutcome.CLARIFY, "reply": "first reply"}),
            ConvStep(update={"outcome": TurnOutcome.DONE, "reply": "second reply"}),
        ]
    )

    first = _post_conversation(_conv_body(message="one"))
    second = _post_conversation(_conv_body(message="two", chat_id=None))

    assert first.status_code == second.status_code == 200
    body = first.json()
    assert set(body) == {"chat_id", "reply", "tool_calls", "world_before", "world_after"}
    assert body["reply"] == "first reply"
    assert body["tool_calls"] == []
    parsed = uuid.UUID(body["chat_id"])
    assert parsed.version == 4
    assert str(parsed) == body["chat_id"]
    assert env.turns.calls[0]["session_id"] == parsed
    assert env.turns.calls[1]["session_id"] == uuid.UUID(second.json()["chat_id"])
    assert second.json()["chat_id"] != body["chat_id"]
    assert [call["message"] for call in env.turns.calls] == ["one", "two"]
    assert env.chats.asked == []


def test_a_second_turn_continues_the_same_chat(
    install_conversation: Callable[..., ConvEnv],
) -> None:
    """Catches a later turn that starts a new chat instead of continuing the one
    given, that answers a different id, that mints its token for a different
    session, or that never asks whether the seat owns the chat.

    The chat is owned by the seat, so the ownership answer is yes; the loop must
    run under that exact id, with the token's session claim the same, and the
    question must name the seat's user and the chat.
    """
    env = install_conversation([ConvStep(update=_plain())], owned=[(SEAT_USER_ID, CHAT_ID)])

    response = _post_conversation(_conv_body(message="and then?", chat_id=str(CHAT_ID)))

    assert response.status_code == 200
    assert response.json()["chat_id"] == str(CHAT_ID)
    assert [call["session_id"] for call in env.turns.calls] == [CHAT_ID]
    assert [call["message"] for call in env.turns.calls] == ["and then?"]
    assert env.chats.asked == [(SEAT_USER_ID, CHAT_ID)]
    assert env.harness.tokens
    for token in env.harness.tokens:
        claims = verify_token(token, TEST_JWT_SIGNING_PHRASE, datetime.now(UTC))
        assert claims.sid == str(CHAT_ID)


def test_each_turn_answers_the_world_before_and_after(
    install_conversation: Callable[..., ConvEnv],
) -> None:
    """Catches an answer whose two worlds are the same read (both before or both
    after the turn), a world read for the wrong session or seat, a world
    missing one of its five lists, and a world read more or less than twice.

    The fake world returns one state while no turn has run and another after,
    so the answered `world_before` can only be the first and `world_after`
    only the second if the reads bracket the loop turn. The read gets the
    database session bound to the seat's tenant and the seat itself.
    """
    env = install_conversation(
        [ConvStep(update=_plain())], world_before=_world_before(), world_after=_world_after()
    )

    response = _post_conversation(_conv_body())

    assert response.status_code == 200
    body = response.json()
    assert set(body["world_before"]) == WORLD_LISTS
    assert set(body["world_after"]) == WORLD_LISTS
    assert body["world_before"] == _world_before().model_dump(mode="json")
    assert body["world_after"] == _world_after().model_dump(mode="json")
    assert [played for played, _, _ in env.worlds.reads] == [0, 1]
    for _, db, seat in env.worlds.reads:
        assert bound_tenant_id(cast(AsyncSession, db)) == SEAT_TENANT_ID
        assert seat.user_id == SEAT_USER_ID


def test_a_chat_id_of_another_seat_is_refused(
    install_conversation: Callable[..., ConvEnv],
) -> None:
    """Catches a route that plays a turn into a chat that belongs to another
    person, so one case could write into someone else's chat: an
    implementation that trusts the `chat_id` of the body.

    The chat exists, but under another user, and the same chat id is answered
    200 for its own seat in the test above, so the 422 can only come from the
    ownership answer. The loop must not run and no token may be minted.
    """
    env = install_conversation([ConvStep(update=_plain())], owned=[(OTHER_USER_ID, CHAT_ID)])

    response = _post_conversation(_conv_body(chat_id=str(CHAT_ID)))

    assert response.status_code == 422
    assert response.json() == {"code": ErrorCode.VALIDATION.value}
    assert env.chats.asked == [(SEAT_USER_ID, CHAT_ID)]
    assert env.turns.calls == []
    assert env.harness.tokens == []


@pytest.mark.parametrize(
    "chat_id",
    [
        pytest.param(str(uuid.UUID(int=12345)), id="well-formed-but-unknown"),
        pytest.param("not-a-uuid", id="not-a-uuid"),
        pytest.param("", id="empty"),
    ],
)
def test_an_unknown_chat_id_is_refused(
    install_conversation: Callable[..., ConvEnv], chat_id: str
) -> None:
    """Catches a later turn with an id the server never started that is played
    anyway (as a new chat or as that id), and an id that is not a uuid
    answered 200 or 500 instead of 422.

    No chat exists for anyone, so the ownership answer is no; nothing may run.
    """
    env = install_conversation([ConvStep(update=_plain())])

    response = _post_conversation(_conv_body(chat_id=chat_id))

    assert response.status_code == 422
    assert env.turns.calls == []
    assert env.harness.tokens == []


def test_an_unknown_chat_id_answers_the_typed_validation_code(
    install_conversation: Callable[..., ConvEnv],
) -> None:
    """Catches an unknown chat answered with a bare detail or another code
    (`NOT_FOUND`, `FORBIDDEN`) instead of the typed `VALIDATION`, which would
    tell a caller that a chat exists under someone else."""
    install_conversation([ConvStep(update=_plain())])

    response = _post_conversation(_conv_body(chat_id=str(uuid.UUID(int=12345))))

    assert (response.status_code, response.json()) == (422, {"code": ErrorCode.VALIDATION.value})


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            {
                "case_id": "conv-001",
                "tenant": TENANT_SLUG,
                "principal_user": PRINCIPAL_USER,
                "turns": ["one", "two"],
            },
            id="old-body",
        ),
        pytest.param(_conv_body(case_id="conv-001"), id="conv-id-on-a-one-turn-body"),
        pytest.param(_conv_body(case_id="conv-045"), id="last-old-id"),
    ],
)
def test_the_old_many_turn_body_is_refused(
    install_conversation: Callable[..., ConvEnv], body: Mapping[str, Any]
) -> None:
    """Catches a route that still takes the old body (a `turns` list), or one
    that takes the old `conv-` ids so old and new scores could mix.

    The middle and last rows are complete one-turn bodies except for the id,
    so only the id pattern can refuse them.
    """
    env = install_conversation([ConvStep(update=_plain()) for _ in range(2)])

    response = _post_conversation(body)

    assert response.status_code == 422
    assert env.turns.calls == []


@pytest.mark.parametrize(
    ("case_id", "status"),
    [
        pytest.param("case-001", 200, id="first"),
        pytest.param("case-000", 200, id="case-000"),
        pytest.param("case-999", 200, id="case-999"),
        pytest.param("case-12", 422, id="two-digit-id"),
        pytest.param("case-1000", 422, id="four-digit-id"),
        pytest.param("case-001\n", 422, id="id-trailing-newline"),
        pytest.param("case-001\r\n", 422, id="id-trailing-crlf"),
        pytest.param("case-001 ", 422, id="id-trailing-space"),
        pytest.param("xcase-001", 422, id="id-leading-text"),
    ],
)
def test_the_case_id_is_case_and_three_digits(
    install_conversation: Callable[..., ConvEnv], case_id: str, status: int
) -> None:
    """Catches an id pattern looser than three digits or unanchored (a trailing
    newline, CRLF or space, or leading text let through) and a range that
    refuses `case-000` or `case-999`.
    """
    env = install_conversation([ConvStep(update=_plain())])

    response = _post_conversation(_conv_body(case_id=case_id))

    assert response.status_code == status
    assert len(env.turns.calls) == (1 if status == 200 else 0)


@pytest.mark.parametrize(
    ("length", "status"),
    [
        pytest.param(1, 200, id="one-character"),
        pytest.param(MAX_MESSAGE_CHARS - 1, 200, id="one-under-the-limit"),
        pytest.param(MAX_MESSAGE_CHARS, 200, id="at-the-limit"),
        pytest.param(MAX_MESSAGE_CHARS + 1, 422, id="one-over-the-limit"),
        pytest.param(0, 422, id="empty"),
    ],
)
def test_a_message_over_the_chat_limit_is_refused(
    install_conversation: Callable[..., ConvEnv], length: int, status: int
) -> None:
    """Catches a message limit that differs from the chat's own: one that is
    one too tight (the limit itself refused), one that is one too loose (one
    over let through), no limit at all, and an empty message let through.

    The chat's own limit is imported, so both sides move together; the
    accepted rows run the turn, so a limit that is too tight fails there.
    """
    env = install_conversation([ConvStep(update=_plain())])

    response = _post_conversation(_conv_body(message="m" * length))

    assert response.status_code == status
    assert len(env.turns.calls) == (1 if status == 200 else 0)
    if status == 200:
        assert len(env.turns.calls[0]["message"]) == length


def test_a_turn_lists_its_own_calls_and_results(
    install_conversation: Callable[..., ConvEnv],
) -> None:
    """Catches a `role_id` left as an id in the answer, a `result` that is
    rebuilt rather than the tool's own answer, calls of another turn listed
    here, and a role list read more than once for one turn.

    The turn makes two calls, so order matters; a second turn on the same chat
    makes none and must read `[]`, so calls piled up across turns fail.
    """
    answer_a = {"request_id": "r-1", "status": "executed", "decision": "auto"}
    answer_b = {"tickets": [{"id": "t-9"}], "count": 1}
    install_conversation(
        [
            ConvStep(
                update=_about("data-warehouse", "analyst"),
                calls=(("create_access_request", DW_ROLE_ID), ("create_ticket", DW_ROLE_ID)),
            ),
            ConvStep(update=_plain(TurnOutcome.CLARIFY)),
        ],
        answers={"create_access_request": answer_a, "create_ticket": answer_b},
        owned=[(SEAT_USER_ID, CHAT_ID)],
    )

    first = _post_conversation(_conv_body())
    second = _post_conversation(_conv_body(chat_id=str(CHAT_ID)))

    assert first.status_code == second.status_code == 200
    assert first.json()["tool_calls"] == [
        {
            "name": "create_access_request",
            "args": {"role": DW_ANALYST},
            "ok": True,
            "result": answer_a,
            "error_code": None,
        },
        {
            "name": "create_ticket",
            "args": {"role": DW_ANALYST},
            "ok": True,
            "result": answer_b,
            "error_code": None,
        },
    ]
    assert second.json()["tool_calls"] == []
    assert FakeDirectory.reads == 2


def test_a_failed_call_is_kept_with_its_error_code(
    install_conversation: Callable[..., ConvEnv],
) -> None:
    """Catches a recorder that drops a call which raised, that keeps it with
    `ok` true, or that loses the error code (`None` or `INTERNAL`); and one that
    swallows the error so the turn never sees it.

    The stub raises the not-found client error, whose code is `NOT_FOUND`;
    the scripted turn catches it as the loop does and still answers. The
    success after it must stay `ok` true.
    """
    install_conversation(
        [
            ConvStep(
                update=_about("wiki", "viewer"),
                calls=(("revoke_access", ROLE_ID), ("create_ticket", ROLE_ID)),
            )
        ],
        answers={"create_ticket": {"ticket_id": "t-1"}},
        failures={"revoke_access": RemoteNotFoundError("no such grant")},
    )

    response = _post_conversation(_conv_body())

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "reply about wiki.viewer"
    failed, fine = body["tool_calls"]
    assert failed["name"] == "revoke_access"
    assert failed["ok"] is False
    assert failed["error_code"] == ErrorCode.NOT_FOUND.value == "NOT_FOUND"
    assert isinstance(failed["result"], dict)
    assert fine["ok"] is True
    assert fine["error_code"] is None


def test_the_conversation_identity_comes_from_the_seat(
    install_conversation: Callable[..., ConvEnv],
) -> None:
    """Catches a turn that takes tenant or user from the body, mints a token
    with a session claim other than the chat's, or lets body text reach a tool.

    The seat's ids are values nothing in the body spells. A first turn and a
    second turn on the answered chat must each carry them, and one chat id; the
    tool's arguments hold only the role id.
    """
    env = install_conversation(
        [
            ConvStep(update=_plain(), calls=(("create_ticket", ROLE_ID),)),
            ConvStep(update=_plain(), calls=(("create_ticket", ROLE_ID),)),
        ],
        answers={"create_ticket": {"ok": True}},
    )

    first = _post_conversation(_conv_body(message="first"))
    chat_id = first.json()["chat_id"]
    env.chats.owned.add((SEAT_USER_ID, uuid.UUID(chat_id)))
    second = _post_conversation(_conv_body(message="second", chat_id=chat_id))

    assert first.status_code == second.status_code == 200
    assert env.harness.seat_lookups == [(TENANT_SLUG, PRINCIPAL_USER)] * 2
    assert {str(call["session_id"]) for call in env.turns.calls} == {chat_id}
    assert len(env.turns.calls) == 2
    for call in env.turns.calls:
        assert call["tenant_id"] == SEAT_TENANT_ID
        assert call["user_id"] == SEAT_USER_ID
        assert call["tenant_slug"] == TENANT_SLUG
        assert bound_tenant_id(cast(AsyncSession, call["deps"].db)) == SEAT_TENANT_ID
    assert len(env.harness.tokens) >= 2
    for token in env.harness.tokens:
        claims = verify_token(token, TEST_JWT_SIGNING_PHRASE, datetime.now(UTC))
        assert claims.sub == str(SEAT_USER_ID)
        assert claims.tenant_id == str(SEAT_TENANT_ID)
        assert claims.sid == chat_id
    for answer in (first.json(), second.json()):
        for call in answer["tool_calls"]:
            assert PRINCIPAL_USER not in json.dumps(call)
            assert "first" not in json.dumps(call["args"])


@pytest.mark.parametrize(
    ("tenant", "principal_user"),
    [
        pytest.param(TENANT_SLUG, "seed:production.employee.999", id="unknown-seat"),
        pytest.param("nobody", PRINCIPAL_USER, id="unknown-tenant"),
        pytest.param(TENANT_SLUG, "not-a-seat", id="malformed-seat"),
    ],
)
def test_an_unknown_seat_is_rejected_on_the_conversation_route(
    install_conversation: Callable[..., ConvEnv], tenant: str, principal_user: str
) -> None:
    """Catches a turn that runs anyway for a seat or tenant that is not
    seeded, or answers 404, 500 or 200 instead of 422 `VALIDATION`.

    The only seat the fake knows is the good one, and each row is unknown for
    its own reason, so a lookup on one half alone lets a row through. No
    world may be read for a seat that does not exist.
    """
    env = install_conversation([ConvStep(update=_plain())])

    response = _post_conversation(_conv_body(tenant=tenant, principal_user=principal_user))

    assert response.status_code == 422
    assert response.json() == {"code": ErrorCode.VALIDATION.value}
    assert env.turns.calls == []
    assert env.harness.tokens == []
    assert env.worlds.reads == []


def test_the_conversation_answer_carries_the_loop_prompt_version(
    install_conversation: Callable[..., ConvEnv],
) -> None:
    """Catches a conversation answer with no `X-Prompt-Versions` header, one
    that still names the old pipeline's four steps, or one that holds anything
    but the sha256 of the loop prompt's bytes (the digest the traces record),
    and an engine header that names anything but the loop.

    The scripted turn leaves the prompt version empty on the state, so the
    header cannot be copied from the state: it has to come from the file.
    """
    install_conversation([ConvStep(update=_plain()), ConvStep(update=_plain())])

    conversation = _post_conversation(_conv_body())

    digest = hashlib.sha256((PROMPTS_DIR / "agent_loop.md").read_bytes()).hexdigest()
    assert conversation.status_code == 200
    assert conversation.headers["X-Prompt-Versions"] == f"loop={digest}"
    assert conversation.headers["X-Chat-Engine"] == "loop"


def test_an_eval_conversation_sends_no_user_header(
    install_conversation: Callable[..., ConvEnv],
) -> None:
    """Catches an eval route that looks up or invents a gateway user id for the
    seat and hands it to the turn, so eval traffic would show up as a real
    person's cost on the gateway.

    The turn is a recorder, so what the route passed is read straight off its
    keyword arguments: none may name a gateway user.
    """
    env = install_conversation([ConvStep(update=_plain())])

    response = _post_conversation(_conv_body())

    assert response.status_code == 200
    assert len(env.turns.calls) == 1
    assert [call.get("gateway_user") for call in env.turns.calls] == [None]
