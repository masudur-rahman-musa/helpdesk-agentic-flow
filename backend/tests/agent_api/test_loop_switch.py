"""The `CHAT_ENGINE` switch between the old pipeline and the agent loop.

Expected implementation surface:

``helpdesk.shared.core.config.Settings.chat_engine`` (env ``CHAT_ENGINE``):
    ``"pipeline"`` (the default) or ``"loop"``; any other value fails loading.
``helpdesk.agent_api.services.chat``
    ``chat_turn`` calls ``run_loop_turn`` when the engine is ``loop`` and
    ``run_turn`` otherwise; both names are module attributes.
``helpdesk.agent_api.services.eval_turn``
    ``run_conversation`` calls the module attribute ``run_loop_turn`` under
    ``loop``; ``prompt_versions("loop")`` is ``loop=<sha256 of agent_loop.md>``;
    the eval route adds the ``X-Chat-Engine`` header.

Cost. Postgres is the shared `agent_db` session fixture: only Postgres shows
the open question saved on the chat row by the loop and picked up from it by
the old graph on the next message. Each case seeds its own tenant and uses a
new chat id, so nothing is shared. The gateway is an `httpx.MockTransport`, the
tool server a recording fake and Redis a counter stub: no socket is opened.
"""

import asyncio
import hashlib
import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from fastapi import Request
from pydantic import ValidationError

from helpdesk.agent_api.agent.confirmation import parse_open_question
from helpdesk.agent_api.agent.prompts import PROMPTS_DIR
from helpdesk.agent_api.agent.state import TurnOutcome, TurnState
from helpdesk.agent_api.api.router import app
from helpdesk.agent_api.schemas.chat import ChatResponse
from helpdesk.agent_api.services import chat as chat_service
from helpdesk.agent_api.services import eval_turn
from helpdesk.agent_api.services.model_gateway.client import ModelClient
from helpdesk.shared.auth.jwt import TokenClaims
from helpdesk.shared.core.config import Settings
from helpdesk.shared.core.db import tenant_session
from helpdesk.shared.crud.messages import list_recent_messages
from tests.agent_api.conftest import AgentDb
from tests.agent_api.test_agent_confirm import (
    COMPOSED,
    CREATE,
    EMPTY_ROUND,
    FakeTools,
    _as_composed,
    _stored,
)
from tests.agent_api.test_agent_confirm import Env as ConfirmEnv
from tests.agent_api.test_agent_confirm import _seed as _seed_confirm_tenant
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
PIPELINE_SAYS = "reply from the old pipeline"


@pytest.fixture
def seeded(agent_db: AgentDb) -> ConfirmEnv:
    """A new tenant with one person, one system and two roles, for one case."""
    return asyncio.run(_seed_confirm_tenant(agent_db))


@pytest.fixture
def chat_settings_env(seeded: ConfirmEnv, monkeypatch: pytest.MonkeyPatch) -> ConfirmEnv:
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


class MixedGateway:
    """Staged answers in order. A dict is a whole gateway body (the loop's
    tool-calling steps); a string is the content of a structured answer (the old
    graph's planner and writer). A request past the last one fails the run."""

    def __init__(self, *items: Mapping[str, Any] | str) -> None:
        self._items = list(items)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        index = len(self.requests) - 1
        if index >= len(self._items):
            raise AssertionError(
                f"the turn made {len(self.requests)} model requests; "
                f"only {len(self._items)} were staged"
            )
        item = self._items[index]
        if isinstance(item, str):
            item = answer_body(_as_composed(item, request))
        return httpx.Response(200, json=dict(item))


def say(
    env: ConfirmEnv,
    gateway: MixedGateway,
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
                    checkpointer=None,
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


def stored_roles(env: ConfirmEnv, chat_id: str) -> list[tuple[str, str]]:
    """The chat's saved messages as (role, text), oldest first."""

    async def read() -> list[tuple[str, str]]:
        async with tenant_session(env.db.factory, env.tenant_id) as db:
            rows = await list_recent_messages(db, uuid.UUID(chat_id))
            return [(row.role, row.content) for row in rows]

    return asyncio.run(read())


def create_call_body(env: ConfirmEnv) -> dict[str, Any]:
    """A model answer proposing one request for the speaker."""
    return answer_body(
        None,
        wire_call(
            "call_c",
            "create_access_request",
            {
                "target_user_id": str(env.user_id),
                "role_id": str(env.viewer_id),
                "justification": "Obviously fake reason: reading the runbooks.",
                "requested_expiry_days": 14,
            },
        ),
    )


def test_pipeline_is_the_default(
    chat_settings_env: ConfirmEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a switch that defaults to the loop, or that ignores the setting
    and always runs the loop: with no `CHAT_ENGINE` set, a chat turn must run
    the old graph once and never touch the loop.

    The loop is replaced by a function that fails the run if it is called, so
    only the default can keep the old graph on the road.
    """
    env = chat_settings_env
    monkeypatch.delenv("CHAT_ENGINE", raising=False)
    old_calls: list[dict[str, Any]] = []

    async def old_graph(**kwargs: Any) -> TurnState:
        old_calls.append(kwargs)
        return TurnState(
            tenant_id=kwargs["tenant_id"],
            user_id=kwargs["user_id"],
            session_id=kwargs["session_id"],
            tenant_slug=kwargs["tenant_slug"],
            user_message=kwargs["message"],
            outcome=TurnOutcome.DONE,
            reply=PIPELINE_SAYS,
        )

    async def loop_must_not_run(**kwargs: Any) -> TurnState:
        raise AssertionError("the loop ran although CHAT_ENGINE was not set")

    monkeypatch.setattr(chat_service, "run_turn", old_graph)
    monkeypatch.setattr(chat_service, "run_loop_turn", loop_must_not_run)

    response = say(env, MixedGateway(), FakeTools(), "hello")

    assert len(old_calls) == 1
    assert old_calls[0]["message"] == "hello"
    assert response.reply == PIPELINE_SAYS


@pytest.mark.parametrize("value", ["agent", "LOOP", "Pipeline", "loop ", "loop\n", "loop\r\n"])
def test_unknown_engine_fails_at_start(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unhappy path. Catches a setting typed as a free string (a typo such as
    `agent` would start the app on some default engine), one that cleans the
    value before checking it, and one that forgets to read the environment.

    Every other required name is set, so the only thing that can fail loading is
    `CHAT_ENGINE`; the error must name that field. The two real values load,
    and the unset one is `pipeline`.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://fake:fake@db.invalid:5432/fake")
    monkeypatch.setenv("REDIS_URL", "redis://redis.invalid:6379/0")
    monkeypatch.setenv("QDRANT_URL", "http://qdrant.invalid:6333")
    monkeypatch.setenv("JWT_SECRET", JWT_PHRASE)

    monkeypatch.delenv("CHAT_ENGINE", raising=False)
    assert Settings(_env_file=None).chat_engine == "pipeline"  # type: ignore[call-arg]
    monkeypatch.setenv("CHAT_ENGINE", "loop")
    assert Settings(_env_file=None).chat_engine == "loop"  # type: ignore[call-arg]
    monkeypatch.setenv("CHAT_ENGINE", "pipeline")
    assert Settings(_env_file=None).chat_engine == "pipeline"  # type: ignore[call-arg]

    monkeypatch.setenv("CHAT_ENGINE", value)
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)  # type: ignore[call-arg]

    assert "chat_engine" in {str(error["loc"][0]) for error in excinfo.value.errors()}


def test_loop_engine_serves_chat(
    chat_settings_env: ConfirmEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a switch that sets the engine but still runs the old graph, a
    loop that answers but saves neither the user's message nor its reply on the
    chat row, and one that saves them in the wrong order.

    The one staged gateway answer is a tool-calling step; the old graph would
    ask for a structured intent instead, so the model request is checked for
    the loop's shape (tools offered, no response format).
    """
    env = chat_settings_env
    monkeypatch.setenv("CHAT_ENGINE", "loop")
    gateway = MixedGateway(answer_body(LOOP_SAYS))

    response = say(env, gateway, FakeTools(), "What can you do for me?")

    assert response.reply == LOOP_SAYS
    assert response.outcome == TurnOutcome.DONE.value
    assert len(gateway.requests) == 1
    body = json.loads(gateway.requests[0].content)
    assert body["model"] == "agent-strong"
    assert "tools" in body
    assert "response_format" not in body
    assert stored_roles(env, response.chat_id) == [
        ("user", "What can you do for me?"),
        ("assistant", LOOP_SAYS),
    ]


def test_yes_after_a_held_change_files_once(
    chat_settings_env: ConfirmEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a loop that holds a change nobody can later confirm (the open
    question is not saved to the chat row, or is saved in a shape the old graph
    cannot read), a yes that is sent to the loop instead of the old graph, a
    yes that files with a new key instead of the stored one, and a second yes
    that files again.

    Message one: the loop proposes a request; nothing is filed and a question
    is saved. Message two, "yes": the old graph files the stored call, exactly
    as stored. Message three, "yes" again: the question is gone, so the loop
    answers and files nothing more.
    """
    env = chat_settings_env
    monkeypatch.setenv("CHAT_ENGINE", "loop")
    tools = FakeTools()
    gateway = MixedGateway(
        create_call_body(env),
        EMPTY_ROUND,
        COMPOSED,
        answer_body("Nothing is waiting for a yes right now."),
    )

    asked = say(env, gateway, tools, ASK)

    assert asked.outcome == TurnOutcome.CONFIRM.value
    assert asked.reply is not None
    assert asked.reply.startswith("I'll file this:")
    assert tools.names.count(CREATE) == 0
    raw = _stored(env, uuid.UUID(asked.chat_id))
    held = parse_open_question(raw, now=NOW)
    assert held is not None
    assert [call.tool for call in held.calls] == [CREATE]
    assert isinstance(raw, dict)
    stored_arguments = raw["calls"][0]["arguments"]
    assert stored_arguments["idempotency_key"]

    filed = say(env, gateway, tools, YES, chat_id=asked.chat_id)

    assert tools.arguments_of(CREATE) == [stored_arguments]
    assert filed.reply == COMPOSED
    assert _stored(env, uuid.UUID(asked.chat_id)) is None

    again = say(env, gateway, tools, YES, chat_id=asked.chat_id)

    assert tools.names.count(CREATE) == 1
    assert again.reply == "Nothing is waiting for a yes right now."
    assert len(gateway.requests) == 4


def test_eval_names_the_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Catches an eval answer with no `X-Chat-Engine` header, one that names
    the engine by a constant, a loop run that still reports the old four prompt
    digests (the score would be filed under the wrong prompts), and a pipeline
    run whose headers changed.

    Both engines run one conversation through the route. Each engine has its own
    recording turn function, and each must be called exactly once, by its own
    engine only.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://fake:fake@db.invalid:5432/fake_eval")
    monkeypatch.setenv("REDIS_URL", "redis://redis.invalid:6379/0")
    monkeypatch.setenv("QDRANT_URL", "http://qdrant.invalid:6333")
    monkeypatch.setenv("JWT_SECRET", EVAL_JWT_PHRASE)
    monkeypatch.setenv("MCP_URL", "http://mcp.invalid:8081/mcp")
    monkeypatch.setenv("EVAL_ROUTES_ENABLED", "true")

    async def resolve(
        _factory: object, *, tenant_slug: str, principal_user: str
    ) -> FakeSeat | None:
        return (
            FakeSeat() if (tenant_slug, principal_user) == (TENANT_SLUG, PRINCIPAL_USER) else None
        )

    loop_turns = ConvTurns([ConvStep(update=_plain())])
    pipeline_turns = ConvTurns([ConvStep(update=_plain())])
    stub = StubCaller({}, {})
    monkeypatch.setattr(eval_turn, "resolve_seat", resolve)
    monkeypatch.setattr(eval_turn, "run_loop_turn", loop_turns)
    monkeypatch.setattr(eval_turn, "run_turn", pipeline_turns)
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

    monkeypatch.setenv("CHAT_ENGINE", "loop")
    looped = _drive(post)
    monkeypatch.setenv("CHAT_ENGINE", "pipeline")
    piped = _drive(post)

    digest = hashlib.sha256((PROMPTS_DIR / "agent_loop.md").read_bytes()).hexdigest()
    assert looped.status_code == 200
    assert looped.headers["X-Chat-Engine"] == "loop"
    assert looped.headers["X-Prompt-Versions"] == f"loop={digest}"
    assert len(loop_turns.calls) == 1
    assert piped.status_code == 200
    assert piped.headers["X-Chat-Engine"] == "pipeline"
    assert piped.headers["X-Prompt-Versions"].startswith("intent=")
    assert "loop=" not in piped.headers["X-Prompt-Versions"]
    assert len(pipeline_turns.calls) == 1
