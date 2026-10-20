"""`POST /admin/test/reset` and `POST /v1/eval/turn`, the routes the companion
console's eval runner calls.

Written before the implementation exists. Surface under test, and the seams
the cases below use. The implementer must provide exactly these names:

``helpdesk.shared.core.config.Settings.eval_routes_enabled`` (env
``EVAL_ROUTES_ENABLED``, default false). Both routes answer only when it is
true, and are a typed 404 ``{"code": "NOT_FOUND"}`` otherwise. Both join the
open paths only when it is true; when it is false they still need a token.

``helpdesk.agent_api.services.eval_turn`` (new module) holds the turn. Three
names in it are looked up at call time, so a test can swap them and no
database, tool server or graph is needed:

``resolve_seat(factory, *, tenant_slug, principal_user)``
    async; the seat's user or ``None`` for an unknown tenant or seat. The
    result is read only through the attributes ``tenant_id``, ``tenant_slug``,
    ``user_id``, ``department_id`` and ``principals``.
``run_turn(**kwargs)``
    the graph's own ``run_turn`` (same keyword-only signature), called once
    per message, in order, with ``deps.db`` bound to the seat's tenant, a
    ``deps.tools`` that records the calls made, and ``deps.model`` the app's
    model client (watched, so the headers of every call are kept).
``build_tool_caller(settings, bearer_token)``
    the same seam ``routes/chat.py`` has: called with the token minted for
    the seat.

The answer is exactly the console's envelope, and every step's prompt version
rides the ``X-Prompt-Versions`` header.

Cost: no backing service, no socket. The database is a recording session
factory, the graph a scripted function, the tool server a recording stub and
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
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.agent_api.agent.prompts import PROMPTS_DIR
from helpdesk.agent_api.agent.state import (
    Citation,
    Intent,
    IntentEntities,
    IntentExtraction,
    ResolvedEntities,
    ToolResult,
    TurnOutcome,
    TurnState,
)
from helpdesk.agent_api.api.router import app
from helpdesk.agent_api.services import eval_turn
from helpdesk.agent_api.services.model_gateway.client import ModelAlias, ModelClient
from helpdesk.shared.auth.jwt import issue_token, verify_token
from helpdesk.shared.core.db import bound_tenant_id
from helpdesk.shared.core.errors import ErrorCode

RESET_PATH = "/admin/test/reset"
TURN_PATH = "/v1/eval/turn"

# Obviously fake, tests only: none of these is a credential.
JWT_SECRET = "eval-routes-test-jwt-secret-0123456789abcdef"
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

ENVELOPE_KEYS = frozenset(
    {
        "intent",
        "tool_calls",
        "decision",
        "risk_class",
        "refused",
        "clarified",
        "citations",
        "reply",
        "latency_ms",
        "cost_usd",
        "provider",
        "region",
    }
)
"""The twelve names the console's envelope allows. It rejects any other."""

GOLDEN = Path(__file__).resolve().parents[2] / "evals" / "golden" / "helpdesk-v1.jsonl"


def _golden_case(case_id: str) -> dict[str, Any]:
    for line in GOLDEN.read_text(encoding="utf-8").splitlines():
        case: dict[str, Any] = json.loads(line)
        if case["id"] == case_id:
            return case
    raise AssertionError(f"{case_id} is not in the golden set")


# --- the fakes ----------------------------------------------------------------


@dataclass(frozen=True)
class FakeSeat:
    """What `resolve_seat` hands back: the five attributes the turn reads."""

    tenant_id: uuid.UUID = SEAT_TENANT_ID
    tenant_slug: str = TENANT_SLUG
    user_id: uuid.UUID = SEAT_USER_ID
    department_id: uuid.UUID = SEAT_DEPARTMENT_ID
    principals: tuple[str, ...] = ("employee",)


class RecordingSession:
    """Stands in for an `AsyncSession`: it keeps the tenant bound to it."""

    def __init__(self) -> None:
        self.info: dict[str, Any] = {}
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


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
    """A `run_turn` that plays one `Step` per message and records every call."""

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


def _extraction() -> IntentExtraction:
    return IntentExtraction(
        intent=Intent.REQUEST_ACCESS,
        entities=IntentEntities(
            system=None,
            role=None,
            target_user=None,
            department=None,
            expiry_days=None,
            justification=None,
        ),
        confidence=0.9,
        clarifying_question=None,
        second_request=None,
    )


def _plain(outcome: TurnOutcome = TurnOutcome.DONE, **more: Any) -> Mapping[str, Any]:
    return {"outcome": outcome, "reply": f"reply after {outcome.value}", **more}


FILED_STATE: Mapping[str, Any] = {
    "outcome": TurnOutcome.DONE,
    "reply": "I have filed your request for the wiki viewer role.",
    "extraction": _extraction(),
    "resolved": ResolvedEntities(
        system_slug="wiki", role_slug="viewer", role_id=ROLE_ID, system_name="Wiki"
    ),
    "citations": (
        Citation(
            document="access-control-policy",
            title="Access control policy",
            version="v1",
            heading_path=("1",),
            chunk_id=uuid.UUID("a5555555-5555-5555-8555-555555555555"),
            text="Fake passage.",
            score=0.9,
        ),
    ),
    "tool_results": (
        ToolResult(
            tool="create_access_request",
            ok=True,
            payload={"decision": "auto", "risk_class": "low", "status": "executed"},
        ),
    ),
}
"""The state aut-001's last message ends in: a low-risk request filed on its own."""


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
    monkeypatch.setenv("JWT_SECRET", JWT_SECRET)
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
        monkeypatch.setattr(eval_turn, "run_turn", turns)
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
        monkeypatch.setattr(app.state, "checkpointer", None, raising=False)
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
        JWT_SECRET,
        datetime.now(UTC),
    )


def _body(
    *,
    messages: Sequence[str] = ("hello",),
    tenant: str = TENANT_SLUG,
    principal_user: str = PRINCIPAL_USER,
) -> dict[str, Any]:
    return {
        "case_id": "aut-001",
        "tenant": tenant,
        "principal_user": principal_user,
        "messages": [{"role": "user", "content": text} for text in messages],
    }


def _post_turn(body: Mapping[str, Any], *, token: str | None = None) -> httpx.Response:
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}

    async def scenario(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post(TURN_PATH, json=dict(body), headers=headers)

    return _drive(scenario)


def _post_reset(*, token: str | None = None) -> httpx.Response:
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}

    async def scenario(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post(RESET_PATH, headers=headers)

    return _drive(scenario)


# --- the switch ---------------------------------------------------------------


def test_eval_routes_are_404_when_switched_off(
    install: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches routes that answer in production: an implementation that ignores
    `EVAL_ROUTES_ENABLED` (always on, or on whenever a token is present) would
    reset or run a turn here instead of answering 404, and a route that is
    simply not mounted would answer FastAPI's own `{"detail": ...}`, not the
    typed `NOT_FOUND`. It also catches an open-path list that skips the token
    check while the setting is false: both routes must still turn away a
    request with no bearer.

    The fakes are installed so the ON path would succeed (200 and 204): the 404
    can only come from the switch.
    """
    harness = install([Step(update=_plain())])
    monkeypatch.setenv("EVAL_ROUTES_ENABLED", "false")
    token = _login_token()

    reset = _post_reset(token=token)
    turn = _post_turn(_body(), token=token)

    assert (reset.status_code, reset.json()) == (404, {"code": ErrorCode.NOT_FOUND.value})
    assert (turn.status_code, turn.json()) == (404, {"code": ErrorCode.NOT_FOUND.value})
    assert harness.turns.calls == []
    assert harness.seat_lookups == []

    for response in (_post_reset(), _post_turn(_body())):
        assert response.status_code == 401
        assert response.json() == {"code": ErrorCode.FORBIDDEN.value}


def test_reset_answers_204_when_switched_on(install: Callable[..., Harness]) -> None:
    """Catches a reset that wants a body or a token (the console sends neither),
    or that answers 200 with a body: the console accepts any 2xx, but the plan
    fixes 204, an empty answer, because the route clears nothing.

    No bearer is sent, so an implementation that left the path off the open
    list would answer 401 here.
    """
    install([])

    response = _post_reset()

    assert response.status_code == 204
    assert response.content == b""


# --- the envelope -------------------------------------------------------------


def test_an_eval_turn_answers_exactly_the_console_envelope(
    install: Callable[..., Harness],
) -> None:
    """Catches an answer with a missing or an extra field (the console rejects
    extras), a role left as an id instead of `<system slug>.<role slug>`, and a
    decision or risk class that is not read off the turn's tool answer.

    aut-001's four real messages are replayed. The scripted last turn files the
    request and calls `create_access_request` with the role's id only, so the
    only way to write `wiki.viewer` is to map that id through the resolved
    entities. No bearer is sent: the console sends none.
    """
    case = _golden_case("aut-001")
    texts = [message["content"] for message in case["messages"]]
    assert len(texts) == 4
    steps = [Step(update=_plain(TurnOutcome.CLARIFY)) for _ in range(3)]
    steps.append(Step(update=FILED_STATE, tool_call="create_access_request"))
    harness = install(steps)

    response = _post_turn(_body(messages=texts, principal_user=case["principal_user"]))

    assert response.status_code == 200
    body = response.json()
    assert set(body) == ENVELOPE_KEYS
    assert body["intent"] == "request_access"
    assert body["tool_calls"] == [
        {"name": "create_access_request", "args": {"role": "wiki.viewer"}}
    ]
    assert body["decision"] == "auto"
    assert body["risk_class"] == "low"
    assert body["refused"] is False
    assert body["clarified"] is False
    assert body["citations"] == ["access-control-policy"]
    assert body["reply"] == "I have filed your request for the wiki viewer role."
    assert isinstance(body["latency_ms"], int | float)
    assert not isinstance(body["latency_ms"], bool)
    assert body["latency_ms"] >= 0
    assert isinstance(body["cost_usd"], str)
    assert isinstance(body["provider"], str)
    assert isinstance(body["region"], str)
    assert [call["message"] for call in harness.turns.calls] == texts


def test_the_answer_carries_every_prompt_version(install: Callable[..., Harness]) -> None:
    """Catches a header that is missing, names fewer than the four steps, is in
    another order, or holds anything but the sha256 of each prompt file's bytes
    (the digest the traces record), such as a digest of the file name.

    The scripted turn leaves `prompt_version` and `planning_prompt_version`
    empty on the state, so the header cannot be copied from the state: it has
    to come from the files.
    """
    install([Step(update=_plain())])

    response = _post_turn(_body())

    def digest(name: str) -> str:
        return hashlib.sha256((PROMPTS_DIR / f"{name}.md").read_bytes()).hexdigest()

    expected = ",".join(
        (
            f"intent={digest('intent_extraction')}",
            f"plan={digest('tool_planning')}",
            f"compose={digest('reply_composition')}",
            f"system={digest('system')}",
        )
    )
    assert response.status_code == 200
    assert response.headers["X-Prompt-Versions"] == expected


def test_region_provider_and_cost_come_from_the_gateway_headers(
    install: Callable[..., Harness],
) -> None:
    """Catches a cost that is the last call's alone, or the first's, instead of
    the sum; a region or provider taken from the first call instead of the last;
    and values read from anywhere but `X-Gateway-Cost-USD`,
    `X-Gateway-Region` and `X-Gateway-Provider`.

    One gateway call in each of two turns, so an implementation that resets its
    totals for each message keeps only 0.0042. The sum is a string with four
    places, exactly "0.0142", so a float sum (0.0142 or 0.014199999) fails.
    """
    install(
        [Step(update=_plain(), model_calls=1), Step(update=_plain(), model_calls=1)],
        gateway_headers=[
            {
                "X-Gateway-Provider": "fake-provider-a",
                "X-Gateway-Region": "us",
                "X-Gateway-Cost-USD": "0.0100",
            },
            {
                "X-Gateway-Provider": "fake-provider-b",
                "X-Gateway-Region": "eu",
                "X-Gateway-Cost-USD": "0.0042",
            },
        ],
    )

    response = _post_turn(_body(messages=("one", "two")))

    body = response.json()
    assert response.status_code == 200
    assert body["cost_usd"] == "0.0142"
    assert body["region"] == "eu"
    assert body["provider"] == "fake-provider-b"


# --- identity -----------------------------------------------------------------


def test_identity_comes_from_the_resolved_seat(install: Callable[..., Harness]) -> None:
    """Catches a turn that takes tenant or user from the body: the seat's ids are
    random values that nothing in the body spells, so only the lookup can
    produce them. The token handed to the tool server, the ids given to the
    graph and the tenant bound to the database session must all be the seat's,
    and the lookup must be given the body's tenant slug and seat name.

    Also catches a token minted with a different chat id than the graph runs
    under: the tool server stamps `sid` on what it files.
    """
    messages = ("first", "second")
    harness = install([Step(update=_plain()), Step(update=_plain())])

    response = _post_turn(_body(messages=messages))

    assert response.status_code == 200
    assert harness.seat_lookups == [(TENANT_SLUG, PRINCIPAL_USER)]
    assert len(harness.turns.calls) == 2
    chat_ids = {call["session_id"] for call in harness.turns.calls}
    assert len(chat_ids) == 1, "every message of one case belongs to one chat"
    chat_id = next(iter(chat_ids))
    for call in harness.turns.calls:
        assert call["tenant_id"] == SEAT_TENANT_ID
        assert call["user_id"] == SEAT_USER_ID
        assert call["tenant_slug"] == TENANT_SLUG
        assert bound_tenant_id(cast(AsyncSession, call["deps"].db)) == SEAT_TENANT_ID
    assert harness.tokens, "the tool server is never reached without a token"
    for token in harness.tokens:
        claims = verify_token(token, JWT_SECRET, datetime.now(UTC))
        assert claims.sub == str(SEAT_USER_ID)
        assert claims.tenant_id == str(SEAT_TENANT_ID)
        assert claims.department_id == str(SEAT_DEPARTMENT_ID)
        assert claims.principals == ["employee"]
        assert claims.sid == str(chat_id)


@pytest.mark.parametrize(
    ("tenant", "principal_user"),
    [
        pytest.param(TENANT_SLUG, "seed:production.employee.999", id="unknown-seat"),
        pytest.param("nobody", PRINCIPAL_USER, id="unknown-tenant"),
        pytest.param(TENANT_SLUG, "not-a-seat", id="malformed-seat"),
    ],
)
def test_an_unknown_seat_or_tenant_is_rejected(
    install: Callable[..., Harness], tenant: str, principal_user: str
) -> None:
    """Catches a turn that runs anyway (as another user, or as nobody) when the
    seat or the tenant is not in the seeded data, and one that answers 404, 500
    or 200 instead of the plan's 422 `VALIDATION`.

    Each row is unknown for its own reason; the only seat the fake knows is the
    good one, so a lookup that matched on the seat alone, or the tenant alone,
    would let one of the first two rows through.
    """
    harness = install([Step(update=_plain())])

    response = _post_turn(_body(tenant=tenant, principal_user=principal_user))

    assert response.status_code == 422
    assert response.json() == {"code": ErrorCode.VALIDATION.value}
    assert harness.turns.calls == []
    assert harness.tokens == []


# --- the flags ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcomes", "refused", "clarified"),
    [
        pytest.param([TurnOutcome.REFUSE], True, False, id="refuse"),
        pytest.param([TurnOutcome.CLARIFY], False, True, id="clarify"),
        pytest.param([TurnOutcome.DISAMBIGUATE], False, True, id="disambiguate"),
        pytest.param([TurnOutcome.CONFIRM], False, False, id="confirm-is-neither"),
        pytest.param([TurnOutcome.DONE], False, False, id="answered"),
        pytest.param([TurnOutcome.REFUSE, TurnOutcome.DONE], False, False, id="only-the-last"),
        pytest.param([TurnOutcome.CLARIFY, TurnOutcome.DONE], False, False, id="clarify-then-done"),
    ],
)
def test_a_refusal_and_a_question_set_the_flags(
    install: Callable[..., Harness],
    outcomes: Sequence[TurnOutcome],
    refused: bool,
    clarified: bool,
) -> None:
    """Catches flags that read the wrong outcome: `refused` true for any turn
    that refused (not only the last), `clarified` left out for a disambiguation,
    or set for a confirmation question, which the plan does not count.

    The two earlier-turn rows fail an implementation that ORs the flags over
    the whole replay instead of reading the last outcome.
    """
    install([Step(update=_plain(outcome)) for outcome in outcomes])

    response = _post_turn(_body(messages=[f"message {n}" for n in range(len(outcomes))]))

    body = response.json()
    assert response.status_code == 200
    assert body["refused"] is refused
    assert body["clarified"] is clarified
