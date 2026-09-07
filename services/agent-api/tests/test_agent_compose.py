"""An earlier change §6: the composed reply, the node that writes it and its refusals.

Expected implementation surface (the implementer must provide exactly this):

``agent_api.graph.nodes.compose_reply``
    ``COMPOSITION_PROMPT = "reply_composition"`` — the prompt file shipped as
        ``services/agent-api/prompts/reply_composition.md``.
    ``async def compose_reply(state: TurnState, deps: TurnDeps) -> TurnState``
        — exactly ONE ``deps.model.complete`` call (decision E: plain
        ``complete``, no ``response_format``) under
        ``ModelAlias.AGENT_DEFAULT`` (decision D), handed the system prompt and
        the composition prompt as the only ``system`` messages and everything
        the turn gathered as ``user`` messages (the design): the intent and
        entities, the resolved directory ids, the citations, the tool results
        and each failure's sentence from ``PLAIN_ERRORS``. It adds the answer's
        ``total_tokens`` to ``tokens_used``, puts the reply on the state and
        writes exactly ONE assistant message. It never raises: a gateway
        failure ends the turn as state (``TurnOutcome.FAILED``, the typed
        ``error_code``, the templated ``FAILURE_REPLY``) with no composed text
        anywhere, exactly as ``plan_tools`` does.

Cost: nothing. No Postgres, no Redis and no socket. The gateway is an
``httpx.MockTransport`` and the database is a recording session object bound to
a tenant the way ``tenant_session`` binds one, so the row this node writes is
observable without a server — and the node still has to go through the
repository helper to produce it.
"""

import asyncio
import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import pytest
from agent_api.graph.deps import TurnDeps
from agent_api.graph.nodes.compose_reply import COMPOSITION_PROMPT, compose_reply
from agent_api.graph.nodes.extract_intent import FAILURE_REPLY, SYSTEM_PROMPT
from agent_api.graph.plain_errors import PLAIN_ERRORS
from agent_api.graph.state import (
    Citation,
    Intent,
    IntentEntities,
    IntentExtraction,
    ResolvedEntities,
    ToolResult,
    TurnOutcome,
    TurnState,
)
from agent_api.mcp_client import ToolCaller
from agent_api.prompts import load_prompt
from helpdesk.core.db import _TENANT_INFO_KEY
from helpdesk.errors import ErrorCode
from helpdesk.gateway.client import ModelAlias, ModelClient
from helpdesk.models.conversation import MAX_MESSAGE_CHARS, MessageRole
from sqlalchemy.ext.asyncio import AsyncSession

NOW = datetime(2026, 6, 2, 10, 15, tzinfo=UTC)

GATEWAY_URL = "http://gateway.invalid:8080"
TENANT_SLUG = "compose-tenant"
# Obviously fake, tests only: this is not a credential.
API_KEYS: dict[str, str] = {TENANT_SLUG: f"fake-key-{TENANT_SLUG}"}

TENANT_ID = uuid.UUID("22222222-2222-5222-8222-222222222222")
USER_ID = uuid.UUID("33333333-3333-5333-8333-333333333333")
SESSION_ID = uuid.UUID("11111111-1111-5111-8111-111111111111")
SYSTEM_ID = uuid.UUID("66666666-6666-5666-8666-666666666666")
ROLE_ID = uuid.UUID("55555555-5555-5555-8555-555555555555")
CHUNK_ID = uuid.UUID("77777777-7777-5777-8777-777777777777")

MESSAGE = "I need access to fake payroll"
COMPOSED = "Your request for fake payroll read access is with your manager now."

COMPOSITION_TOKENS = 37
"""The composition's own bill, deliberately not the 2 the other cases in
`test_agent_turn.py` use, so "these tokens were added" cannot be satisfied by
any other call's count."""

CITATION_TEXT = "Obviously fake policy text about requesting payroll access."
PAYLOAD_MARKER = "fake-request-0001"


def _citation() -> Citation:
    return Citation(
        document="fake-access-policy",
        title="Fake Access Policy",
        version="v1",
        heading_path=("Fake Section", "Fake Subsection"),
        chunk_id=CHUNK_ID,
        text=CITATION_TEXT,
        score=0.81,
    )


class RecordingSession:
    """An `AsyncSession` stand-in that records the rows a node adds.

    Bound to a tenant exactly as `tenant_session` binds one, because
    `append_message` reads that binding through `bound_tenant_id` before it
    builds a row: a node that wrote through the repository helper works here
    unchanged, and one that reached past it for raw SQL does not.
    """

    def __init__(self) -> None:
        self.info: dict[str, Any] = {_TENANT_INFO_KEY: TENANT_ID}
        self.added: list[Any] = []

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        return None

    @property
    def messages(self) -> list[Any]:
        return [row for row in self.added if hasattr(row, "role") and hasattr(row, "content")]


class StagedGateway:
    """One staged answer, and a request past it fails the test.

    That is how "exactly one `complete` call" is asserted as a hard stop
    rather than as a count read afterwards: a node that retried, or that made
    a second structured call, fails at the moment of the second request.
    """

    def __init__(
        self, text: str = COMPOSED, *, status: int = 200, total_tokens: int = COMPOSITION_TOKENS
    ) -> None:
        self._text = text
        self._status = status
        self._total_tokens = total_tokens
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if len(self.requests) > 1:
            raise AssertionError(
                f"the node made {len(self.requests)} model requests; one was staged"
            )
        if self._status != 200:
            return httpx.Response(self._status, json={"error": "obviously fake gateway detail"})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": self._text}}],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": self._total_tokens - 1,
                    "total_tokens": self._total_tokens,
                },
            },
        )


def _state(
    *,
    citations: Sequence[Citation] = (),
    tool_results: Sequence[ToolResult] = (),
    tokens_used: int = 0,
) -> TurnState:
    """A turn standing where the last round said there was nothing left to do."""
    return TurnState(
        tenant_id=TENANT_ID,
        user_id=USER_ID,
        session_id=SESSION_ID,
        tenant_slug=TENANT_SLUG,
        user_message=MESSAGE,
        extraction=IntentExtraction(
            intent=Intent.REQUEST_ACCESS,
            entities=IntentEntities(system="payroll", role="payroll-read"),
            confidence=0.95,
        ),
        resolved=ResolvedEntities(
            system_id=SYSTEM_ID,
            system_slug="payroll",
            role_id=ROLE_ID,
            role_slug="payroll-read",
        ),
        outcome=TurnOutcome.DONE,
        citations=tuple(citations),
        tool_results=tuple(tool_results),
        tool_calls_made=len(tool_results),
        tokens_used=tokens_used,
        round_number=2,
        started_at=NOW,
    )


def _compose(state: TurnState, gateway: StagedGateway, db: RecordingSession) -> TurnState:
    """`compose_reply` alone: a staged gateway, a recording session, no graph.

    `tools` is `None` behind a cast — the composition executes nothing, so a
    node that reached for the MCP caller raises `AttributeError` here rather
    than making a tool call after the round was declared finished.
    """

    async def scenario() -> TurnState:
        client = ModelClient(
            base_url=GATEWAY_URL,
            api_keys=API_KEYS,
            transport=httpx.MockTransport(gateway.handler),
        )
        try:
            deps = TurnDeps(
                db=cast(AsyncSession, db),
                model=client,
                tools=cast(ToolCaller, None),
                now=lambda: NOW,
            )
            return await compose_reply(state, deps)
        finally:
            await client.aclose()

    return asyncio.run(scenario())


def _body(request: httpx.Request) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(request.content)
    return body


def _messages(request: httpx.Request) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = _body(request)["messages"]
    return messages


def _ok_result() -> ToolResult:
    return ToolResult(tool="create_access_request", ok=True, payload={"id": PAYLOAD_MARKER})


def _failed_result(code: ErrorCode = ErrorCode.SOD_CONFLICT) -> ToolResult:
    return ToolResult(tool="create_access_request", ok=False, error_code=code)


# --- criterion 7: one call, the right alias, the right shape -------------------


def test_the_composition_makes_exactly_one_call_under_the_default_alias() -> None:
    """Decisions D and E, and the only case that can see either: the call
    succeeds whichever alias is named and whatever the body asks for, and no
    state field records it.

    Catches a composition copied from `plan_tools` and left on
    `agent-strong` — paid on every turn that gets this far, for a call that
    writes down a decision the policy engine and the tools already made — and
    one wrapped in `complete_json`, which buys no structure for prose and
    hides the `ModelAnswer` whose `total_tokens` this plan needs."""
    gateway = StagedGateway()

    _compose(_state(citations=[_citation()]), gateway, RecordingSession())

    assert len(gateway.requests) == 1
    body = _body(gateway.requests[0])
    assert body["model"] == ModelAlias.AGENT_DEFAULT.value
    assert "response_format" not in body


def test_everything_the_turn_gathered_is_quoted_as_data_and_not_as_instructions() -> None:
    """the design, on the one call whose input includes a tool's own output and
    somebody else's policy text. Catches a composition that formats the
    citations, the tool results or the failure sentences into its system
    message: merged there they become instructions the model obeys, which is
    the prompt-injection path to a reply that contradicts the engine.

    Every system message must be a prompt file this repo ships, and each of
    the five kinds of material must be findable in a `user` message — the
    payload marker and the citation text are unique strings, so a composition
    that dropped either fails on that string alone."""
    gateway = StagedGateway()
    state = _state(citations=[_citation()], tool_results=[_ok_result(), _failed_result()])

    _compose(state, gateway, RecordingSession())

    messages = _messages(gateway.requests[0])
    shipped = {load_prompt(SYSTEM_PROMPT).text, load_prompt(COMPOSITION_PROMPT).text}
    system_content = [item["content"] for item in messages if item["role"] == "system"]
    user_content = " ".join(item["content"] for item in messages if item["role"] == "user")

    assert set(system_content) == shipped
    assert len(system_content) == 2
    assert CITATION_TEXT in user_content
    assert PAYLOAD_MARKER in user_content
    assert PLAIN_ERRORS[ErrorCode.SOD_CONFLICT] in user_content
    assert Intent.REQUEST_ACCESS.value in user_content
    assert str(SYSTEM_ID) in user_content
    assert MESSAGE in user_content


def test_a_failure_reaches_the_prompt_in_plain_words_and_never_as_its_code() -> None:
    """Criterion 6 where it is paid for. Catches a composition that hands the
    model the raw `error_code` or the tool caller's message: `SOD_CONFLICT`
    read back to a user is internal vocabulary, and the caller's message is
    the server's own text, which is exactly what may not cross into a chat
    window.

    The result carries no payload, so the sentence is the only thing that can
    tell the model what happened."""
    gateway = StagedGateway()

    _compose(
        _state(tool_results=[_failed_result(ErrorCode.POLICY_BLOCKED)]),
        gateway,
        RecordingSession(),
    )

    user_content = " ".join(
        item["content"] for item in _messages(gateway.requests[0]) if item["role"] == "user"
    )
    assert PLAIN_ERRORS[ErrorCode.POLICY_BLOCKED] in user_content


# --- criterion 7: exactly one assistant message, and the reply on the state ----


def test_the_composed_reply_is_stored_once_and_carried_on_the_state() -> None:
    """Catches a node that puts the reply on the state and leaves the storing
    to an ending node the graph no longer routes through — the user would see
    an answer that the next turn's history has no record of — and one that
    writes the row twice (once itself, once through a shared helper), which
    reads as the assistant answering the same question two ways.

    The stored content is asserted to be the MODEL's text, so a node that
    stored the templated reply and returned the composed one fails here."""
    db = RecordingSession()

    state = _compose(_state(citations=[_citation()]), StagedGateway(), db)

    assert state.reply == COMPOSED
    assert len(db.messages) == 1
    assert db.messages[0].content == COMPOSED
    assert db.messages[0].role == MessageRole.ASSISTANT.value
    assert db.messages[0].session_id == SESSION_ID
    assert db.messages[0].tenant_id == TENANT_ID


def test_the_compositions_own_tokens_are_added_to_the_turns_counter() -> None:
    """Criterion 8's last call site. Catches a composition that spends the
    gateway's tokens and reports none: the counter would understate every turn
    by the largest single call it makes, and a turn could pass
    `MAX_TURN_TOKENS` while having spent well past it.

    The state starts at a non-zero count, so a node that ASSIGNS its own usage
    rather than adding it fails this row — that implementation would throw the
    extraction's and the planner's spend away at the last node of the turn."""
    state = _compose(_state(tokens_used=1000), StagedGateway(), RecordingSession())

    assert state.tokens_used == 1000 + COMPOSITION_TOKENS


# --- the unhappy path ----------------------------------------------------------


def test_a_gateway_failure_ends_the_turn_and_leaves_no_partial_reply() -> None:
    """Criterion 9. Catches a `compose_reply` that lets the gateway's
    `ToolError` escape — `run_turn` would raise where every other failure
    comes back as state, so the route would have an exception to translate —
    and one that stores a half-composed answer before the call returned.

    No row at all is written here: the templated reply is `fail_turn`'s to
    store, and a node that wrote one as well would leave the turn with two
    assistant messages for one question."""
    db = RecordingSession()

    state = _compose(_state(citations=[_citation()]), StagedGateway(status=503), db)

    assert state.outcome is TurnOutcome.FAILED
    assert state.error_code is ErrorCode.INTERNAL
    assert state.reply == FAILURE_REPLY
    assert db.messages == []


# --- criterion 10: the stored reply against the column's own cap ---------------


def test_a_composed_reply_of_exactly_the_cap_is_stored() -> None:
    """The inclusive half of a project rule's boundary, on the one value this
    plan produces that is STORED. Catches an exclusive comparison that refuses
    the longest reply the `messages.content` column can hold — the turn would
    end on a templated failure after every tool call had already succeeded,
    which is the worst possible moment to lose the answer."""
    reply = "x" * MAX_MESSAGE_CHARS
    db = RecordingSession()

    state = _compose(_state(), StagedGateway(text=reply), db)

    assert len(reply) == MAX_MESSAGE_CHARS
    assert state.reply == reply
    assert len(db.messages) == 1
    assert len(db.messages[0].content) == MAX_MESSAGE_CHARS


def test_a_composed_reply_one_character_over_the_cap_writes_no_row_at_all() -> None:
    """The exclusive half, and the "before any effect" half: catches a node
    that hands the model's output straight to `append_message` and lets the
    COLUMN refuse it, which raises inside the turn's transaction after the row
    was added rather than answering as state.

    The turn ends the way every other model failure ends — the templated
    reply, a typed code, nothing composed — because a reply this long is
    output the user must not be shown either."""
    db = RecordingSession()

    state = _compose(_state(), StagedGateway(text="x" * (MAX_MESSAGE_CHARS + 1)), db)

    assert state.outcome is TurnOutcome.FAILED
    assert state.error_code is ErrorCode.VALIDATION
    assert state.reply == FAILURE_REPLY
    assert db.messages == []


def test_the_staged_gateway_refuses_a_second_request() -> None:
    """Guard against a vacuous fixture: "exactly one `complete` call" rests on
    the second request failing the test, so a handler that answered every
    request would make that claim pass whatever the node did."""
    gateway = StagedGateway()
    request = httpx.Request("POST", f"{GATEWAY_URL}/chat/completions")

    gateway.handler(request)

    with pytest.raises(AssertionError):
        gateway.handler(request)
