"""The agent loop as the only chat engine, over the real chat row.

Expected implementation surface:

``helpdesk.shared.core.config.Settings``
    has no ``chat_engine`` field; a ``CHAT_ENGINE`` in the environment is ignored.
``helpdesk.agent_api.services.chat``
    ``chat_turn`` calls ``run_loop_turn``; the module has no ``run_turn``.
``helpdesk.agent_api.services.eval_turn``
    ``run_conversation`` calls the module attribute ``run_loop_turn``; the
    module has no ``run_turn``. ``X-Chat-Engine`` always says ``loop`` and
    ``X-Prompt-Versions`` is ``loop=<sha256 of agent_loop.md>``.

Cost. Postgres is the shared `agent_db` session fixture: only Postgres shows
the open question saved on the chat row by the loop and read back from it by
the loop on the next message. Each case seeds its own tenant and uses a new
chat id, so nothing is shared. The gateway is an `httpx.MockTransport`, the
tool server a recording fake and Redis a counter stub: no socket is opened.
This file stands alone: it imports nothing from the pipeline's test files.
"""

import asyncio
import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from fastapi import Request
from pydantic import BaseModel

from helpdesk.agent_api.agent.confirmation import parse_open_question
from helpdesk.agent_api.agent.prompts import PROMPTS_DIR
from helpdesk.agent_api.agent.state import TurnOutcome
from helpdesk.agent_api.api.router import app
from helpdesk.agent_api.schemas.chat import ChatResponse
from helpdesk.agent_api.services import chat as chat_service
from helpdesk.agent_api.services import eval_turn
from helpdesk.agent_api.services.mcp_client import ToolCaller
from helpdesk.agent_api.services.model_gateway.client import ModelClient
from helpdesk.shared.auth.jwt import TokenClaims
from helpdesk.shared.core.config import Settings
from helpdesk.shared.core.db import tenant_session
from helpdesk.shared.crud.messages import list_recent_messages
from helpdesk.shared.crud.sessions import get_open_question
from helpdesk.shared.models import Department, Role, System, Tenant, User
from tests.agent_api.conftest import AgentDb
from tests.agent_api.test_agent_loop import answer_body, wire_call
from tests.agent_api.test_eval_routes import (
    PRINCIPAL_USER,
    TENANT_SLUG,
    ConvStep,
    ConvTurns,
    FakeSeat,
    RecordingFactory,
    StubCaller,
    _conv_body,
    _drive,
    _plain,
)

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
GATEWAY_URL = "http://gateway.invalid:8080"
# Obviously fake, tests only: these are not credentials.
JWT_PHRASE = "loop-switch-test-jwt-secret-0123456789abcdef"
EVAL_JWT_PHRASE = "loop-switch-eval-jwt-secret-0123456789abcdef"

ASK = "I need viewer access to the internal wiki"
YES = "Yes, that is right."
LOOP_SAYS = "Here is what I can tell you."
CREATE = "create_access_request"
REQUEST_ID = uuid.UUID("77777777-7777-5777-8777-777777777777")
"""What the fake `create_access_request` answers, obviously fake."""
FILED_SAYS = "Your request REQ-0001 was filed."


# --- the seeded tenant ----------------------------------------------------------


@dataclass(frozen=True)
class Env:
    db: AgentDb
    tenant_id: uuid.UUID
    tenant_slug: str
    user_id: uuid.UUID
    viewer_id: uuid.UUID


async def _seed(db: AgentDb) -> Env:
    slug = f"switch-{uuid.uuid4().hex[:12]}"
    async with db.factory() as session:
        tenant = Tenant(slug=slug, name="Fake Switch Tenant", settings={})
        session.add(tenant)
        await session.commit()
        tenant_id = tenant.id

    async with tenant_session(db.factory, tenant_id) as session:
        department = Department(tenant_id=tenant_id, name="fake-dept", manager_user_id=None)
        session.add(department)
        await session.flush()
        person = User(
            tenant_id=tenant_id,
            department_id=department.id,
            email=f"fake.person@{slug}.example.test",
            display_name="Fake Person",
            password_hash="argon2id$obviously-fake-test-hash",
            principals=["employee"],
            status="active",
            start_date=date(2026, 1, 5),
            end_date=None,
        )
        session.add(person)
        await session.flush()
        system = System(
            tenant_id=tenant_id,
            slug="internal-wiki",
            name="Internal Wiki",
            sensitivity="low",
            owner_user_id=person.id,
        )
        session.add(system)
        await session.flush()
        role = Role(
            tenant_id=tenant_id,
            system_id=system.id,
            slug="wiki-viewer",
            name="Viewer",
            privilege="read",
            default_expiry_days=90,
        )
        session.add(role)
        await session.flush()
        return Env(
            db=db,
            tenant_id=tenant_id,
            tenant_slug=slug,
            user_id=person.id,
            viewer_id=role.id,
        )


@pytest.fixture
def seeded(agent_db: AgentDb) -> Env:
    """A new tenant with one person, one system and one role, for one case."""
    return asyncio.run(_seed(agent_db))


@pytest.fixture
def chat_settings_env(seeded: Env, monkeypatch: pytest.MonkeyPatch) -> Env:
    """Fake settings for the chat service. Nothing here is dialled."""
    monkeypatch.setenv("DATABASE_URL", seeded.db.url)
    monkeypatch.setenv("REDIS_URL", "redis://redis.invalid:6379/0")
    monkeypatch.setenv("QDRANT_URL", "http://qdrant.invalid:6333")
    monkeypatch.setenv("JWT_SECRET", JWT_PHRASE)
    monkeypatch.setenv("GATEWAY_URL", GATEWAY_URL)
    monkeypatch.setenv("MCP_URL", "http://mcp.invalid:8081/mcp")
    return seeded


class CounterRedis:
    """Stands in for Redis: the rate limit only counts and sets an expiry."""

    async def incr(self, key: str) -> int:
        return 1

    async def expire(self, key: str, seconds: int) -> bool:
        return True


class FakeTools:
    """A `ToolCaller` answering the filing call and recording every call."""

    def __init__(self, lookups: Mapping[str, Mapping[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.lookups = dict(lookups or {})
        """Read tools answered as scripted, so a change can carry looked-up ids."""

    async def __aenter__(self) -> ToolCaller:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def call_tool(self, name: str, arguments: BaseModel) -> Mapping[str, Any]:
        self.calls.append((name, arguments.model_dump(mode="json")))
        if name in self.lookups:
            return self.lookups[name]
        if name != CREATE:
            raise AssertionError(f"the turn called an unscripted tool: {name}")
        return {
            "request_id": str(REQUEST_ID),
            "request_number": "REQ-0001",
            "status": "pending",
            "decision": "approval",
            "required_approvers": [],
        }

    @property
    def names(self) -> list[str]:
        return [name for name, _arguments in self.calls]

    def arguments_of(self, tool: str) -> list[dict[str, Any]]:
        return [arguments for name, arguments in self.calls if name == tool]


class Gateway:
    """Whole gateway bodies in order; a request past the last one fails the run."""

    def __init__(self, *bodies: Mapping[str, Any]) -> None:
        self._bodies = list(bodies)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        index = len(self.requests) - 1
        if index >= len(self._bodies):
            raise AssertionError(
                f"the turn made {len(self.requests)} model requests; "
                f"only {len(self._bodies)} were staged"
            )
        return httpx.Response(200, json=dict(self._bodies[index]))


def say(
    env: Env,
    gateway: Gateway,
    tools: FakeTools,
    message: str,
    *,
    chat_id: str | None = None,
) -> ChatResponse:
    """One message through `chat_turn`, as the route calls it."""

    async def scenario() -> ChatResponse:
        model = ModelClient(
            base_url=GATEWAY_URL,
            api_keys={env.tenant_slug: f"fake-key-{env.tenant_slug}"},
            transport=httpx.MockTransport(gateway.handler),
        )
        claims = TokenClaims(
            sub=str(env.user_id),
            tenant_id=str(env.tenant_id),
            department_id=str(uuid.UUID(int=9)),
            principals=["employee"],
            sid=str(uuid.uuid4()),
            exp=int(NOW.timestamp()) + 3600,
            iat=int(NOW.timestamp()),
        )
        request = SimpleNamespace(
            state=SimpleNamespace(auth_claims=claims),
            app=SimpleNamespace(
                state=SimpleNamespace(
                    session_factory=env.db.factory,
                    redis=CounterRedis(),
                    model=model,
                    checkpointer=None,  # an old reader still finds it; the loop ignores it
                )
            ),
        )
        try:
            return await chat_service.chat_turn(
                cast(Request, request),
                message,
                NOW,
                lambda _token: tools,
                chat_id,
            )
        finally:
            await model.aclose()

    return asyncio.run(scenario())


def stored_roles(env: Env, chat_id: str) -> list[tuple[str, str]]:
    """The chat's saved messages as (role, text), oldest first."""

    async def read() -> list[tuple[str, str]]:
        async with tenant_session(env.db.factory, env.tenant_id) as db:
            rows = await list_recent_messages(db, uuid.UUID(chat_id))
            return [(row.role, row.content) for row in rows]

    return asyncio.run(read())


def stored_question(env: Env, chat_id: str) -> object:
    """The raw open question on the chat row, read through a new session."""

    async def read() -> object:
        async with tenant_session(env.db.factory, env.tenant_id) as db:
            return await get_open_question(db, session_id=uuid.UUID(chat_id))

    return asyncio.run(read())


def lookup_body() -> dict[str, Any]:
    """A model step looking up the person and their access, so the request
    proposed next carries ids a lookup of this turn returned."""
    return answer_body(
        None,
        wire_call("call_me", "get_me", {}),
        wire_call("call_access", "list_my_access", {}),
    )


def lookup_answers(env: Env) -> dict[str, dict[str, Any]]:
    """What `get_me` and `list_my_access` answer: the person's id and the role id."""
    return {
        "get_me": {"user_id": str(env.user_id)},
        "list_my_access": {"grants": [{"role_id": str(env.viewer_id)}]},
    }


def create_call_body(env: Env) -> dict[str, Any]:
    """A model answer proposing one request for the speaker."""
    return answer_body(
        None,
        wire_call(
            "call_c",
            CREATE,
            {
                "target_user_id": str(env.user_id),
                "role_id": str(env.viewer_id),
                "justification": "Obviously fake reason: reading the runbooks.",
                "requested_expiry_days": 14,
            },
        ),
    )


def test_loop_is_the_only_engine(chat_settings_env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Catches a service that still reads an engine setting (an old
    `CHAT_ENGINE=pipeline` in the environment would then start the deleted
    pipeline), a settings class that still carries the field, a chat service
    that still holds the old runner, and a loop that answers but saves neither
    the user's message nor its reply on the chat row.

    The environment still says `pipeline`. The one staged gateway answer is a
    tool-calling step; the old graph would ask for a structured intent instead,
    so the model request is checked for the loop's shape (tools offered, no
    response format).
    """
    env = chat_settings_env
    monkeypatch.setenv("CHAT_ENGINE", "pipeline")
    gateway = Gateway(answer_body(LOOP_SAYS))

    response = say(env, gateway, FakeTools(), "What can you do for me?")

    assert "chat_engine" not in Settings.model_fields
    assert not hasattr(chat_service, "run_turn")
    assert response.reply == LOOP_SAYS
    assert response.outcome == TurnOutcome.DONE.value
    assert len(gateway.requests) == 1
    body = json.loads(gateway.requests[0].content)
    assert body["model"] == "agent-default"
    assert "tools" in body
    assert "response_format" not in body
    assert stored_roles(env, response.chat_id) == [
        ("user", "What can you do for me?"),
        ("assistant", LOOP_SAYS),
    ]


def test_yes_after_a_held_change_files_once(chat_settings_env: Env) -> None:
    """Catches a loop that holds a change nobody can later confirm (the open
    question is not saved to the chat row, or is saved in a shape it cannot
    read back), a yes that is answered as a new question instead of filing, a
    yes that files with a new key instead of the stored one, a question left on
    the row after filing, and a second yes that files again.

    Message one: the loop proposes a request; nothing is filed and a question is
    saved. Message two, "yes": the loop files the stored call, exactly as
    stored, with one tool-less model step. Message three, "yes" again: the
    question is gone, so a normal turn answers and files nothing more.
    """
    env = chat_settings_env
    tools = FakeTools(lookup_answers(env))
    gateway = Gateway(
        lookup_body(),
        create_call_body(env),
        answer_body(FILED_SAYS),
        answer_body("Nothing is waiting for a yes right now."),
    )

    asked = say(env, gateway, tools, ASK)

    assert asked.outcome == TurnOutcome.CONFIRM.value
    assert asked.reply is not None
    assert asked.reply.startswith("I'll file this:")
    assert tools.names.count(CREATE) == 0
    raw = stored_question(env, asked.chat_id)
    held = parse_open_question(raw, now=NOW)
    assert held is not None
    assert [call.tool for call in held.calls] == [CREATE]
    assert isinstance(raw, dict)
    stored_arguments = raw["calls"][0]["arguments"]
    assert stored_arguments["idempotency_key"]

    filed = say(env, gateway, tools, YES, chat_id=asked.chat_id)

    assert tools.arguments_of(CREATE) == [stored_arguments]
    assert filed.reply == FILED_SAYS
    answer_only = json.loads(gateway.requests[2].content)
    assert answer_only["tools"]
    assert answer_only["tool_choice"] == "none"
    assert stored_question(env, asked.chat_id) is None

    again = say(env, gateway, tools, YES, chat_id=asked.chat_id)

    assert tools.names.count(CREATE) == 1
    assert again.reply == "Nothing is waiting for a yes right now."
    assert len(gateway.requests) == 4


def test_eval_names_the_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Catches an eval answer with no `X-Chat-Engine` header, one that names a
    different engine, a run that still reports the old four prompt digests (the
    score would be filed under the wrong prompts), a run that still reads
    `CHAT_ENGINE` and so runs the deleted pipeline, and an eval service that
    keeps the old runner.

    The route runs one conversation with no engine setting and with an old
    `CHAT_ENGINE=pipeline` left in the environment. The loop's recording turn
    function must be called exactly once per run, and the headers must name the
    loop and its one prompt digest.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://fake:fake@db.invalid:5432/fake_eval")
    monkeypatch.setenv("REDIS_URL", "redis://redis.invalid:6379/0")
    monkeypatch.setenv("QDRANT_URL", "http://qdrant.invalid:6333")
    monkeypatch.setenv("JWT_SECRET", EVAL_JWT_PHRASE)
    monkeypatch.setenv("MCP_URL", "http://mcp.invalid:8081/mcp")
    monkeypatch.setenv("EVAL_ROUTES_ENABLED", "true")
    monkeypatch.setenv("CHAT_ENGINE", "pipeline")

    async def resolve(
        _factory: object, *, tenant_slug: str, principal_user: str
    ) -> FakeSeat | None:
        return (
            FakeSeat() if (tenant_slug, principal_user) == (TENANT_SLUG, PRINCIPAL_USER) else None
        )

    loop_turns = ConvTurns([ConvStep(update=_plain())])
    stub = StubCaller({}, {})
    monkeypatch.setattr(eval_turn, "resolve_seat", resolve)
    monkeypatch.setattr(eval_turn, "run_loop_turn", loop_turns)
    monkeypatch.setattr(eval_turn, "build_tool_caller", lambda _settings, _token: stub)
    model = ModelClient(
        base_url=GATEWAY_URL,
        api_keys={TENANT_SLUG: "fake-eval-key"},
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    monkeypatch.setattr(app.state, "session_factory", RecordingFactory(), raising=False)
    monkeypatch.setattr(app.state, "model", model, raising=False)
    monkeypatch.setattr(app.state, "checkpointer", None, raising=False)

    async def post(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post("/v1/eval/conversation", json=_conv_body())

    looped = _drive(post)

    digest = hashlib.sha256((PROMPTS_DIR / "agent_loop.md").read_bytes()).hexdigest()
    assert not hasattr(eval_turn, "run_turn")
    assert looped.status_code == 200
    assert looped.headers["X-Chat-Engine"] == "loop"
    assert looped.headers["X-Prompt-Versions"] == f"loop={digest}"
    assert len(loop_turns.calls) == 1
