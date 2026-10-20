"""The SSE frame encoder, and `stream_turn` with no app at all.

Expected implementation surface (the implementer must provide exactly this):

``agent_api.sse`` — the wire format and nothing else. Pure: it takes values
and returns strings, so every case in the first half of this file runs with no
app, no graph and no database.
    ``STEP = "step"`` · ``TOKEN = "token"`` · ``REPLY = "reply"``
    ``ERROR = "error"`` · ``DONE = "done"`` — the five event names.
    ``REPLY_CHUNK_CHARS = 64`` — how many characters one ``token`` event
        carries (the last requirement, pinned from both sides below).
    ``def chunk_reply(reply: str) -> tuple[str, ...]`` — the CHECKED reply cut
        into ``REPLY_CHUNK_CHARS``-character pieces, in order, whose
        concatenation is the reply again.
    ``def encode_event(event: str, data: Mapping[str, Any]) -> str`` — one SSE
        frame: ``event: {event}\\ndata: {one line of JSON}\\n\\n``. The data
        line is ONE line whatever the payload contains (the contract's fourth
        criterion), which ``json.dumps`` gives for free by escaping ``\\n`` —
        and which an implementation that pretty-printed, or that wrote the
        reply text raw, would lose.

``agent_api.graph.build``
    ``async def stream_turn(*, deps: TurnDeps, tenant_id, user_id, session_id,
        tenant_slug, message, checkpointer: Saver | None = None)
        -> AsyncIterator[str]`` — an async GENERATOR beside ``run_turn``,
        taking the same arguments and running the same two pre-checks, over
        ``graph.astream(stream_mode="updates")``. It yields whole encoded
        frames, so the route is a ``StreamingResponse`` over it and nothing
        else, and both this file and ``test_chat_stream.py`` parse one format.

Three shapes the requirements leave open, fixed here, owed to the plan's
Deviations:

1. A ``step`` event's data is EXACTLY the three keys ``{"node", "round",
   "tools"}``. ``node`` is LangGraph's own update key, ``round``
   is the state's ``round_number`` as of that update, and ``tools`` is the
   list of results that ROUND produced — ``[]`` for every node that executed
   nothing, and one ``{"tool", "ok", "error_code"}`` object per call for
   ``execute_tools``. A list rather than the plan's singular "the tool name
   and its ok/error code" because ``MAX_CALLS_PER_ROUND = 3``: a round can
   execute three calls, and a single ``tool`` key could only name one of them.
   The key set is FIXED rather than varying by node so a client parses one
   shape; no arguments, no payload, no entities and no history appear in it,
   which is what ``test_a_step_carries_no_arguments_no_payload_and_no_entities``
   reads the frames for.
2. A ``token`` event's data is ``{"text": <chunk>}``.
3. The terminal ``reply`` event's data is exactly the body ``/chat`` answers a
   finished or paused turn with (19a decision E1):
   ``{"reply", "outcome", "citations", "request_id", "decision"}``, whose
   ``decision`` is the turn's decision card or ``null``. One shape
   for both routes, so a streaming client and a non-streaming one read the
   same turn.
4. An in-stream failure is ONE ``error`` event whose data is the body ``/chat``
   answers a failed turn with — ``{"code": "INTERNAL"}``,
   ``ErrorCode.INTERNAL`` (decision E1) — followed by ``done``, with no
   ``token`` event and no ``reply`` event at all.

Cost and isolation. The compose Postgres only: no Redis, no Qdrant, no gateway
socket (an ``httpx.MockTransport``) and no MCP server (the scripted stub of
17b). ONE session-scoped fixture builds the throwaway database, its schema,
one obviously fake tenant and LangGraph's checkpoint tables, once for the whole
module; per-case isolation is a namespace — a fresh
``session_id``, which IS the checkpoint thread id — never a new database, a new
pool or a flushed table. A pool and an engine are opened per ``asyncio.run``
because each case drives its own event loop and a connection pool belongs to
the loop it was opened in, exactly as ``test_agent_approval_interrupt.py``
already does. Fixtures live in this module rather than a conftest, because a
second ``conftest`` under ``services/agent-api/tests`` would collide with
``packages/helpdesk/tests/conftest.py`` under the repo-wide ``mypy`` run.
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

from helpdesk.agent_api.agent.checkpointer import build_checkpointer, build_pool, psycopg_dsn
from helpdesk.agent_api.agent.checkpointer import setup_checkpointer as setup_checkpoint_tables
from helpdesk.agent_api.agent.events import ErrorEvent, ReplyEvent, StepEvent
from helpdesk.agent_api.agent.follow_up import CANNOT_PLACE_REPLY
from helpdesk.agent_api.agent.graph import TurnDeps, stream_turn
from helpdesk.agent_api.agent.nodes.await_approval import PENDING_APPROVAL_REPLY
from helpdesk.agent_api.agent.state import TurnOutcome
from helpdesk.agent_api.agent.tool_args import SEARCH_POLICY
from helpdesk.agent_api.api.sse import (
    DONE,
    ERROR,
    REPLY,
    REPLY_CHUNK_CHARS,
    STEP,
    TOKEN,
    chunk_reply,
    encode_event,
    encode_turn_event,
)
from helpdesk.agent_api.services.mcp_client import RemotePolicyBlockedError, ToolCaller
from helpdesk.agent_api.services.model_gateway.client import ModelClient
from helpdesk.agent_api.services.model_gateway.strict_schema import strict_schema_problems
from helpdesk.shared.core.config import load_settings
from helpdesk.shared.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.shared.core.errors import ErrorCode
from helpdesk.shared.crud import Repository
from helpdesk.shared.crud.messages import append_message
from helpdesk.shared.crud.sessions import get_or_create_session
from helpdesk.shared.models import Base, Department, Message, Role, System, Tenant, User
from helpdesk.shared.models.conversation import MessageRole

NOW = datetime(2026, 6, 2, 10, 15, tzinfo=UTC)

GATEWAY_URL = "http://gateway.invalid:8080"

TENANT_SLUG = "stream-tenant"

# Obviously fake, tests only: this is not a credential.
API_KEYS: dict[str, str] = {TENANT_SLUG: f"fake-key-{TENANT_SLUG}"}

CLASSIFY = "classify_access_request"
CREATE_REQUEST = "create_access_request"

REQUEST_ID = uuid.UUID("66666666-6666-5666-8666-666666666666")
"""The request id a paused turn is waiting on, obviously fake."""

MESSAGE = "Please give me read access to fake payroll."

COMPOSED_REPLY = (
    "I have filed the fake payroll read request for you, and the fake access "
    "policy your fake company publishes is what it was judged against."
)
"""One composed reply, deliberately longer than `REPLY_CHUNK_CHARS`.

137 characters, so the ordinary turn streams THREE `token` events rather than
one: an implementation that emitted the whole reply in a single frame would
otherwise satisfy the order case by accident.
"""

CONTRADICTING_REPLY = "Good news, your fake payroll access has been granted."
"""A composition claiming a decision word this turn never returned.

The turn below runs one `search_policy` retrieval and no tool through
`execute_tools`, so `postcheck.permitted_words` is EMPTY and `granted` is a
contradiction — 19b regenerates it once, and the whole point is
that not one character of it reaches the wire.
"""

SECOND_CONTRADICTING_REPLY = "To confirm: the fake payroll access is approved."
"""A second contradiction, which is what drives the turn to the fallback."""

CITED_DOCUMENT = "fake-access-policy"
CITATION_TEXT = "Obviously fake policy text about fake payroll access."
PLANTED_HISTORY_LINE = "A fake earlier question nobody should see on the wire."

_UNREACHABLE_MSG = (
    "Postgres is unreachable at the configured DATABASE_URL.\n"
    "Plan 19c stream tests require the compose stack: run `docker compose up -d` first.\n"
    "Original error: {error!r}"
)


# --- reading frames back off the wire -----------------------------------------


@dataclass(frozen=True)
class Frame:
    """One parsed SSE frame: its event name and its decoded data."""

    event: str
    data: dict[str, Any]


def _blocks(payload: str) -> list[list[str]]:
    """The stream split the way an SSE client splits it: on a blank line."""
    return [block.split("\n") for block in payload.split("\n\n") if block.strip()]


def parse_frames(payload: str) -> list[Frame]:
    """Every frame an SSE client would see, forged ones included.

    Deliberately naive, exactly as a browser is: a block is a frame, and a
    `data:` line inside a payload the encoder failed to escape becomes a frame
    of its own. That is what makes the forging case able to fail.
    """
    frames: list[Frame] = []
    for lines in _blocks(payload):
        event = ""
        data_lines: list[str] = []
        for line in lines:
            if line.startswith("event: "):
                event = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data_lines.append(line.removeprefix("data: "))
        data: dict[str, Any] = {}
        if data_lines:
            loaded = json.loads("\n".join(data_lines))
            assert isinstance(loaded, dict), f"a frame's data is not an object: {loaded!r}"
            data = loaded
        frames.append(Frame(event=event, data=data))
    return frames


def _names(frames: Sequence[Frame]) -> list[str]:
    return [frame.event for frame in frames]


def _of(frames: Sequence[Frame], event: str) -> list[Frame]:
    return [frame for frame in frames if frame.event == event]


def _streamed_text(frames: Sequence[Frame]) -> str:
    """Every `token` event's text, in order, joined back into one reply."""
    return "".join(str(frame.data["text"]) for frame in _of(frames, TOKEN))


def _raw(frames: Sequence[Frame]) -> str:
    """Every frame's data as one string, for "this never appears" assertions."""
    return json.dumps([frame.data for frame in frames], sort_keys=True)


# --- the encoder, with no app, no graph and no database -----------------------


def test_a_reply_of_exactly_sixty_four_characters_is_one_token_event() -> None:
    """Catches a chunker that splits at 63, or one whose `range` step is off by
    one and emits an empty trailing chunk at an exact multiple: at exactly the
    cap the reply is ONE frame, and its text is the whole reply.

    The constant is asserted as the literal 64 beside the
    behaviour, so widening the chunk without moving this line is red.
    """
    assert REPLY_CHUNK_CHARS == 64

    reply = "a" * REPLY_CHUNK_CHARS
    chunks = chunk_reply(reply)

    assert chunks == (reply,)
    frames = parse_frames("".join(encode_event(TOKEN, {"text": chunk}) for chunk in chunks))
    assert _names(frames) == [TOKEN]
    assert _streamed_text(frames) == reply


def test_a_reply_of_sixty_five_characters_is_two() -> None:
    """Catches a chunker that cuts on a boundary other than the one the
    constant names — a word-boundary or sentence splitter would put 65 `a`s in
    one frame, and a 1-based slice would put 63 characters in the first.

    The other side of the same boundary, and the concatenation is asserted so a
    chunker that dropped or duplicated the boundary character is red rather
    than merely differently shaped.
    """
    reply = "a" * (REPLY_CHUNK_CHARS + 1)
    chunks = chunk_reply(reply)

    assert [len(chunk) for chunk in chunks] == [REPLY_CHUNK_CHARS, 1]
    assert "".join(chunks) == reply


FORGED_FRAME = "\n\nevent: reply\ndata: {}"
"""The fourth requirement, verbatim: a reply that tries to write its own frame."""


def test_a_reply_containing_a_frame_boundary_forges_no_event() -> None:
    """Catches an encoder that writes the reply text into the frame raw (an
    f-string, or a `data: ` per line): the text below closes the frame and
    opens a `reply` event of its own, so a client would see two reply events
    and act on a body the turn never produced.

    Both halves of the criterion, because the text travels twice — once cut
    into `token` events and once inside the terminal `reply` event — and an
    encoder could escape one and not the other.
    """
    reply = f"Your fake request is filed.{FORGED_FRAME} and the rest of the sentence."

    tokens = "".join(encode_event(TOKEN, {"text": chunk}) for chunk in chunk_reply(reply))
    token_frames = parse_frames(tokens)
    assert set(_names(token_frames)) == {TOKEN}
    assert _of(token_frames, REPLY) == []
    assert _streamed_text(token_frames) == reply

    final = parse_frames(
        encode_event(
            REPLY, {"reply": reply, "outcome": "done", "citations": [], "request_id": None}
        )
    )
    assert _names(final) == [REPLY]
    assert final[0].data["reply"] == reply


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("a newline", {"text": "first fake line\nsecond fake line"}),
        ("a crlf", {"text": "first fake line\r\nsecond fake line"}),
        ("a lone carriage return", {"text": "first fake line\rsecond fake line"}),
        ("a line separator", {"text": "first fake line\u2028second fake line"}),
        ("a forged frame", {"text": FORGED_FRAME}),
        ("no payload at all", {}),
        ("a nested object", {"node": "execute_tools", "round": 2, "tools": [{"tool": CLASSIFY}]}),
        ("non-ascii", {"text": "fake ümlaut and a fake emoji 🙂"}),
    ],
)
def test_every_frame_carries_its_data_on_one_line(label: str, payload: dict[str, Any]) -> None:
    """Catches an encoder that pretty-prints its JSON (`indent=2`) or that
    lets a raw newline through: every one of these payloads splits a frame in
    two on the wire, and the second half is either dropped by the client or
    read as a forged event.

    Asserted on the frame's own text — exactly two lines and one of them the
    data line — rather than on a re-parse, because a parser this file wrote
    could be as forgiving as the encoder is wrong.
    """
    frame = encode_event(STEP, payload)

    assert frame.endswith("\n\n"), f"{label}: a frame is terminated by a blank line"
    lines = frame.removesuffix("\n\n").split("\n")
    assert lines[0] == f"event: {STEP}", label
    assert len(lines) == 2, f"{label}: the data must be ONE line, got {lines[1:]!r}"
    assert lines[1].startswith("data: "), label
    assert json.loads(lines[1].removeprefix("data: ")) == payload, label


# --- the encoder, with no app, no graph and no database -----------------------


def test_a_step_event_encodes_to_one_step_frame() -> None:
    """Catches an `encode_turn_event` that emits zero frames, more than one
    frame, or a frame with the wrong event name for a `StepEvent`.
    """
    event = StepEvent(node="load_session", round=0, tools=[])
    frames = parse_frames("".join(encode_turn_event(event)))
    assert len(frames) == 1
    assert frames[0].event == STEP
    assert frames[0].data == {"node": "load_session", "round": 0, "tools": []}


def test_an_error_event_encodes_to_one_error_frame_then_done() -> None:
    """Catches an encoder that omits the trailing `done` frame on error, emits
    it before the error frame, or uses a key other than `code` for the error
    code.
    """
    event = ErrorEvent(code=ErrorCode.INTERNAL)
    frames = parse_frames("".join(encode_turn_event(event)))
    assert len(frames) == 2
    assert frames[0].event == ERROR
    assert frames[0].data == {"code": ErrorCode.INTERNAL.value}
    assert frames[1].event == DONE


def test_a_reply_event_encodes_to_token_frames_then_a_reply_frame_then_done() -> None:
    """Catches an encoder that emits the reply in one token frame instead of
    chunking it, drops the trailing `done` frame, or puts the `reply` frame
    before the token frames.
    """
    reply_data: dict[str, Any] = {
        "reply": COMPOSED_REPLY,
        "outcome": "done",
        "citations": [],
        "request_id": None,
        "decision": None,
    }
    event = ReplyEvent(reply=COMPOSED_REPLY, reply_data=reply_data)
    frames = parse_frames("".join(encode_turn_event(event)))

    token_count = len(chunk_reply(COMPOSED_REPLY))
    assert token_count == 3, (
        "premise: COMPOSED_REPLY must span three chunks at REPLY_CHUNK_CHARS=64"
    )
    assert [f.event for f in frames] == [TOKEN] * token_count + [REPLY, DONE]
    assert _streamed_text(frames) == COMPOSED_REPLY
    reply_frame = _of(frames, REPLY)[0]
    assert reply_frame.data == reply_data


# --- the throwaway database, its tenant and its checkpoint tables -------------


async def _admin_execute(admin_url: str, sql: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


@dataclass(frozen=True)
class StreamEnv:
    """One database, one obviously fake tenant, and the checkpointer's DSN."""

    database_url: str
    dsn: str
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    system_id: uuid.UUID
    role_id: uuid.UUID


async def _seed(factory: async_sessionmaker[AsyncSession]) -> tuple[uuid.UUID, ...]:
    """One fake tenant with one person, one system and one role."""
    async with factory() as session:
        tenant = Tenant(slug=TENANT_SLUG, name="Fake Stream Tenant", residency="eu", settings={})
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
def stream_env() -> Iterator[StreamEnv]:
    """The schema, one seeded tenant and the checkpoint tables, built once.

    Nothing per-case is built here. A throwaway database
    rather than the compose one for the same reason as `test_models.py` and
    `test_seed.py` assert on a database's full table list, and LangGraph's
    checkpoint tables are outside `Base.metadata` on purpose.
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

    name = f"helpdesk_stream_test_{uuid.uuid4().hex[:12]}"
    asyncio.run(_admin_execute(admin_url, f'CREATE DATABASE "{name}"'))
    url = make_url(admin_url).set(database=name).render_as_string(hide_password=False)

    async def prepare() -> StreamEnv:
        engine = create_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            tenant_id, user_id, system_id, role_id = await _seed(create_session_factory(engine))
        finally:
            await engine.dispose()
        return StreamEnv(
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
    assert name.startswith("helpdesk_stream_test_")
    asyncio.run(_admin_execute(admin_url, f'DROP DATABASE "{name}" WITH (FORCE)'))


def _with_pool[T](dsn: str, scenario: Callable[[Any], Awaitable[T]]) -> T:
    """Run one scenario over a pool opened and closed inside its own loop.

    Not a per-case backing service: the database, its schema
    and its tables are the session fixture's, and only the pool — which belongs
    to the event loop it was opened in — is per `asyncio.run`. The explicit
    close is what keeps this suite from leaking connections into the shared
    compose Postgres.
    """

    async def runner() -> T:
        pool = build_pool(dsn)
        async with pool:
            return await scenario(pool)

    return asyncio.run(runner())


# --- the staged gateway and the scripted caller -------------------------------


def _as_composed(answer: str, request: httpx.Request) -> str:
    """What the gateway sends for a staged string. A request for `ComposedReply`
    gets a plain staged reply as the shape, marked a question when it holds "?"
    (a writer that marks honestly); a string that is already JSON goes as it is."""
    body = json.loads(request.content)
    if body.get("response_format", {}).get("json_schema", {}).get("name") != "ComposedReply":
        return answer
    try:
        parsed = json.loads(answer)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        return answer
    return json.dumps({"text": answer, "asks_question": "?" in answer, "used_passages": [1]})


def _completion(answer: str, request: httpx.Request) -> httpx.Response:
    content = _as_composed(answer, request)
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )


class StagedGateway:
    """Answers staged in order; a request past the last one fails the test.

    That is how "the stream never composed" and "no attempt beyond the scripted
    ones was made" are asserted as hard stops rather than as counts read
    afterwards. `fail_after` turns the transport into a dead gateway from that
    request onward, which is the failure no node can turn into a real reply.
    """

    def __init__(self, answers: Sequence[str], *, fail_after: int | None = None) -> None:
        self._answers = list(answers)
        self._fail_after = fail_after
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        schema = json.loads(request.content).get("response_format", {}).get("json_schema", {})
        if strict_schema_problems(schema.get("schema", {})):
            # As OpenAI answers a schema strict mode refuses, so a
            # non-strict schema cannot pass this suite unseen.
            return httpx.Response(400, json={"error": "the schema breaks strict mode"})
        index = len(self.requests) - 1
        if self._fail_after is not None and index >= self._fail_after:
            raise httpx.ConnectError("connection refused", request=request)
        if index >= len(self._answers):
            raise AssertionError(
                f"the turn made {len(self.requests)} model requests; "
                f"only {len(self._answers)} were staged"
            )
        return _completion(self._answers[index], request)

    @property
    def call_count(self) -> int:
        return len(self.requests)


class ScriptedToolCaller:
    """A `ToolCaller` answering one scripted payload per call, in order.

    An answer may be an exception, which is raised instead: that is how a round
    with a typed tool failure is driven without an MCP server. `entered` and
    `exited` are counted separately, so a stream that opened the turn's session
    and never closed it is a red case rather than an invisible leak.
    """

    def __init__(self, *answers: Mapping[str, Any] | BaseException) -> None:
        self._answers = list(answers)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> ToolCaller:
        self.entered += 1
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.exited += 1

    async def call_tool(self, name: str, arguments: BaseModel) -> Mapping[str, Any]:
        self.calls.append((name, arguments.model_dump(mode="json")))
        index = len(self.calls) - 1
        if index >= len(self._answers):
            raise AssertionError(
                f"the turn made {len(self.calls)} tool calls ({self.names}); "
                f"only {len(self._answers)} were scripted"
            )
        answer = self._answers[index]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    @property
    def names(self) -> list[str]:
        return [name for name, _arguments in self.calls]


class UnreachableToolCaller:
    """A caller whose handshake fails, exactly as `McpToolCaller.__aenter__` does."""

    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> ToolCaller:
        self.entered += 1
        raise RemotePolicyBlockedError("the handshake never completed")

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.exited += 1

    async def call_tool(self, name: str, arguments: BaseModel) -> Mapping[str, Any]:
        raise AssertionError("a caller whose handshake failed must never be called")


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
                "text": CITATION_TEXT,
                "score": 0.42,
            }
        ],
        "refused": False,
    }


def _approval_payload() -> dict[str, Any]:
    """A `create_access_request` answer that went to an approval chain."""
    return {
        "request_id": str(REQUEST_ID),
        "decision": "approval",
        "status": "pending",
        "required_approvers": ["manager", "system_owner"],
    }


def _classification_payload() -> dict[str, Any]:
    """A `classify_access_request` answer: a read, and no request filed."""
    return {"decision": "approval", "risk": "medium", "required_approvers": ["manager"]}


def _extraction_answer() -> str:
    return json.dumps(
        {
            "intent": "request_access",
            "entities": {
                "system": "payroll",
                "role": "payroll-read",
                "target_user": None,
                "department": None,
                "expiry_days": 30,
                "justification": "Obviously fake reason.",
            },
            "confidence": 0.95,
            "clarifying_question": None,
            "second_request": None,
        }
    )


EMPTY_PLAN = json.dumps({"calls": []})
"""A planning round proposing nothing: the ordinary end of a turn."""


def _classify_plan(env: StreamEnv) -> str:
    """One planning round proposing a single read-only classification."""
    return json.dumps(
        {
            "calls": [
                {
                    "tool": CLASSIFY,
                    "arguments": json.dumps(
                        {
                            "target_user_id": str(env.user_id),
                            "role_id": str(env.role_id),
                            "requested_expiry_days": None,
                        }
                    ),
                }
            ]
        }
    )


def _request_plan(env: StreamEnv) -> str:
    """One planning round proposing exactly the request a pause waits on."""
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


# --- driving the generator ----------------------------------------------------


YES = "Yes, that is right."
"""The second message every access request now needs."""


def _stream(
    env: StreamEnv,
    *,
    session_id: uuid.UUID,
    gateway: StagedGateway,
    tools: ScriptedToolCaller | UnreachableToolCaller,
    message: str = MESSAGE,
    plant: Sequence[tuple[MessageRole, str]] = (),
) -> list[Frame]:
    """Consume `stream_turn` to exhaustion and parse what it put on the wire.

    No app and no route: the frames here are the generator's own, so a case
    that fails cannot be blamed on FastAPI's response machinery.
    """

    async def scenario(pool: Any) -> list[Frame]:
        checkpointer = build_checkpointer(pool)
        engine = create_engine(env.database_url)
        try:
            factory = create_session_factory(engine)
            if plant:
                async with tenant_session(factory, env.tenant_id) as db:
                    await get_or_create_session(
                        db, session_id=session_id, user_id=env.user_id, now=NOW
                    )
                    for role, content in plant:
                        await append_message(db, session_id=session_id, role=role, content=content)
            client = ModelClient(
                base_url=GATEWAY_URL,
                api_keys=API_KEYS,
                transport=httpx.MockTransport(gateway.handler),
            )
            try:
                async with tenant_session(factory, env.tenant_id) as db:
                    deps = TurnDeps(db=db, model=client, now=lambda: NOW, tools=tools)
                    all_frames: list[str] = []
                    async for event in stream_turn(
                        deps=deps,
                        tenant_id=env.tenant_id,
                        user_id=env.user_id,
                        session_id=session_id,
                        tenant_slug=TENANT_SLUG,
                        message=message,
                        checkpointer=checkpointer,
                    ):
                        all_frames.extend(encode_turn_event(event))
            finally:
                await client.aclose()
        finally:
            await engine.dispose()
        return parse_frames("".join(all_frames))

    return _with_pool(env.dsn, scenario)


def _stored_messages(env: StreamEnv, session_id: uuid.UUID) -> list[tuple[str, str]]:
    """Every message row of one conversation, in `ordinal` order."""

    async def scenario() -> list[tuple[str, str]]:
        engine = create_engine(env.database_url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, env.tenant_id) as db:
                rows = await Repository(db, Message).list()
                stored = sorted(
                    (row for row in rows if row.session_id == session_id),
                    key=lambda row: row.ordinal,
                )
                return [(str(row.role), row.content) for row in stored]
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


# --- the order, the vocabulary and what a step carries ------------------------


def test_the_events_are_steps_then_tokens_then_reply_then_done(stream_env: StreamEnv) -> None:
    """Catches a stream that interleaves its events — tokens emitted as the
    nodes run, or the `reply` event written before the text it summarises —
    and one that never terminates with `done`, which is the only frame telling
    a client the turn is over rather than the socket merely quiet.

    The reply is longer than `REPLY_CHUNK_CHARS`, so "then tokens" is three
    frames and not one; the whole name list is asserted as a literal sequence,
    which no ordering other than the contract's can satisfy.
    """
    session_id = uuid.uuid4()
    gateway = StagedGateway([_extraction_answer(), EMPTY_PLAN, COMPOSED_REPLY])
    tools = ScriptedToolCaller(_page())

    frames = _stream(stream_env, session_id=session_id, gateway=gateway, tools=tools)

    token_count = len(chunk_reply(COMPOSED_REPLY))
    assert token_count == 3, "premise: the composed reply must span several chunks"
    assert _names(frames) == [STEP] * 7 + [TOKEN] * token_count + [REPLY, DONE]
    assert _streamed_text(frames) == COMPOSED_REPLY

    final = _of(frames, REPLY)[0]
    assert set(final.data) == {"reply", "outcome", "citations", "request_id", "decision"}
    assert final.data["reply"] == COMPOSED_REPLY
    assert final.data["outcome"] == TurnOutcome.DONE.value
    assert [citation["document"] for citation in final.data["citations"]] == [CITED_DOCUMENT]
    assert final.data["request_id"] is None
    assert _of(frames, ERROR) == []
    assert tools.names == [SEARCH_POLICY]


def test_the_step_names_are_the_graphs_node_names(stream_env: StreamEnv) -> None:
    """Catches a stream that invents its own step vocabulary (a "thinking" or
    "working" label), and — the plan's second risk — a rename of a node in
    `build.py` silently changing the wire words a UI is written against.

    The names are written out as literals rather than imported from
    `build.py`'s constants: importing them would make this case agree with any
    rename, which is the exact drift it exists to catch.
    """
    session_id = uuid.uuid4()
    gateway = StagedGateway([_extraction_answer(), EMPTY_PLAN, COMPOSED_REPLY])
    tools = ScriptedToolCaller(_page())

    frames = _stream(stream_env, session_id=session_id, gateway=gateway, tools=tools)

    assert [str(frame.data["node"]) for frame in _of(frames, STEP)] == [
        "load_session",
        "extract_intent",
        "resolve_entities",
        "retrieve_policy",
        "plan_tools",
        "validate_tool_calls",
        "compose_reply",
    ]


def test_a_step_carries_no_arguments_no_payload_and_no_entities(stream_env: StreamEnv) -> None:
    """Catches decision C2 shipped by accident — a step built from the node's
    whole state update, which puts the tool arguments, the tool payload, the
    resolved directory ids and the loaded history on the wire.

    Two assertions, because either alone is escapable: the exact key set (a
    step with a fourth key fails), and a substring sweep over the frames for
    the four things that must not be there, each planted so it EXISTS in this
    turn's state — the history line, the citation text, the resolved role id
    and the user's own message. A step payload carrying the state update would
    contain all four.
    """
    session_id = uuid.uuid4()
    gateway = StagedGateway(
        [_extraction_answer(), _classify_plan(stream_env), EMPTY_PLAN, COMPOSED_REPLY]
    )
    tools = ScriptedToolCaller(_page(), _classification_payload())

    frames = _stream(
        stream_env,
        session_id=session_id,
        gateway=gateway,
        tools=tools,
        plant=[(MessageRole.USER, PLANTED_HISTORY_LINE)],
    )

    steps = _of(frames, STEP)
    assert steps, "the turn emitted no step at all"
    for step in steps:
        assert set(step.data) == {"node", "round", "tools"}, step.data

    wire = _raw(steps)
    for forbidden in (
        PLANTED_HISTORY_LINE,
        CITATION_TEXT,
        str(stream_env.role_id),
        MESSAGE,
        "medium",
    ):
        assert forbidden not in wire, f"a step put {forbidden!r} on the wire"


def test_an_execution_step_carries_the_tool_name_and_its_error_code(
    stream_env: StreamEnv,
) -> None:
    """Catches an execution step that reports the turn's ACCUMULATED results
    rather than the round's own — two rounds each execute exactly one call, so
    an accumulating implementation puts two tools on the second step — and one
    that drops the typed code, leaving a UI unable to tell a refused call from
    a successful one.

    The failing call answers `POLICY_BLOCKED` rather than `INTERNAL` on
    purpose: `INTERNAL` ends the turn, so the turn would never
    reach the second round and the case would pin the ending instead of the
    step.
    """
    session_id = uuid.uuid4()
    gateway = StagedGateway(
        [
            _extraction_answer(),
            _classify_plan(stream_env),
            _request_plan(stream_env),
            EMPTY_PLAN,
            COMPOSED_REPLY,
        ]
    )
    tools = ScriptedToolCaller(
        _page(),
        _classification_payload(),
        RemotePolicyBlockedError("the fake policy engine refused this fake request"),
    )

    # The request turn classifies and asks; the yes runs the create.
    asked = _stream(stream_env, session_id=session_id, gateway=gateway, tools=tools)
    frames = _stream(stream_env, session_id=session_id, gateway=gateway, tools=tools, message=YES)

    asked_executions = [s for s in _of(asked, STEP) if s.data["node"] == "execute_tools"]
    assert [step.data["round"] for step in asked_executions] == [1]
    assert asked_executions[0].data["tools"] == [
        {"tool": CLASSIFY, "ok": True, "error_code": None},
    ]
    executions = [frame for frame in _of(frames, STEP) if frame.data["node"] == "execute_tools"]
    assert len(executions) == 1
    assert executions[0].data["tools"] == [
        {"tool": CREATE_REQUEST, "ok": False, "error_code": ErrorCode.POLICY_BLOCKED.value},
    ]
    assert tools.names == [SEARCH_POLICY, CLASSIFY, CREATE_REQUEST]


# --- what may be streamed, and when -------------------------------------------


def test_no_token_is_emitted_before_the_composition_returns(stream_env: StreamEnv) -> None:
    """Catches decision B2 shipped by accident — tokens forwarded from the
    gateway as they arrive — which would put a sentence claiming a grant that
    never happened on the wire and then correct it.

    The first composition claims `granted` over a turn that returned no tool
    result at all, so 19b's post-check regenerates it; the assertion is that
    not one character of that first text appears in ANY frame, checked over the
    raw frame data rather than over the token events alone, so a stream leaking
    it as a `step` or `reply` field is caught too.
    """
    session_id = uuid.uuid4()
    gateway = StagedGateway([_extraction_answer(), EMPTY_PLAN, CONTRADICTING_REPLY, COMPOSED_REPLY])
    tools = ScriptedToolCaller(_page())

    frames = _stream(stream_env, session_id=session_id, gateway=gateway, tools=tools)

    assert _streamed_text(frames) == COMPOSED_REPLY
    assert "granted" not in _raw(frames)
    assert CONTRADICTING_REPLY not in _raw(frames)
    assert _of(frames, REPLY)[0].data["reply"] == COMPOSED_REPLY
    assert gateway.call_count == 4, "premise: the composition must have run twice"


def test_a_fallback_streams_the_template_and_neither_attempt(stream_env: StreamEnv) -> None:
    """Catches a stream that reaches for the model's last answer when the
    post-check refused both: what a twice-contradicting turn that ran no
    tool streams is `CANNOT_PLACE_REPLY` and nothing else (an earlier change: it ran no
    tool, so there is no result to build the sentence from), because both
    attempts made a claim the turn cannot support.

    The two attempts use DIFFERENT decision words, so a stream that emitted
    either one is caught by name rather than by a shared prefix.
    """
    session_id = uuid.uuid4()
    gateway = StagedGateway(
        [
            _extraction_answer(),
            EMPTY_PLAN,
            CONTRADICTING_REPLY,
            SECOND_CONTRADICTING_REPLY,
        ]
    )
    tools = ScriptedToolCaller(_page())

    frames = _stream(stream_env, session_id=session_id, gateway=gateway, tools=tools)

    assert _streamed_text(frames) == CANNOT_PLACE_REPLY
    wire = _raw(frames)
    assert CONTRADICTING_REPLY not in wire
    assert SECOND_CONTRADICTING_REPLY not in wire
    assert "granted" not in wire
    assert "approved" not in wire
    assert _of(frames, REPLY)[0].data["reply"] == CANNOT_PLACE_REPLY


# --- the paused turn ----------------------------------------------------------


def test_a_paused_turn_streams_its_pending_reply_and_the_request_id(
    stream_env: StreamEnv,
) -> None:
    """Catches a stream that treats a pause as an unfinished turn — no reply
    event, or a `done` with nothing before it — and one that RESUMES the run to
    find something to say: no composition answer is staged, so a stream that
    reached `compose_reply` asks for an answer nobody staged and fails outright.

    The tokens and the final event are compared against each other as well as
    against the template, which catches a stream that announces one reply and
    streams a different one.
    """
    session_id = uuid.uuid4()
    gateway = StagedGateway([_extraction_answer(), _request_plan(stream_env)])
    tools = ScriptedToolCaller(_page(), _approval_payload())

    _stream(stream_env, session_id=session_id, gateway=gateway, tools=tools)
    frames = _stream(stream_env, session_id=session_id, gateway=gateway, tools=tools, message=YES)

    final = _of(frames, REPLY)
    assert len(final) == 1
    assert final[0].data["outcome"] == TurnOutcome.AWAIT_APPROVAL.value
    assert final[0].data["request_id"] == str(REQUEST_ID)

    streamed = _streamed_text(frames)
    assert streamed.startswith(PENDING_APPROVAL_REPLY.split("{")[0])
    assert streamed == final[0].data["reply"]
    assert _names(frames)[-1] == DONE
    assert _of(frames, ERROR) == []
    assert "compose_reply" not in [str(step.data["node"]) for step in _of(frames, STEP)]


def test_a_paused_turn_stores_exactly_one_assistant_row(stream_env: StreamEnv) -> None:
    """Catches a `stream_turn` that stores the pending reply itself on top of
    the one `run_turn` already writes on the interrupt branch, and one that
    stores a row per `token` event: the conversation is read back, and a paused
    turn owes exactly the user's message and one assistant row.

    The row's content is the streamed text, so a second row carrying the same
    words is caught by the COUNT rather than by the content.
    """
    session_id = uuid.uuid4()
    gateway = StagedGateway([_extraction_answer(), _request_plan(stream_env)])
    tools = ScriptedToolCaller(_page(), _approval_payload())

    _stream(stream_env, session_id=session_id, gateway=gateway, tools=tools)
    frames = _stream(stream_env, session_id=session_id, gateway=gateway, tools=tools, message=YES)
    stored = _stored_messages(stream_env, session_id)

    # The request and its question come first, then the yes and the pause.
    assert [role for role, _content in stored] == [
        MessageRole.USER.value,
        MessageRole.ASSISTANT.value,
        MessageRole.USER.value,
        MessageRole.ASSISTANT.value,
    ]
    assert stored[0][1] == MESSAGE
    assert stored[1][1].startswith("I'll file this:")
    assert stored[2][1] == YES
    assert stored[3][1] == _streamed_text(frames)


# --- a failure after the first byte -------------------------------------------


def test_a_gateway_failure_mid_stream_is_one_typed_error_event_then_done(
    stream_env: StreamEnv,
) -> None:
    """Catches decision E2 shipped by accident (an aborted connection, which a
    client cannot tell from a dropped one) and, worse, a stream that renders a
    FAILED turn's templated failure sentence as ordinary `token` events — prose
    about a turn nothing decided, which the contract calls out as "never a partial reply".

    The gateway answers the first two calls and then refuses the dial, so the
    failure lands AFTER steps are already on the wire, which is the only case
    where a typed event rather than a typed status is the answer.
    """
    session_id = uuid.uuid4()
    gateway = StagedGateway([_extraction_answer(), EMPTY_PLAN], fail_after=2)
    tools = ScriptedToolCaller(_page())

    frames = _stream(stream_env, session_id=session_id, gateway=gateway, tools=tools)

    errors = _of(frames, ERROR)
    assert len(errors) == 1
    assert errors[0].data == {"code": ErrorCode.INTERNAL.value}
    assert _names(frames)[-2:] == [ERROR, DONE]
    assert _of(frames, TOKEN) == []
    assert _of(frames, REPLY) == []
    assert _of(frames, STEP), "the failure must land after steps are already on the wire"


def test_the_turns_session_is_closed_when_the_stream_ends_on_a_failure(
    stream_env: StreamEnv,
) -> None:
    """Catches the plan's first risk: `async with deps.tools` now lives inside
    a response body, so a generator that returns out of the failure branch from
    INSIDE that block — or that yields its `error` event from an `except` which
    swallows the context — leaves the turn's MCP session open for the life of
    the process.

    Asserted on the caller's own entry and exit counts, which only the exit
    running can satisfy; the `error` and `done` frames are asserted alongside
    so a stream that never failed at all cannot pass. The DATABASE session is
    the ROUTE's to open and close (`stream_turn` takes it on `deps`), so its
    half is pinned in `test_chat_stream.py`, not here.
    """
    session_id = uuid.uuid4()
    gateway = StagedGateway([_extraction_answer(), EMPTY_PLAN], fail_after=2)
    tools = ScriptedToolCaller(_page())

    frames = _stream(stream_env, session_id=session_id, gateway=gateway, tools=tools)

    assert _names(frames)[-2:] == [ERROR, DONE]
    assert tools.entered == 1
    assert tools.exited == 1
