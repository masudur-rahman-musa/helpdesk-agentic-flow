"""An earlier change §6: the approval branch as a pure gate, then through the graph.

Expected implementation surface (the implementer must provide exactly this):

``agent_api.graph.state``
    ``TurnOutcome.AWAIT_APPROVAL = "await_approval"`` — the seventh member.
    ``TurnState.awaiting_request_id: uuid.UUID | None`` — the request a paused
        turn is waiting on, and what an earlier change's resume matches a decision
        against.
``agent_api.graph.gates``
    ``def approval_outcome(results: Sequence[ToolResult]) -> TurnOutcome`` —
        PURE, no framework and no store. ``AWAIT_APPROVAL`` when a SUCCESSFUL
        ``create_access_request`` result's payload says
        ``decision == "approval"``; ``FAILED`` when such a payload carries no
        usable ``request_id``; ``CONTINUE`` otherwise (decision F).
``agent_api.graph.nodes.await_approval``
    ``PENDING_APPROVAL_REPLY: str`` — the node's OWN template (decision E),
        never a composed reply.
    ``async def await_approval(state, deps) -> TurnState`` — calls LangGraph's
        ``interrupt()`` with the request id.
``agent_api.graph.build``
    ``def build_graph(checkpointer: BaseCheckpointSaver | None = None)`` —
        argument-free call still compiles with none.
    ``run_turn(..., checkpointer=...)`` — passes the thread config and maps the
        interrupt to a paused ``TurnState``: ``TurnOutcome.AWAIT_APPROVAL``,
        ``awaiting_request_id`` and the templated reply.

Cost and isolation. The compose Postgres only — no Redis, no Qdrant, no
gateway socket and no MCP server; the gateway is an ``httpx.MockTransport``
and the tool caller is a scripted stub. ONE throwaway database, one seeded
fake tenant and LangGraph's checkpoint tables, all built once for the whole
module; per-case isolation is a namespace — a fresh
``session_id``, which IS the checkpoint thread id (decision A1) — never a new
database and never a new set of tables. A pool and an engine are opened per
``asyncio.run``, because each case drives its own event loop and a connection
pool belongs to the loop it was opened in; that is the shape
``test_agent_turn.py`` already uses for its engine.
"""

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from helpdesk.agent_api.checkpointer import (
    build_checkpointer,
    build_pool,
    psycopg_dsn,
    thread_config,
)
from helpdesk.agent_api.checkpointer import setup_checkpointer as setup_checkpoint_tables
from helpdesk.agent_api.graph.build import TurnDeps, run_turn
from helpdesk.agent_api.graph.gates import approval_outcome
from helpdesk.agent_api.graph.nodes.await_approval import PENDING_APPROVAL_REPLY
from helpdesk.agent_api.graph.nodes.extract_intent import FAILURE_REPLY
from helpdesk.agent_api.graph.state import ToolResult, TurnOutcome
from helpdesk.agent_api.mcp_client import ToolCaller
from helpdesk.agent_api.services.model_gateway.client import ModelClient
from helpdesk.agent_api.services.model_gateway.strict_schema import strict_schema_problems
from helpdesk.agent_api.tools.catalogue import SEARCH_POLICY
from helpdesk.shared.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.shared.core.settings import load_settings
from helpdesk.shared.errors import ErrorCode
from helpdesk.shared.models import Base, Department, Role, System, Tenant, User

NOW = datetime(2026, 6, 2, 10, 15, tzinfo=UTC)

GATEWAY_URL = "http://gateway.invalid:8080"

TENANT_SLUG = "approval-tenant"

# Obviously fake, tests only: this is not a credential.
API_KEYS: dict[str, str] = {TENANT_SLUG: f"fake-key-{TENANT_SLUG}"}

CREATE_REQUEST = "create_access_request"
CLASSIFY = "classify_access_request"

REQUEST_ID = uuid.UUID("66666666-6666-5666-8666-666666666666")
"""The request id a paused turn is waiting on, obviously fake."""

AWAIT_APPROVAL_VALUE = "await_approval"
"""The seventh outcome's wire value, written out.

The same literal `test_agent_gates.py::test_turn_outcome_has_exactly_the_seven_members`
pins on the enum: this file reads it back out of a Postgres checkpoint, where
the member is a string and nothing else.
"""

_UNREACHABLE_MSG = (
    "Postgres is unreachable at the configured DATABASE_URL.\n"
    "Plan 18a approval tests require the compose stack: run `docker compose up -d` first.\n"
    "Original error: {error!r}"
)


# --- the gate, with no graph at all -------------------------------------------


def _payload(
    *, decision: str, request_id: object = str(REQUEST_ID), **extra: object
) -> dict[str, Any]:
    """A `create_access_request` answer as the tool puts it on the wire.

    Json mode, exactly as `execute_tools` records it: the ids are strings, so a
    gate that compared a `uuid.UUID` would be reading the wrong shape.
    """
    payload: dict[str, Any] = {
        "request_id": request_id,
        "decision": decision,
        "status": "pending" if decision == "approval" else "approved",
        "required_approvers": ["manager", "system_owner"],
        **extra,
    }
    return payload


def _ok(tool: str, payload: Mapping[str, Any]) -> ToolResult:
    return ToolResult(tool=tool, ok=True, payload=dict(payload))


APPROVAL_RESULT = _ok(CREATE_REQUEST, _payload(decision="approval"))
AUTO_RESULT = _ok(CREATE_REQUEST, _payload(decision="auto"))
BLOCKED_RESULT = _ok(CREATE_REQUEST, _payload(decision="blocked"))
FAILED_RESULT = ToolResult(tool=CREATE_REQUEST, ok=False, error_code=ErrorCode.POLICY_BLOCKED)
CLASSIFY_RESULT = _ok(CLASSIFY, _payload(decision="approval"))


@pytest.mark.parametrize(
    ("results", "expected"),
    [
        ((), TurnOutcome.CONTINUE),
        ((AUTO_RESULT,), TurnOutcome.CONTINUE),
        ((APPROVAL_RESULT,), TurnOutcome.AWAIT_APPROVAL),
        ((BLOCKED_RESULT,), TurnOutcome.CONTINUE),
        ((FAILED_RESULT,), TurnOutcome.CONTINUE),
        ((CLASSIFY_RESULT,), TurnOutcome.CONTINUE),
        ((CLASSIFY_RESULT, APPROVAL_RESULT), TurnOutcome.AWAIT_APPROVAL),
    ],
)
def test_approval_outcome_answers_await_only_for_an_approval_decision(
    results: Sequence[ToolResult], expected: TurnOutcome
) -> None:
    """Catches a gate that pauses on anything a request answers with:

    the `auto` row catches a predicate keyed on "a request was filed at all",
    which would hang every auto-granted turn waiting for an approval nobody
    owes; the `blocked` row catches one keyed on "the request is not approved",
    which would pause a turn that policy already refused;
    the FAILED row catches a gate reading `payload` without checking `ok`,
    since a refused call carries no decision at all;
    and the `classify_access_request` row is decision F — the same payload
    under a different tool must not pause the turn, so a gate that scanned for
    a `decision` key anywhere in the round is red on it.

    The last row is the one the earlier rows cannot decide: a gate that only
    ever looked at `results[0]` would answer `CONTINUE` for a round whose
    classification ran before the request it justifies, which is the ordinary
    two-call round this branch exists for."""
    assert approval_outcome(results) is expected


@pytest.mark.parametrize("request_id", [None, "", "   "])
def test_approval_outcome_fails_a_decision_with_no_request_id(request_id: object) -> None:
    """Catches a gate that pauses on an approval decision whose payload names
    no usable request: nothing could ever decide that pause — 18b's resume
    matches a decision against `awaiting_request_id` — so the turn would sit
    paused for ever with a reply telling the user a request is waiting.

    The empty and whitespace rows catch an `if "request_id" in payload` or an
    `is None` check, both of which read a present-but-empty id as an answer."""
    results = (_ok(CREATE_REQUEST, _payload(decision="approval", request_id=request_id)),)

    assert approval_outcome(results) is TurnOutcome.FAILED


def test_approval_outcome_fails_a_decision_whose_payload_omits_the_request_id() -> None:
    """Catches the same gap when the key is ABSENT rather than empty: a gate
    written as `payload["request_id"]` raises `KeyError` inside a node that
    must never raise, and one written as `payload.get("request_id", "unknown")`
    would pause on a request id it invented."""
    payload = _payload(decision="approval")
    del payload["request_id"]

    assert approval_outcome((_ok(CREATE_REQUEST, payload),)) is TurnOutcome.FAILED


# --- the throwaway database, its tenant and its checkpoint tables --------------


async def _admin_execute(admin_url: str, sql: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


@dataclass(frozen=True)
class ApprovalEnv:
    """One database, one fake tenant, and the DSN the checkpointer uses."""

    database_url: str
    dsn: str
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    system_id: uuid.UUID
    role_id: uuid.UUID


async def _seed(factory: async_sessionmaker[AsyncSession]) -> tuple[uuid.UUID, ...]:
    """One obviously fake tenant with one person, one system and one role."""
    async with factory() as session:
        tenant = Tenant(slug=TENANT_SLUG, name="Fake Approval Tenant", residency="eu", settings={})
        session.add(tenant)
        await session.commit()
        tenant_id = tenant.id

    async with tenant_session(factory, tenant_id) as session:
        department = Department(tenant_id=tenant_id, name="fake-dept", manager_user_id=None)
        session.add(department)
        await session.flush()

        person = User(
            tenant_id=tenant_id,
            department_id=department.id,
            email=f"fake.person@{TENANT_SLUG}.example.test",
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
            slug="payroll",
            name="Fake Payroll",
            sensitivity="low",
            owner_user_id=person.id,
        )
        session.add(system)
        await session.flush()

        role = Role(
            tenant_id=tenant_id,
            system_id=system.id,
            slug="payroll-read",
            name="Fake Payroll Read",
            privilege="read",
            default_expiry_days=90,
        )
        session.add(role)
        await session.flush()

        return tenant_id, person.id, system.id, role.id


@pytest.fixture(scope="session")
def approval_env() -> Iterator[ApprovalEnv]:
    """The conversation schema, one seeded tenant and the checkpoint tables.

    Built exactly once. A throwaway database rather than the
    compose one for the reason the plan's risk section gives: `test_models.py`
    and `test_seed.py` both assert on a database's full table list, and
    LangGraph's tables are outside `Base.metadata` on purpose.
    """
    try:
        settings = load_settings()
    except ValidationError as exc:
        pytest.fail(
            f"DATABASE_URL is not configured (env or .env). Copy .env.example to .env.\n{exc}",
            pytrace=False,
        )
    admin_url = settings.database_url
    try:
        asyncio.run(_admin_execute(admin_url, "SELECT 1"))
    except Exception as exc:
        pytest.fail(_UNREACHABLE_MSG.format(error=exc), pytrace=False)

    name = f"helpdesk_approval_test_{uuid.uuid4().hex[:12]}"
    asyncio.run(_admin_execute(admin_url, f'CREATE DATABASE "{name}"'))
    url = make_url(admin_url).set(database=name).render_as_string(hide_password=False)

    async def prepare() -> ApprovalEnv:
        engine = create_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            tenant_id, user_id, system_id, role_id = await _seed(create_session_factory(engine))
        finally:
            await engine.dispose()
        return ApprovalEnv(
            database_url=url,
            dsn=psycopg_dsn(url),
            tenant_id=tenant_id,
            user_id=user_id,
            system_id=system_id,
            role_id=role_id,
        )

    env = asyncio.run(prepare())
    _with_pool(env.dsn, setup_checkpoint_tables)
    yield env
    assert name.startswith("helpdesk_approval_test_")
    asyncio.run(_admin_execute(admin_url, f'DROP DATABASE "{name}" WITH (FORCE)'))


def _with_pool[T](dsn: str, scenario: Callable[[Any], Awaitable[T]]) -> T:
    """Run one scenario over a pool opened and closed inside its own loop.

    The explicit close is the plan's answer to its own second risk: a second
    driver into the shared compose database that never closed its pool is the
    connection leak nothing else in this suite would see.
    """

    async def runner() -> T:
        pool = build_pool(dsn)
        async with pool:
            return await scenario(pool)

    return asyncio.run(runner())


# --- the stubbed gateway and the scripted caller ------------------------------


def _completion(answer: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": answer}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )


class StagedGateway:
    """Answers staged in order; a request past the last one fails the test.

    That is how "the turn never composed a reply" is asserted as a hard stop:
    decision E renders the paused reply from a template, so a run that reached
    `compose_reply` asks for an answer nobody staged.
    """

    def __init__(self, answers: Sequence[str]) -> None:
        self._answers = list(answers)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        schema = json.loads(request.content).get("response_format", {}).get("json_schema", {})
        if strict_schema_problems(schema.get("schema", {})):
            # As OpenAI answers a schema strict mode refuses, so a
            # non-strict schema cannot pass this suite unseen.
            return httpx.Response(400, json={"error": "the schema breaks strict mode"})
        index = len(self.requests) - 1
        if index >= len(self._answers):
            raise AssertionError(
                f"the turn made {len(self.requests)} model requests; "
                f"only {len(self._answers)} were staged"
            )
        return _completion(self._answers[index])

    @property
    def call_count(self) -> int:
        return len(self.requests)


class ScriptedToolCaller:
    """A `ToolCaller` answering one scripted payload per call, in order.

    A call beyond the script fails the test rather than repeating the last
    answer, so "no tool was called after the pause" is caught where it happens.
    """

    def __init__(self, *answers: Mapping[str, Any]) -> None:
        self._answers = list(answers)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> ToolCaller:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def call_tool(self, name: str, arguments: BaseModel) -> Mapping[str, Any]:
        self.calls.append((name, arguments.model_dump(mode="json")))
        index = len(self.calls) - 1
        if index >= len(self._answers):
            raise AssertionError(
                f"the turn made {len(self.calls)} tool calls ({self.names}); "
                f"only {len(self._answers)} were scripted"
            )
        return self._answers[index]

    @property
    def names(self) -> list[str]:
        return [name for name, _arguments in self.calls]


CITED_DOCUMENT = "fake-access-policy"


def _page() -> dict[str, Any]:
    """One `search_policy` page with a single obviously fake citation."""
    return {
        "citations": [
            {
                "document": CITED_DOCUMENT,
                "title": "Fake Access Policy",
                "version": "v1",
                "heading_path": ["Fake Section"],
                "chunk_id": str(uuid.uuid4()),
                "text": "Obviously fake policy text about fake payroll access.",
                "score": 0.42,
            }
        ],
        "refused": False,
    }


def _extraction_answer() -> str:
    return json.dumps(
        {
            "intent": "request_access",
            "entities": {
                "system": "payroll",
                "role": "payroll-read",
                "target_user": None,
                "department": None,
                "expiry_days": None,
                "justification": None,
            },
            "confidence": 0.95,
            "clarifying_question": None,
        }
    )


def _plan_answer(env: ApprovalEnv) -> str:
    """One planning round proposing exactly the request this plan pauses on."""
    return json.dumps(
        {
            "calls": [
                {
                    "tool": CREATE_REQUEST,
                    "arguments": json.dumps(
                        {
                            "target_user_id": str(env.user_id),
                            "role_id": str(env.role_id),
                            "justification": "Obviously fake justification.",
                            "requested_expiry_days": None,
                            "citations": [],
                        }
                    ),
                }
            ]
        }
    )


def _run_paused_turn(
    env: ApprovalEnv,
    *,
    session_id: uuid.UUID,
    gateway: StagedGateway,
    tools: ScriptedToolCaller,
) -> Any:
    """One turn against the real checkpointer, over this case's own thread id."""

    async def scenario(pool: Any) -> Any:
        checkpointer = build_checkpointer(pool)
        engine = create_engine(env.database_url)
        try:
            factory = create_session_factory(engine)
            client = ModelClient(
                base_url=GATEWAY_URL,
                api_keys=API_KEYS,
                transport=httpx.MockTransport(gateway.handler),
            )
            try:
                async with tenant_session(factory, env.tenant_id) as db:
                    deps = TurnDeps(db=db, model=client, now=lambda: NOW, tools=tools)
                    return await run_turn(
                        deps=deps,
                        tenant_id=env.tenant_id,
                        user_id=env.user_id,
                        session_id=session_id,
                        tenant_slug=TENANT_SLUG,
                        message="I need access to fake payroll",
                        checkpointer=checkpointer,
                    )
            finally:
                await client.aclose()
        finally:
            await engine.dispose()

    return _with_pool(env.dsn, scenario)


def _plain(value: Any) -> Any:
    """A checkpointed channel value as plain JSON-comparable data.

    LangGraph's serde may hand back the model, a dict or a string depending on
    how the state was written; every assertion below turns on the DATA, so the
    shape is normalised here rather than pinned.
    """
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _checkpointed_state(env: ApprovalEnv, session_id: uuid.UUID) -> dict[str, Any]:
    """The paused thread's channel values, read by a SECOND checkpointer.

    A second pool and a second saver on purpose: this is criterion 2 — the
    state has to be readable after the compiled graph that wrote it is gone,
    which is what an earlier change's resume depends on. It is not a per-test backing
    service; the database and its tables are still the
    session fixture's, and only the reader is new.
    """

    async def scenario(pool: Any) -> dict[str, Any]:
        saver = build_checkpointer(pool)
        tuple_ = await saver.aget_tuple(thread_config(str(session_id)))
        assert tuple_ is not None, "no checkpoint was written for this thread at all"
        values: dict[str, Any] = dict(tuple_.checkpoint["channel_values"])
        return {key: _plain(value) for key, value in values.items()}

    return _with_pool(env.dsn, scenario)


# --- the paused turn ----------------------------------------------------------


def test_a_paused_turn_returns_await_approval_and_the_request_id(
    approval_env: ApprovalEnv,
) -> None:
    """Catches a `run_turn` that reads LangGraph's `__interrupt__` answer as a
    finished state: `ainvoke` comes back with the interrupt rather than the
    turn's state, so a mapping that skipped it returns the pre-pause outcome
    and the route answers a waiting user as though the turn were done.

    It also catches decision E going the other way — a paused turn routed
    through `compose_reply` — because no composition answer is staged, so a
    third gateway request fails the run outright rather than passing quietly.

    The `awaiting_request_id` assertion is what tells this state from any
    other pause: 18b refuses a decision whose request id does not match it, so
    a `None` here disarms that check before it is written."""
    session_id = uuid.uuid4()
    gateway = StagedGateway([_extraction_answer(), _plan_answer(approval_env)])
    tools = ScriptedToolCaller(_page(), _payload(decision="approval"))

    state = _run_paused_turn(approval_env, session_id=session_id, gateway=gateway, tools=tools)

    assert state.outcome is TurnOutcome.AWAIT_APPROVAL
    assert state.awaiting_request_id is not None
    assert str(state.awaiting_request_id) == str(REQUEST_ID)
    assert state.reply is not None
    assert state.reply.startswith(PENDING_APPROVAL_REPLY.split("{")[0])
    assert tools.names == [SEARCH_POLICY, CREATE_REQUEST]
    assert gateway.call_count == 2


def test_the_paused_state_is_readable_from_a_second_checkpointer(
    approval_env: ApprovalEnv,
) -> None:
    """Catches a pause that only exists in the process that made it: a graph
    compiled with no checkpointer, one compiled with a `MemorySaver`, or a
    `run_turn` that never passes the thread config all return the same paused
    state to the caller and leave NOTHING in Postgres — so the resume an earlier change
    builds on has nothing to read and every restart drops every pending
    approval.

    The four values are the ones a resume needs: the citations the request was
    decided against, the results it was filed by, the request id a decision is
    matched to, and the outcome that says this thread is paused rather than
    finished. Reading them through a saver built after the writing graph is
    gone is the only arrangement in which an in-process saver cannot pass."""
    session_id = uuid.uuid4()
    gateway = StagedGateway([_extraction_answer(), _plan_answer(approval_env)])
    tools = ScriptedToolCaller(_page(), _payload(decision="approval"))

    _run_paused_turn(approval_env, session_id=session_id, gateway=gateway, tools=tools)
    values = _checkpointed_state(approval_env, session_id)

    assert str(values["outcome"]) == AWAIT_APPROVAL_VALUE
    assert str(values["awaiting_request_id"]) == str(REQUEST_ID)
    assert [citation["document"] for citation in values["citations"]] == [CITED_DOCUMENT]
    assert [result["tool"] for result in values["tool_results"]] == [CREATE_REQUEST]


def test_a_decision_with_no_request_id_ends_the_turn_and_writes_no_paused_checkpoint(
    approval_env: ApprovalEnv,
) -> None:
    """The unhappy path through the graph. Catches a node that pauses on the
    decision alone and reads the request id afterwards: the turn would stop on
    an interrupt nobody can resume, because 18b matches a decision against
    `awaiting_request_id` and there is none — a checkpoint claiming an
    approval nobody can decide, plus a reply telling the user their request is
    with an approver.

    The reply pins which ending was taken: `FAILURE_REPLY` and not the pending
    template, so a version that failed the turn but still rendered the pause
    is red. The checkpoint half is what tells a turn that never paused from
    one that paused and was rolled back."""
    session_id = uuid.uuid4()
    gateway = StagedGateway([_extraction_answer(), _plan_answer(approval_env)])
    tools = ScriptedToolCaller(_page(), _payload(decision="approval", request_id=None))

    state = _run_paused_turn(approval_env, session_id=session_id, gateway=gateway, tools=tools)

    assert state.outcome is TurnOutcome.FAILED
    assert state.reply == FAILURE_REPLY
    assert state.awaiting_request_id is None

    values = _checkpointed_state(approval_env, session_id)
    assert str(values["outcome"]) != AWAIT_APPROVAL_VALUE
    assert values.get("awaiting_request_id") is None
