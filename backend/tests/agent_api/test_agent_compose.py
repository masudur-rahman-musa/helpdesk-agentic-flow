"""The composed reply, the node that writes it and its refusals.

Expected implementation surface (the implementer must provide exactly this):

The writer answers in the `ComposedReply` shape (`text` and
`asks_question`) through `complete_json`, and the question checks read the mark.

``agent_api.graph.nodes.compose_reply``
    ``COMPOSITION_PROMPT = "reply_composition"`` — the prompt file shipped as
        ``services/agent-api/prompts/reply_composition.md``.
    ``async def compose_reply(state: TurnState, deps: TurnDeps) -> TurnState``
        — exactly ONE ``deps.model.complete_json`` call (``ComposedReply``,
        strict ``response_format``) under
        ``ModelAlias.AGENT_DEFAULT``, handed the system prompt and
        the composition prompt as the only ``system`` messages and everything
        the turn gathered as ``user`` messages: the intent and
        entities, the resolved directory ids, the citations, the tool results
        and each failure's sentence from ``PLAIN_ERRORS``. It adds the answer's
        ``total_tokens`` to ``tokens_used``, puts the reply on the state and
        writes exactly ONE assistant message. It never raises: a gateway
        failure ends the turn as state (``TurnOutcome.FAILED``, the typed
        ``error_code``, the templated ``FAILURE_REPLY``) with no composed text
        anywhere, exactly as ``plan_tools`` does.

A later change adds the decision-word post-check to the same node and the same
harness. Expected additional surface:

``agent_api.graph.nodes.compose_reply``
    ``COMPOSITION_ATTEMPTS = 2`` — the hard bound on compose attempts from this
        node, ever. Each attempt may retry output off the shape once, so a
        turn may cost up to four gateway calls on bad output.
    ``compose_reply`` composes, checks the answer against
        ``postcheck.contradicts``, regenerates ONCE with the permitted words
        quoted as a ``user`` data message, checks again and falls
        back to ``POSTCHECK_FALLBACK_REPLY``. Exactly one assistant row on
        every path that writes one, both calls' ``total_tokens`` added, and
        ``state.postcheck`` recording which of the three things it did.
        A regeneration that fails or answers past
        ``MAX_MESSAGE_CHARS`` ends the turn exactly as a first call would.

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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.agent_api.agent.errors import PLAIN_ERRORS
from helpdesk.agent_api.agent.nodes.compose_reply import (
    COMPOSITION_ATTEMPTS,
    COMPOSITION_PROMPT,
    compose_reply,
)
from helpdesk.agent_api.agent.nodes.extract_intent import FAILURE_REPLY, SYSTEM_PROMPT
from helpdesk.agent_api.agent.postcheck import (
    POSTCHECK_FALLBACK_REPLY,
    outcome_reply,
    permitted_words,
)
from helpdesk.agent_api.agent.prompts import load_prompt
from helpdesk.agent_api.agent.state import (
    Citation,
    ComposedReply,
    Intent,
    IntentEntities,
    IntentExtraction,
    PostCheckOutcome,
    ResolvedEntities,
    ToolResult,
    TurnOutcome,
    TurnState,
)
from helpdesk.agent_api.agent.turn_deps import TurnDeps
from helpdesk.agent_api.services.mcp_client import ToolCaller
from helpdesk.agent_api.services.model_gateway.client import ModelAlias, ModelClient
from helpdesk.agent_api.services.model_gateway.strict_schema import strict_schema_problems
from helpdesk.shared.core.db import _TENANT_INFO_KEY
from helpdesk.shared.core.errors import ErrorCode
from helpdesk.shared.models.access import RequestStatus
from helpdesk.shared.models.conversation import MAX_MESSAGE_CHARS, MessageRole

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

REGENERATION_TOKENS = 53
"""The regeneration's own bill, deliberately different from
`COMPOSITION_TOKENS` so "both calls were added" cannot be satisfied by an
implementation that counted either call twice
(`test_a_contradicting_reply_is_regenerated_once_and_only_the_second_is_stored`)."""

PENDING_REPLY = "Your request for fake payroll read access is pending with your manager."
"""A reply whose only decision word is one a `pending` request permits."""

CONTRADICTING_REPLY = "Your fake payroll read access has been approved."
"""A reply claiming `approved` over a turn whose only result says `pending`."""

SECOND_CONTRADICTING_REPLY = "Your fake payroll read access was blocked."
"""A different unpermitted word, so the fallback case cannot be passed by an
implementation that merely compares the two answers to each other."""

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


@dataclass(frozen=True)
class StagedAnswer:
    """One answer the gateway is prepared to give, and the bill for it."""

    text: str = COMPOSED
    status: int = 200
    total_tokens: int = COMPOSITION_TOKENS


def _as_composed(text: str, body: dict[str, Any]) -> str:
    """What the gateway sends for a staged string. A request for `ComposedReply`
    gets a plain staged reply as the shape, marked a question when it holds "?"
    (a writer that marks honestly); a string that is already JSON goes as it is."""
    name = body.get("response_format", {}).get("json_schema", {}).get("name")
    if name != "ComposedReply":
        return text
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        return text
    return json.dumps({"text": text, "asks_question": "?" in text})


def marked(text: str, *, asks: bool) -> str:
    """A staged answer in the shape, with the mark set by the test, not by "?"."""
    return json.dumps({"text": text, "asks_question": asks})


class StagedGateway:
    """A LIST of staged answers, and a request past the list fails the test.

    That is how a call bound is asserted as a hard stop rather than as a count
    read afterwards: a node that retried once more than the staging allows
    fails at the moment of the extra request, not in an assertion it might
    never reach. The post-check cases need the bound from both sides, so
    the default staging is still ONE answer — every 17d case keeps its
    "exactly one `complete` call" guarantee unchanged — and a case that
    expects a regeneration stages exactly two.

    Both halves of the fixture guard are pinned by
    `test_the_staged_gateway_refuses_a_second_request` and
    `test_a_two_answer_staging_refuses_a_third_request`.
    """

    def __init__(
        self,
        text: str = COMPOSED,
        *,
        status: int = 200,
        total_tokens: int = COMPOSITION_TOKENS,
        answers: Sequence[StagedAnswer] | None = None,
    ) -> None:
        self._answers: tuple[StagedAnswer, ...] = (
            tuple(answers)
            if answers is not None
            else (StagedAnswer(text=text, status=status, total_tokens=total_tokens),)
        )
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        # The two fixture-guard cases below send a bare request with no body.
        body = json.loads(request.content) if request.content else {}
        schema = body.get("response_format", {}).get("json_schema", {})
        if strict_schema_problems(schema.get("schema", {})):
            # As OpenAI answers a schema strict mode refuses, so a
            # non-strict schema cannot pass this suite unseen.
            return httpx.Response(400, json={"error": "the schema breaks strict mode"})
        if len(self.requests) > len(self._answers):
            raise AssertionError(
                f"the node made {len(self.requests)} model requests; "
                f"{len(self._answers)} were staged"
            )
        answer = self._answers[len(self.requests) - 1]
        if answer.status != 200:
            return httpx.Response(answer.status, json={"error": "obviously fake gateway detail"})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": _as_composed(answer.text, body)}}],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": answer.total_tokens - 1,
                    "total_tokens": answer.total_tokens,
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
            entities=IntentEntities(
                system="payroll",
                role="payroll-read",
                target_user=None,
                department=None,
                expiry_days=None,
                justification=None,
            ),
            confidence=0.95,
            clarifying_question=None,
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


# --- one call, the right alias, the right shape -------------------


def test_the_composition_makes_exactly_one_call_under_the_default_alias() -> None:
    """Decisions D and E, and the only case that can see either: the call
    succeeds whichever alias is named, and no state field records it.

    Catches a composition copied from `plan_tools` and left on
    `agent-strong` — paid on every turn that gets this far, for a call that
    writes down a decision the policy engine and the tools already made.

    The writer returns a structured answer, so the body asks for
    `ComposedReply`; this also catches a composition left on plain `complete`,
    where the mark would never be asked for."""
    gateway = StagedGateway()

    _compose(_state(citations=[_citation()]), gateway, RecordingSession())

    assert len(gateway.requests) == 1
    body = _body(gateway.requests[0])
    assert body["model"] == ModelAlias.AGENT_DEFAULT.value
    assert body["response_format"]["json_schema"]["name"] == "ComposedReply"


def test_everything_the_turn_gathered_is_quoted_as_data_and_not_as_instructions() -> None:
    """On the one call whose input includes a tool's own output and
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
    # The resolved rows reach the writer by name; their ids never do.
    assert '"system_slug": "payroll"' in user_content
    assert str(SYSTEM_ID) not in user_content
    assert MESSAGE in user_content


def test_a_failure_reaches_the_prompt_in_plain_words_and_never_as_its_code() -> None:
    """The call limit where it is paid for. Catches a composition that hands the
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


# --- exactly one assistant message, and the reply on the state ----


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
    """The last call site. Catches a composition that spends the
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
    """Catches a `compose_reply` that lets the gateway's
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


# --- the stored reply against the column's own cap ---------------


def test_a_composed_reply_of_exactly_the_cap_is_stored() -> None:
    """The inclusive half of the length limit, on the one value this
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


# --- the decision-word post-check on the composing node --------------


def _pending_result() -> ToolResult:
    """A filed request: the turn returned `pending` and nothing else.

    Read by every post-check case below, so the permitted set is
    `{"pending"}` — one word permitted and three not, which is what lets a
    single reply be judged either way without changing the turn.
    """
    return ToolResult(
        tool="create_access_request",
        ok=True,
        payload={"id": PAYLOAD_MARKER, "status": RequestStatus.PENDING.value},
    )


def _pending_state(*, tokens_used: int = 0) -> TurnState:
    return _state(tool_results=[_pending_result()], tokens_used=tokens_used)


def test_the_composition_bound_is_exactly_two_attempts() -> None:
    """The number that is the node's worst case: asserted
    as the value, because the cost of this node — latency and tokens — is
    multiplied by it on every turn that reaches composition.

    Catches a loop bound raised to 3 while every other case still passes: the
    two-contradiction case would simply spend a third call before falling
    back, and nothing else in this file could see it."""
    assert COMPOSITION_ATTEMPTS == 2


def test_a_permitted_reply_is_stored_unchanged_after_a_single_call() -> None:
    """The reply carries `pending`, which is exactly what this
    turn's one successful result returned, so the post-check has something to
    approve rather than merely nothing to object to.

    Catches a post-check that regenerates whenever the reply contains ANY
    decision word — the staged gateway refuses the second request, so that
    implementation fails here at the moment it makes it — and one that stores
    the template over a perfectly good answer. `postcheck` is `passed`, which
    catches a node that recorded an outcome only on the unhappy paths and left
    the metrics counting nothing on the ordinary one."""
    db = RecordingSession()
    state = _pending_state()
    gateway = StagedGateway(text=PENDING_REPLY)

    assert permitted_words(state) == frozenset({"pending"})

    result = _compose(state, gateway, db)

    assert len(gateway.requests) == 1
    assert result.reply == PENDING_REPLY
    assert result.postcheck is PostCheckOutcome.PASSED
    assert len(db.messages) == 1
    assert db.messages[0].content == PENDING_REPLY


def test_a_contradicting_reply_is_regenerated_once_and_only_the_second_is_stored() -> None:
    """The whole of it. The first answer claims `approved` over a
    turn whose only result says `pending`; the second says `pending`.

    Catches a node that stores the contradicting answer and regenerates
    afterwards — the user would have already been told their access was
    approved, which is the sentence this change exists to prevent, and the
    turn's history would carry two assistant messages for one question — and
    one that stores both. The stored content is asserted to be the SECOND
    reply and the first is asserted absent, so neither half can pass alone.

    `tokens_used` starts non-zero and is asserted as the sum of both calls,
    which catches a regeneration whose spend is dropped: the turn would
    understate itself by a whole gateway call and could pass `MAX_TURN_TOKENS`
    having spent well past it."""
    db = RecordingSession()
    gateway = StagedGateway(
        answers=[
            StagedAnswer(text=CONTRADICTING_REPLY, total_tokens=COMPOSITION_TOKENS),
            StagedAnswer(text=PENDING_REPLY, total_tokens=REGENERATION_TOKENS),
        ]
    )

    result = _compose(_pending_state(tokens_used=1000), gateway, db)

    assert len(gateway.requests) == 2
    assert result.reply == PENDING_REPLY
    assert result.postcheck is PostCheckOutcome.REGENERATED
    assert len(db.messages) == 1
    assert db.messages[0].content == PENDING_REPLY
    assert CONTRADICTING_REPLY not in [row.content for row in db.messages]
    assert result.tokens_used == 1000 + COMPOSITION_TOKENS + REGENERATION_TOKENS


def test_the_regeneration_names_the_permitted_words_as_data_and_adds_no_system_message() -> None:
    """On the one message this change adds
    to a prompt. Catches a regeneration that re-sends the identical request —
    which mostly reproduces the same contradiction and spends the second call
    to reach the same fallback — because the second request would then be
    byte-identical to the first and nothing would be added at all.

    It also catches the shape 17d's data-quoting rule exists against: a
    per-turn `system` message assembled from what the tools returned. The
    system messages are asserted to be exactly the 2 shipped prompt files, so
    an added instruction is red here even though it would produce a perfectly
    good reply."""
    state = _pending_state()
    gateway = StagedGateway(
        answers=[
            StagedAnswer(text=CONTRADICTING_REPLY, total_tokens=COMPOSITION_TOKENS),
            StagedAnswer(text=PENDING_REPLY, total_tokens=REGENERATION_TOKENS),
        ]
    )

    _compose(state, gateway, RecordingSession())

    first = _messages(gateway.requests[0])
    second = _messages(gateway.requests[1])
    shipped = {load_prompt(SYSTEM_PROMPT).text, load_prompt(COMPOSITION_PROMPT).text}
    system_content = [item["content"] for item in second if item["role"] == "system"]
    added = [item for item in second if item not in first]

    assert set(system_content) == shipped
    assert len(system_content) == 2
    assert len(added) == 1
    assert added[0]["role"] == "user"
    assert permitted_words(state) == frozenset({"pending"})
    for word in permitted_words(state):
        assert word in added[0]["content"]


def test_a_second_contradicting_reply_ends_the_turn_on_the_template() -> None:
    """And the call limit from the upper side: only two
    answers are staged, so a node that tried a third time fails at the request
    rather than at an assertion.

    The second answer carries a DIFFERENT unpermitted word from the first, so
    a node that only compared the two answers to each other — and passed the
    second because it was not a repeat — is red here.

    Catches a fallback that keeps the last contradicting reply anyway, which
    would ship the very sentence the check refused, and one that writes no row
    at all, which leaves the turn with an answer the next turn's history has
    no record of. Both calls' tokens are still billed: the spend happened
    whether or not the prose was usable."""
    db = RecordingSession()
    gateway = StagedGateway(
        answers=[
            StagedAnswer(text=CONTRADICTING_REPLY, total_tokens=COMPOSITION_TOKENS),
            StagedAnswer(text=SECOND_CONTRADICTING_REPLY, total_tokens=REGENERATION_TOKENS),
        ]
    )

    result = _compose(_pending_state(tokens_used=1000), gateway, db)

    assert len(gateway.requests) == 2
    assert result.reply == outcome_reply(_pending_state())
    assert result.postcheck is PostCheckOutcome.FALLBACK
    assert result.outcome is TurnOutcome.DONE
    assert len(db.messages) == 1
    assert db.messages[0].content == outcome_reply(_pending_state())
    assert result.tokens_used == 1000 + COMPOSITION_TOKENS + REGENERATION_TOKENS


def test_a_gateway_failure_on_the_regeneration_ends_the_turn_with_no_row() -> None:
    """The first half, on the path that only exists after this change.
    The FIRST call succeeded, so a node that stored its answer before checking
    it would have a row here — and that row would be the contradicting reply.

    Catches a regeneration written outside the node's `try`, which lets the
    gateway's `ToolError` escape where `run_turn` has no translation for it,
    and a partial write: the templated reply is `fail_turn`'s to store, so no
    row at all is correct."""
    db = RecordingSession()
    gateway = StagedGateway(
        answers=[
            StagedAnswer(text=CONTRADICTING_REPLY, total_tokens=COMPOSITION_TOKENS),
            StagedAnswer(status=500),
        ]
    )

    result = _compose(_pending_state(), gateway, db)

    assert len(gateway.requests) == 2
    assert result.outcome is TurnOutcome.FAILED
    assert result.error_code is ErrorCode.INTERNAL
    assert result.reply == FAILURE_REPLY
    assert db.messages == []


def test_a_regenerated_reply_of_exactly_the_cap_is_stored() -> None:
    """The inclusive half ON THE REGENERATION PATH. The
    same gate against the same constant applies at the second call
    site, not only the first.

    Catches an implementation that checks `check_message_content` on the first
    answer and hands the regenerated one straight to `append_message`, where
    an exclusive comparison or the column itself decides instead — invisible
    to 17d's boundary cases, which never reach a second call. The staged reply
    carries no decision word, so the post-check passes it and the length gate
    is the only thing left that can refuse it."""
    reply = "x" * MAX_MESSAGE_CHARS
    db = RecordingSession()
    gateway = StagedGateway(
        answers=[
            StagedAnswer(text=CONTRADICTING_REPLY, total_tokens=COMPOSITION_TOKENS),
            StagedAnswer(text=reply, total_tokens=REGENERATION_TOKENS),
        ]
    )

    result = _compose(_pending_state(), gateway, db)

    assert len(reply) == MAX_MESSAGE_CHARS
    assert len(gateway.requests) == 2
    assert result.reply == reply
    assert result.postcheck is PostCheckOutcome.REGENERATED
    assert len(db.messages) == 1
    assert len(db.messages[0].content) == MAX_MESSAGE_CHARS


def test_a_regenerated_reply_one_character_over_the_cap_writes_no_row_at_all() -> None:
    """The exclusive half on the same path, and the "before any
    effect" half of the length limit.

    Catches a node that falls back to the TEMPLATE when the regenerated answer
    is too long — the template would be stored as though the composition had
    merely contradicted itself, and a reply the length gate refused would be
    reported to the metrics as a post-check fallback rather than as the validation
    failure it is. The turn ends the way every other model failure ends, with
    no row."""
    db = RecordingSession()
    gateway = StagedGateway(
        answers=[
            StagedAnswer(text=CONTRADICTING_REPLY, total_tokens=COMPOSITION_TOKENS),
            StagedAnswer(text="x" * (MAX_MESSAGE_CHARS + 1), total_tokens=REGENERATION_TOKENS),
        ]
    )

    result = _compose(_pending_state(), gateway, db)

    assert len(gateway.requests) == 2
    assert result.outcome is TurnOutcome.FAILED
    assert result.error_code is ErrorCode.VALIDATION
    assert result.reply == FAILURE_REPLY
    assert db.messages == []


def test_a_two_answer_staging_refuses_a_third_request() -> None:
    """Guard against a vacuous fixture, the upper half of the call limit: "at
    most 2 gateway calls, ever" rests on the third request failing the test,
    so a handler that kept answering from its last staged answer would make
    every case above pass whatever the node did."""
    gateway = StagedGateway(
        answers=[StagedAnswer(text=CONTRADICTING_REPLY), StagedAnswer(text=PENDING_REPLY)]
    )
    request = httpx.Request("POST", f"{GATEWAY_URL}/chat/completions")

    gateway.handler(request)
    gateway.handler(request)

    with pytest.raises(AssertionError):
        gateway.handler(request)


def test_a_question_after_a_filed_request_is_regenerated_with_its_own_note() -> None:
    """The reply asks "how long?" over a turn whose request is already filed.

    Catches a node that stores it, and one that retries with the decision-word
    note, which says nothing about questions and would likely repeat it. The
    added message must name the question, not the permitted words."""
    asking = "Your request is filed. How long do you need this access for?"
    gateway = StagedGateway(
        answers=[
            StagedAnswer(text=asking, total_tokens=COMPOSITION_TOKENS),
            StagedAnswer(text=PENDING_REPLY, total_tokens=REGENERATION_TOKENS),
        ]
    )
    db = RecordingSession()

    result = _compose(_pending_state(), gateway, db)

    first = _messages(gateway.requests[0])
    added = [item for item in _messages(gateway.requests[1]) if item not in first]
    assert result.reply == PENDING_REPLY
    assert result.postcheck is PostCheckOutcome.REGENERATED
    assert [row.content for row in db.messages] == [PENDING_REPLY]
    assert len(added) == 1
    assert "question" in added[0]["content"]


def test_a_twice_refused_reply_on_a_pending_request_says_it_waits_for_approval() -> None:
    """Catches a fallback that stores the generic "something went wrong" text
    over a filed request: the chat would disagree with the card beside it."""
    state = _pending_state()
    expected = outcome_reply(state)
    gateway = StagedGateway(
        answers=[
            StagedAnswer(text=CONTRADICTING_REPLY, total_tokens=COMPOSITION_TOKENS),
            StagedAnswer(text=SECOND_CONTRADICTING_REPLY, total_tokens=REGENERATION_TOKENS),
        ]
    )
    db = RecordingSession()

    result = _compose(state, gateway, db)

    assert expected != POSTCHECK_FALLBACK_REPLY
    assert result.reply == expected
    assert [row.content for row in db.messages] == [expected]
    assert result.postcheck is PostCheckOutcome.FALLBACK


def test_a_question_after_a_classification_is_kept() -> None:
    """Catches a check that still counts a classification as a filing: the
    natural "Shall I file it?" would be regenerated and then replaced."""
    asking = "Wiki editor needs your manager's approval. Shall I file it?"
    state = _state(
        tool_results=[
            ToolResult(
                tool="classify_access_request",
                ok=True,
                payload={
                    "decision": "approval",
                    "required_approvers": [{"slot": "department_manager", "user_id": None}],
                    "effective_expiry_days": 30,
                },
            )
        ]
    )
    gateway = StagedGateway(text=asking)
    db = RecordingSession()

    result = _compose(state, gateway, db)

    assert len(gateway.requests) == 1
    assert result.postcheck is PostCheckOutcome.PASSED
    assert result.reply == asking
    assert [row.content for row in db.messages] == [asking]


# --- the next steps the writer may offer ---------------------------

NEXT_STEPS_LABEL = "the only next steps you may offer this person"


def _next_steps_messages(request: httpx.Request) -> list[dict[str, str]]:
    return [
        item for item in _messages(request) if item["content"].startswith(f"{NEXT_STEPS_LABEL}:\n")
    ]


def test_the_next_steps_reach_the_writer_as_data() -> None:
    """Catches a composition that leaves the next steps to the prompt (the
    writer then invents them), or puts them in a system message, where gathered
    material would act as an instruction.

    Both lines are asserted whole and in order, in one `user` message."""
    from helpdesk.shared.auth.capabilities import SENTENCES
    from helpdesk.shared.auth.rbac import Tool

    state = _state().model_copy(
        update={
            "principals": ("employee",),
            "resolved": ResolvedEntities(
                system_slug="kyc",
                system_name="KYC",
                role_slug="kyc-analyst",
                role_name="Analyst",
                other_role_names=("Approver", "Viewer"),
            ),
        }
    )
    gateway = StagedGateway()

    _compose(state, gateway, RecordingSession())

    messages = _messages(gateway.requests[0])
    found = _next_steps_messages(gateway.requests[0])
    expected = [
        "Ask for another role on KYC: Approver, Viewer.",
        SENTENCES[Tool.CREATE_TICKET],
    ]
    assert len(found) == 1
    assert found[0]["role"] == "user"
    assert found[0]["content"] == f"{NEXT_STEPS_LABEL}:\n{json.dumps(expected)}"
    assert len([item for item in messages if item["role"] == "system"]) == 2


def test_an_empty_next_steps_list_is_still_sent() -> None:
    """Catches a composition that sends the message only when there are steps:
    with nothing sent, the writer sees no list and falls back to offering
    whatever it likes. An empty list is what tells it to offer nothing."""
    state = _state().model_copy(
        update={"principals": (), "resolved": ResolvedEntities(system_slug="vpn")}
    )
    gateway = StagedGateway()

    _compose(state, gateway, RecordingSession())

    found = _next_steps_messages(gateway.requests[0])
    assert len(found) == 1
    assert found[0]["role"] == "user"
    assert found[0]["content"] == f"{NEXT_STEPS_LABEL}:\n[]"


# --- the writer marks its own question -----------------------------

FILED_REPLY = "Filed as REQ-0107. Why wait?"
UNMARKED_QUESTION = "Tell me if anything else comes up"


def test_the_writer_is_asked_for_the_composed_reply_shape() -> None:
    """Catches a composition that asks for free text or for a looser schema: the
    mark would be missing or optional, and a forgotten mark would quietly read
    as "no question". Pins the alias, the schema name, strict mode and the exact
    two properties."""
    gateway = StagedGateway()

    _compose(_state(), gateway, RecordingSession())

    assert len(gateway.requests) == 1
    body = _body(gateway.requests[0])
    schema = body["response_format"]["json_schema"]
    assert body["model"] == "agent-default"
    assert schema["name"] == "ComposedReply"
    assert schema["strict"] is True
    assert set(schema["schema"]["properties"]) == {"text", "asks_question"}
    assert ComposedReply.model_fields.keys() == {"text", "asks_question"}


def test_a_question_mark_the_writer_did_not_mark_is_kept() -> None:
    """Catches a check that still looks for "?" in the text: a filed turn whose
    answer quotes a question but is marked false would be retried and replaced."""
    gateway = StagedGateway(text=marked(FILED_REPLY, asks=False))
    db = RecordingSession()

    result = _compose(_pending_state(), gateway, db)

    assert len(gateway.requests) == 1
    assert result.postcheck is PostCheckOutcome.PASSED
    assert result.reply == FILED_REPLY
    assert [row.content for row in db.messages] == [FILED_REPLY]


def test_a_marked_question_with_no_question_mark_is_retried() -> None:
    """Catches a check that needs a "?" to see a question: an answer marked as
    asking, worded without one, would slip through after the request was filed."""
    gateway = StagedGateway(
        answers=[
            StagedAnswer(text=marked(UNMARKED_QUESTION, asks=True)),
            StagedAnswer(text=marked(PENDING_REPLY, asks=False)),
        ]
    )

    result = _compose(_pending_state(), gateway, RecordingSession())

    first = _messages(gateway.requests[0])
    added = [item for item in _messages(gateway.requests[1]) if item not in first]
    assert len(gateway.requests) == 2
    assert len(added) == 1
    assert "already filed" in added[0]["content"]
    assert "was just asked" not in added[0]["content"]
    assert result.postcheck is PostCheckOutcome.REGENERATED
    assert result.reply == PENDING_REPLY


def test_a_marked_question_on_a_best_reading_turn_is_retried() -> None:
    """Catches a best-reading check that reads the text: a marked question with
    no "?" would be shown right after the person was already asked one."""
    state = _state().model_copy(update={"best_reading": True})
    gateway = StagedGateway(
        answers=[
            StagedAnswer(text=marked(UNMARKED_QUESTION, asks=True)),
            StagedAnswer(text=marked("Reading it as read access.", asks=False)),
        ]
    )

    result = _compose(state, gateway, RecordingSession())

    first = _messages(gateway.requests[0])
    added = [item for item in _messages(gateway.requests[1]) if item not in first]
    assert len(gateway.requests) == 2
    assert len(added) == 1
    assert "was just asked" in added[0]["content"]
    assert "already filed" not in added[0]["content"]
    assert result.postcheck is PostCheckOutcome.REGENERATED


def test_an_answer_off_the_shape_twice_fails_the_turn() -> None:
    """Catches a node that treats a bad shape as an empty reply or lets the
    error escape: `{"text": "hi"}` has no mark, so after the one retry the turn
    must end failed with the fixed sentence and no row."""
    bad = json.dumps({"text": "hi"})
    gateway = StagedGateway(answers=[StagedAnswer(text=bad), StagedAnswer(text=bad)])
    db = RecordingSession()

    result = _compose(_pending_state(), gateway, db)

    assert len(gateway.requests) == 2
    assert result.outcome is TurnOutcome.FAILED
    assert result.error_code is ErrorCode.INTERNAL
    assert result.reply == FAILURE_REPLY
    assert db.messages == []
