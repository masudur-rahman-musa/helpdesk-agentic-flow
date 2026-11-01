"""`POST /admin/test/reset` and `POST /v1/eval/conversation`, the routes the
companion console's eval runner calls.

Written before the implementation exists. Surface under test, and the seams
the cases below use. The implementer must provide exactly these names:

``helpdesk.shared.core.config.Settings.eval_routes_enabled`` (env
``EVAL_ROUTES_ENABLED``, default false). Both routes answer only when it is
true, and are a typed 404 ``{"code": "NOT_FOUND"}`` otherwise. Both join the
open paths only when it is true; when it is false they still need a token.

``helpdesk.agent_api.services.eval_turn`` holds the turn. Four names in it
are looked up at call time, so a test can swap them and no database, tool
server or model is needed:

``resolve_seat(factory, *, tenant_slug, principal_user)``
    async; the seat's user or ``None`` for an unknown tenant or seat. The
    result is read only through the attributes ``tenant_id``, ``tenant_slug``,
    ``user_id``, ``department_id`` and ``principals``.
``run_loop_turn(**kwargs)``
    the loop's own ``run_loop_turn`` (same keyword-only signature), called once
    per message, in order, with ``deps.db`` bound to the seat's tenant, a
    ``deps.tools`` that records the calls made, and ``deps.model`` the app's
    model client (watched, so the headers of every call are kept).
``build_tool_caller(settings, bearer_token)``
    the same seam ``routes/chat.py`` has: called with the token minted for
    the seat.
``DbLoopStore(db, *, session_id, user_id)``
    read once per conversation for ``role_ids()``, the tenant's roles, so a
    call's ``role_id`` reads back as ``<system slug>.<role slug>``.

The answer is exactly the console's envelope, and every step's prompt version
rides the ``X-Prompt-Versions`` header.

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
# `POST /v1/eval/conversation` and `eval_turn.run_conversation`. The same
# seams are swapped (`resolve_seat`, `run_loop_turn`, `build_tool_caller`,
# `DbLoopStore`), so no database, tool server or model is needed.

CONVERSATION_PATH = "/v1/eval/conversation"
DW_ANALYST = "data-warehouse.analyst"
DW_ROLE_ID = uuid.UUID("a6666666-6666-5666-8666-666666666666")


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


@pytest.fixture
def install_conversation(
    install: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> Callable[..., tuple[Harness, ConvTurns]]:
    """`install`, then the conversation's own scripted turns and stub caller."""

    def build(
        steps: Sequence[ConvStep],
        *,
        answers: Mapping[str, Mapping[str, Any]] | None = None,
        failures: Mapping[str, ToolError] | None = None,
    ) -> tuple[Harness, ConvTurns]:
        harness = install([])
        turns = ConvTurns(steps)
        tokens = harness.tokens
        stub = StubCaller(answers or {}, failures or {})

        def build_caller(_settings: object, bearer_token: str) -> StubCaller:
            tokens.append(bearer_token)
            return stub

        monkeypatch.setattr(eval_turn, "run_loop_turn", turns)
        monkeypatch.setattr(eval_turn, "build_tool_caller", build_caller)
        monkeypatch.setattr(eval_turn, "DbLoopStore", FakeDirectory)
        monkeypatch.setattr(FakeDirectory, "reads", 0)
        return harness, turns

    return build


def _conv_body(
    *,
    turns: Sequence[str] = ("hello",),
    case_id: str = "conv-001",
    tenant: str = TENANT_SLUG,
    principal_user: str = PRINCIPAL_USER,
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "tenant": tenant,
        "principal_user": principal_user,
        "turns": list(turns),
    }


def _post_conversation(body: Mapping[str, Any], *, token: str | None = None) -> httpx.Response:
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}

    async def scenario(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post(CONVERSATION_PATH, json=dict(body), headers=headers)

    return _drive(scenario)


def test_the_conversation_route_is_404_when_switched_off(
    install_conversation: Callable[..., tuple[Harness, ConvTurns]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a conversation route that answers in production: one that ignores
    `EVAL_ROUTES_ENABLED`, or is not mounted (FastAPI's `{"detail"}` instead of
    the typed `NOT_FOUND`).

    The fakes would make the switched-on route answer 200, so the 404 can only
    come from the switch.
    """
    harness, turns = install_conversation([ConvStep(update=_plain())])
    monkeypatch.setenv("EVAL_ROUTES_ENABLED", "false")

    response = _post_conversation(_conv_body(), token=_login_token())

    assert (response.status_code, response.json()) == (404, {"code": ErrorCode.NOT_FOUND.value})
    assert turns.calls == []
    assert harness.seat_lookups == []


def test_a_conversation_answers_one_record_per_turn_in_order(
    install_conversation: Callable[..., tuple[Harness, ConvTurns]],
) -> None:
    """Catches an answer that keeps only the last turn's reply (as `/v1/eval/turn`
    does), numbers turns from 0, reorders them, or starts a new chat per turn.

    Each scripted reply differs, so a record carrying another turn's reply fails.
    """
    _, turns = install_conversation(
        [
            ConvStep(update={"outcome": TurnOutcome.CLARIFY, "reply": "first reply"}),
            ConvStep(update={"outcome": TurnOutcome.CLARIFY, "reply": "second reply"}),
            ConvStep(update={"outcome": TurnOutcome.DONE, "reply": "third reply"}),
        ]
    )

    response = _post_conversation(_conv_body(turns=("one", "two", "three"), case_id="conv-007"))

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"case_id", "turns"}
    assert body["case_id"] == "conv-007"
    assert [(t["turn"], t["reply"]) for t in body["turns"]] == [
        (1, "first reply"),
        (2, "second reply"),
        (3, "third reply"),
    ]
    assert [call["message"] for call in turns.calls] == ["one", "two", "three"]
    assert len({call["session_id"] for call in turns.calls}) == 1


def test_each_turn_lists_only_its_own_calls_and_results(
    install_conversation: Callable[..., tuple[Harness, ConvTurns]],
) -> None:
    """Catches calls piled up across the whole conversation (as the single-turn
    envelope does), so turn 3 would list turn 1's call too; a `role_id` left as
    an id; and a `result` that is rebuilt rather than the tool's own answer.

    Turn 2 makes no call and must read `[]`. The role names come from the
    tenant's role list, read once for the whole conversation, so turn 3 reads
    the same name back without a second read.
    """
    answer_a = {"request_id": "r-1", "status": "executed", "decision": "auto"}
    answer_b = {"tickets": [{"id": "t-9"}], "count": 1}
    _, _ = install_conversation(
        [
            ConvStep(
                update=_about("data-warehouse", "analyst"),
                calls=(("create_access_request", DW_ROLE_ID),),
            ),
            ConvStep(update=_plain(TurnOutcome.CLARIFY)),
            ConvStep(update=_plain(), calls=(("create_ticket", DW_ROLE_ID),)),
        ],
        answers={"create_access_request": answer_a, "create_ticket": answer_b},
    )

    response = _post_conversation(_conv_body(turns=("a", "b", "c")))

    assert response.status_code == 200
    records = response.json()["turns"]
    assert records[0]["tool_calls"] == [
        {
            "name": "create_access_request",
            "args": {"role": DW_ANALYST},
            "ok": True,
            "result": answer_a,
            "error_code": None,
        }
    ]
    assert records[1]["tool_calls"] == []
    assert records[2]["tool_calls"] == [
        {
            "name": "create_ticket",
            "args": {"role": DW_ANALYST},
            "ok": True,
            "result": answer_b,
            "error_code": None,
        }
    ]
    assert FakeDirectory.reads == 1


def test_a_failed_call_is_kept_with_its_error_code(
    install_conversation: Callable[..., tuple[Harness, ConvTurns]],
) -> None:
    """Catches a recorder that drops a call which raised, that keeps it with
    `ok` true, or that loses the error code (`None` or `INTERNAL`); and one that
    swallows the error so the turn never sees it.

    The stub raises the not-found client error, whose code is `NOT_FOUND`;
    the scripted turn catches it as the loop does and still answers. The
    success after it must stay `ok` true.
    """
    _, _ = install_conversation(
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
    record = response.json()["turns"][0]
    assert record["reply"] == "reply about wiki.viewer"
    failed, fine = record["tool_calls"]
    assert failed["name"] == "revoke_access"
    assert failed["ok"] is False
    assert failed["error_code"] == ErrorCode.NOT_FOUND.value == "NOT_FOUND"
    assert isinstance(failed["result"], dict)
    assert fine["ok"] is True
    assert fine["error_code"] is None


def test_the_conversation_identity_comes_from_the_seat(
    install_conversation: Callable[..., tuple[Harness, ConvTurns]],
) -> None:
    """Catches a conversation that takes tenant or user from the body, mints a
    token per turn with a fresh `sid`, or lets body text reach a tool.

    The seat's ids are values nothing in the body spells. Every turn's token and
    loop call must carry them and one chat id; the tool's arguments hold
    only the role id.
    """
    harness, turns = install_conversation(
        [
            ConvStep(update=_plain(), calls=(("create_ticket", ROLE_ID),)),
            ConvStep(update=_plain(), calls=(("create_ticket", ROLE_ID),)),
        ],
        answers={"create_ticket": {"ok": True}},
    )

    response = _post_conversation(_conv_body(turns=("first", "second")))

    assert response.status_code == 200
    assert harness.seat_lookups == [(TENANT_SLUG, PRINCIPAL_USER)]
    chat_ids = {call["session_id"] for call in turns.calls}
    assert len(chat_ids) == 1
    chat_id = next(iter(chat_ids))
    for call in turns.calls:
        assert call["tenant_id"] == SEAT_TENANT_ID
        assert call["user_id"] == SEAT_USER_ID
        assert call["tenant_slug"] == TENANT_SLUG
        assert bound_tenant_id(cast(AsyncSession, call["deps"].db)) == SEAT_TENANT_ID
    assert len(harness.tokens) >= 1
    for token in harness.tokens:
        claims = verify_token(token, TEST_JWT_SIGNING_PHRASE, datetime.now(UTC))
        assert claims.sub == str(SEAT_USER_ID)
        assert claims.tenant_id == str(SEAT_TENANT_ID)
        assert claims.sid == str(chat_id)
    for record in response.json()["turns"]:
        for call in record["tool_calls"]:
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
    install_conversation: Callable[..., tuple[Harness, ConvTurns]],
    tenant: str,
    principal_user: str,
) -> None:
    """Catches a conversation that runs anyway for a seat or tenant that is not
    seeded, or answers 404, 500 or 200 instead of 422 `VALIDATION`.

    The only seat the fake knows is the good one, and each row is unknown for
    its own reason, so a lookup on one half alone lets a row through.
    """
    harness, turns = install_conversation([ConvStep(update=_plain())])

    response = _post_conversation(_conv_body(tenant=tenant, principal_user=principal_user))

    assert response.status_code == 422
    assert response.json() == {"code": ErrorCode.VALIDATION.value}
    assert turns.calls == []
    assert harness.tokens == []


@pytest.mark.parametrize(
    ("case_id", "turn_count", "status"),
    [
        pytest.param("conv-001", 1, 200, id="one-turn"),
        pytest.param("conv-001", 10, 200, id="ten-turns"),
        pytest.param("conv-000", 2, 200, id="conv-000"),
        pytest.param("conv-001", 0, 422, id="zero-turns"),
        pytest.param("conv-001", 11, 422, id="eleven-turns"),
        pytest.param("conv-12", 2, 422, id="two-digit-id"),
        pytest.param("conv-1000", 2, 422, id="four-digit-id"),
        pytest.param("conv-001\n", 2, 422, id="id-trailing-newline"),
        pytest.param("conv-001\r\n", 2, 422, id="id-trailing-crlf"),
        pytest.param("conv-001 ", 2, 422, id="id-trailing-space"),
    ],
)
def test_the_conversation_body_limits(
    install_conversation: Callable[..., tuple[Harness, ConvTurns]],
    case_id: str,
    turn_count: int,
    status: int,
) -> None:
    """Catches limits that are off by one (0 or 11 turns let through, 10 refused),
    an id pattern that is looser than three digits or unanchored (a trailing
    newline, CRLF or space let through), and a lower bound that refuses `conv-000`.

    The accepted rows run every turn, so a bound that is too tight also fails.
    """
    _, turns = install_conversation([ConvStep(update=_plain()) for _ in range(10)])
    messages = [f"message {n}" for n in range(turn_count)]

    response = _post_conversation(_conv_body(turns=messages, case_id=case_id))

    assert response.status_code == status
    assert len(turns.calls) == (turn_count if status == 200 else 0)


def test_the_conversation_answer_carries_the_loop_prompt_version(
    install_conversation: Callable[..., tuple[Harness, ConvTurns]],
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
    install_conversation: Callable[..., tuple[Harness, ConvTurns]],
) -> None:
    """Catches an eval route that looks up or invents a gateway user id for the
    seat and hands it to the turn, so eval traffic would show up as a real
    person's cost on the gateway.

    The turn is a recorder, so what the route passed is read straight off its
    keyword arguments: none may name a gateway user.
    """
    _, turns = install_conversation([ConvStep(update=_plain()), ConvStep(update=_plain())])

    response = _post_conversation(_conv_body(turns=("one", "two")))

    assert response.status_code == 200
    assert len(turns.calls) == 2
    assert [call.get("gateway_user") for call in turns.calls] == [None, None]
