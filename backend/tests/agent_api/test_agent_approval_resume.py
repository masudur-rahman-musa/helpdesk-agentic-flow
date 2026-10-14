"""An earlier change §6: the recorded decision as a pure mapping, then the resume.

Expected implementation surface (the implementer must provide exactly this):

``agent_api.graph.gates``
    ``APPROVE_REQUEST = "approve_request"``, ``REJECT_REQUEST = "reject_request"``
    ``def decision_result(*, status: str, request_id: uuid.UUID) -> ToolResult | None``
        — PURE, no framework, no store, no ORM. The decided statuses map to the
        tool result the composition reads; `pending`, `rolled_back` and
        `expired` map to `None`, which is what `resume_turn` refuses on.
``agent_api.graph.build``
    ``Saver = BaseCheckpointSaver[Any]`` — the alias other modules import so
        that LangGraph stays inside this one file (the design).
    ``async def resume_graph(*, deps, thread_id, decision, checkpointer) -> TurnState``
``agent_api.resume`` (new, imports NO framework and no driver)
    ``class ResumeRefusedError(ToolError)`` with ``code = ErrorCode.VALIDATION``
    ``async def resume_turn(*, deps, request_id, checkpointer) -> TurnState``
``agent_api.graph.nodes.await_approval``
    the ``append_message`` call MOVES OUT (decision C); the resume return
    appends the decision result and sets ``outcome = TurnOutcome.DONE``.

Cost and isolation. The compose Postgres only, through
``test_agent_approval_interrupt.py``'s session-scoped ``approval_env``: ONE
throwaway database, one seeded fake tenant and one set of LangGraph tables for
both this file and ``test_agent_replay.py`` (a project rule, and the plan's
own §6 cost paragraph — "reused from `test_agent_approval_interrupt.py`'s
arrangement"). Per-case isolation is a namespace and never a rebuild: a fresh
``session_id`` (which IS the checkpoint thread id, 18a decision A1) and a fresh
``access_requests`` row id per case. A pool and an engine are opened per
``asyncio.run`` because each case drives its own event loop; the resume cases
open a SECOND pool, engine and ``TurnDeps`` on the same DSN on purpose, which
is criterion 1 — the process that paused the run is gone by then.
"""

import ast
import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

# Imported, not redefined: one seeded database, one set of checkpoint tables
# and one staged-gateway arrangement for the whole session, shared with
# `test_agent_approval_interrupt.py` and `test_agent_replay.py` (standing
# rule 7 — the plan's §6 says "reused from that file's arrangement").
from test_agent_approval_interrupt import (
    API_KEYS,
    AWAIT_APPROVAL_VALUE,
    CREATE_REQUEST,
    GATEWAY_URL,
    NOW,
    ApprovalEnv,
    ScriptedToolCaller,
    StagedGateway,
    _checkpointed_state,
    _extraction_answer,
    _page,
    _payload,
    _plan_answer,
    _run_paused_turn,
    _with_pool,
    approval_env,  # noqa: F401
)

import helpdesk.agent_api.agent.resume as resume_module
from helpdesk.agent_api.agent.checkpointer import build_checkpointer
from helpdesk.agent_api.agent.gates import APPROVE_REQUEST, REJECT_REQUEST, decision_result
from helpdesk.agent_api.agent.graph import TurnDeps
from helpdesk.agent_api.agent.resume import ResumeRefusedError, resume_turn
from helpdesk.agent_api.agent.state import ToolResult, TurnOutcome, TurnState
from helpdesk.agent_api.services.model_gateway.client import ModelClient
from helpdesk.shared.core.db import create_engine, create_session_factory, tenant_session
from helpdesk.shared.core.repository import list_recent_messages
from helpdesk.shared.errors import ErrorCode
from helpdesk.shared.models.access import AccessRequest, RequestStatus
from helpdesk.shared.models.conversation import MessageRole

COMPOSED_APPROVAL = "Your fake payroll access was approved and is now in place."
"""The one composition answer staged for a resumed approve run.

A sentence no template in this service renders, so a reply that came from
`PENDING_APPROVAL_REPLY` or from `FAILURE_REPLY` cannot be mistaken for it.
"""

COMPOSED_REJECTION = "An approver declined your fake payroll request."
"""The same for the reject path, and equally not any template's words."""


# --- the pure mapping, with no graph and no database at all -------------------


@pytest.mark.parametrize(
    ("status", "expected_tool", "expected_ok"),
    [
        (RequestStatus.APPROVED.value, APPROVE_REQUEST, True),
        (RequestStatus.EXECUTED.value, APPROVE_REQUEST, True),
        (RequestStatus.REJECTED.value, REJECT_REQUEST, True),
        (RequestStatus.FAILED.value, APPROVE_REQUEST, False),
    ],
)
def test_the_decision_result_is_built_from_the_row_and_not_from_an_argument(
    status: str, expected_tool: str, expected_ok: bool
) -> None:
    """Catches a mapping that reports every decided row as an approval.

    The `rejected` row is the one no other row can decide: an implementation
    that returned `ToolResult(tool=APPROVE_REQUEST, ok=True, ...)` for anything
    that is not `pending` reads identically on the three other rows here, and
    would hand `compose_reply` an approval for a request an approver refused —
    the exact contradiction the plan's fourth risk names.

    The `failed` row is the second one nothing else pins: it is a DECIDED row
    (so it must resume rather than be refused) whose execution did not succeed,
    so a mapping keyed on "decided means ok" tells the user a grant exists that
    does not. `error_code` stays `None` because the row records no failure
    code, and inventing one would tell the composition a cause nobody wrote
    down; `PLAIN_ERRORS` would then put that invented sentence in the prompt.

    The payload ids are STRINGS, the shape `execute_tools` records: a mapping
    that put a `uuid.UUID` in the payload would not survive the checkpoint's
    json serde, so the value asserted here is the wire value exactly."""
    request_id = uuid.UUID("77777777-7777-5777-8777-777777777777")

    result = decision_result(status=status, request_id=request_id)

    assert result == ToolResult(
        tool=expected_tool,
        ok=expected_ok,
        payload={"request_id": str(request_id), "status": status},
        error_code=None,
    )


@pytest.mark.parametrize(
    "status",
    [
        RequestStatus.PENDING.value,
        RequestStatus.ROLLED_BACK.value,
        RequestStatus.EXPIRED.value,
    ],
)
def test_a_pending_request_maps_to_no_decision_result(status: str) -> None:
    """Catches a mapping that treats "not approved" as a rejection.

    A `pending` request has no decision at all, and a resume built on one would
    compose a reply about an outcome no approver has reached; the `rolled_back`
    and `expired` rows catch the same gap from the other side — both are ENDED
    states rather than decisions, so an implementation branching on
    `status != "approved"` would answer them as refusals to the user.

    This is the pure half of criterion 3: `resume_turn` refuses exactly when
    this answers `None`, so a mapping that invented a result here would let a
    still-pending request drive a run to completion."""
    request_id = uuid.UUID("77777777-7777-5777-8777-777777777777")

    assert decision_result(status=status, request_id=request_id) is None


# --- the paused turn, the decided row, and the resume -------------------------


def _pause(env: ApprovalEnv) -> tuple[uuid.UUID, uuid.UUID, TurnState]:
    """One paused turn over this case's own session id and request id.

    Returns the thread (the session id), the request the pause is waiting on
    and the paused state, so a case can assert against the rendering the pause
    actually produced rather than against a second copy of the template.
    """
    session_id = uuid.uuid4()
    request_id = uuid.uuid4()
    gateway = StagedGateway([_extraction_answer(), _plan_answer(env)])
    tools = ScriptedToolCaller(_page(), _payload(decision="approval", request_id=str(request_id)))

    paused: TurnState = _run_paused_turn(env, session_id=session_id, gateway=gateway, tools=tools)

    assert paused.outcome is TurnOutcome.AWAIT_APPROVAL, "the case needs a paused thread"
    return session_id, request_id, paused


def _store_request(
    env: ApprovalEnv,
    *,
    request_id: uuid.UUID,
    status: str,
    session_id: uuid.UUID | None,
) -> None:
    """One obviously fake `access_requests` row, as an approver would leave it.

    Written directly rather than through `approve_request`: this plan reads
    what the MCP server recorded and never calls it (§3), so the row is the
    fixture and the tool is out of scope.
    """

    async def scenario() -> None:
        engine = create_engine(env.database_url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, env.tenant_id) as db:
                db.add(
                    AccessRequest(
                        id=request_id,
                        tenant_id=env.tenant_id,
                        requester_user_id=env.user_id,
                        target_user_id=env.user_id,
                        role_id=env.role_id,
                        justification="Obviously fake justification.",
                        requested_expiry_days=None,
                        risk_class="low",
                        decision="approval",
                        decision_reasons=[],
                        citations=[],
                        status=status,
                        approver_user_ids=[],
                        approvals=[],
                        session_id=session_id,
                        trace_id=None,
                        idempotency_key=None,
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def _resume(
    env: ApprovalEnv,
    *,
    request_id: uuid.UUID,
    gateway: StagedGateway,
    tools: ScriptedToolCaller,
) -> TurnState:
    """The resume, from a process that shares nothing with the one that paused.

    A NEW pool, a NEW engine, a NEW saver and a NEW `TurnDeps` over the same
    DSN — the compiled graph that paused the run, and everything it held, is
    gone by the time this runs. That is criterion 1, and it is the only
    arrangement in which an in-process saver cannot pass.
    """

    async def scenario(pool: Any) -> TurnState:
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
                    return await resume_turn(
                        deps=deps,
                        request_id=request_id,
                        checkpointer=checkpointer,
                    )
            finally:
                await client.aclose()
        finally:
            await engine.dispose()

    return _with_pool(env.dsn, scenario)


def _messages(env: ApprovalEnv, session_id: uuid.UUID) -> list[tuple[str, str]]:
    """This conversation's stored rows, oldest first, as (role, content)."""

    async def scenario() -> list[tuple[str, str]]:
        engine = create_engine(env.database_url)
        try:
            factory = create_session_factory(engine)
            async with tenant_session(factory, env.tenant_id) as db:
                rows = await list_recent_messages(db, session_id)
                return [(row.role, row.content) for row in rows]
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _prompt_text(request: httpx.Request) -> str:
    """Every message of one gateway request, flattened to one string.

    The composition quotes the tool results as data, so what the model was
    told about the decision is somewhere in this text and nowhere else.
    """
    body: Any = json.loads(request.content)
    messages: list[dict[str, Any]] = body["messages"]
    return "\n".join(str(message.get("content", "")) for message in messages)


def test_a_recorded_approval_resumes_the_run_from_a_fresh_saver(
    approval_env: ApprovalEnv,  # noqa: F811
) -> None:
    """Catches a resume that starts a NEW run instead of continuing the paused
    one: a `resume_turn` that invoked the graph with a fresh `TurnState` rather
    than sending `Command(resume=...)` on the thread would re-extract, re-plan
    and file the request a second time — the tool caller here is scripted with
    NOTHING, so any such call fails the case where it happens.

    It also catches the pause never surviving its process, which is the whole
    point of the plan: the pool, the engine, the saver and the `TurnDeps` that
    wrote the checkpoint are all closed before `resume_turn` opens its own, so
    an implementation that kept the compiled graph in memory has nothing to
    resume from.

    Exactly ONE composition answer is staged, so the run is pinned from both
    sides: a resume that never reached `compose_reply` returns a templated
    reply that is not `COMPOSED_APPROVAL`, and one that composed twice asks for
    a second answer nobody staged and fails outright.

    The last tool result is what tells this from a resume that composed over
    the pre-pause state alone: `compose_reply` quotes `tool_results` as data,
    so a decision that never reached the state is a reply about a request whose
    outcome the model was never told."""
    session_id, request_id, _paused = _pause(approval_env)
    _store_request(
        approval_env,
        request_id=request_id,
        status=RequestStatus.APPROVED.value,
        session_id=session_id,
    )
    gateway = StagedGateway([COMPOSED_APPROVAL])
    tools = ScriptedToolCaller()

    state = _resume(approval_env, request_id=request_id, gateway=gateway, tools=tools)

    assert state.outcome is TurnOutcome.DONE
    assert state.reply == COMPOSED_APPROVAL
    assert gateway.call_count == 1
    assert tools.names == []
    assert [result.tool for result in state.tool_results] == [CREATE_REQUEST, APPROVE_REQUEST]
    last = state.tool_results[-1]
    assert last.ok is True
    assert last.payload == {
        "request_id": str(request_id),
        "status": RequestStatus.APPROVED.value,
    }


def test_a_rejection_resumes_and_composes_the_rejection(
    approval_env: ApprovalEnv,  # noqa: F811
) -> None:
    """Catches a resume that hard-codes the approve path.

    An implementation that sent `approve_request` for every decided row — or
    that re-entered `execute_tools` on resume, which is what the design says
    literally and what a design note amends — reads identically on the approve case
    above and is wrong here in the way that matters most: it would grant, or
    tell the user it granted, access an approver refused.

    The tool caller is scripted with NOTHING, so decision A1's "no further tool
    call" is a hard stop rather than a count: a run that re-entered execution
    fails inside the caller. And the prompt assertion is what pins the
    composition being told WHICH decision it is composing — a reject result
    that never reached `tool_results` leaves the model composing from the
    pre-pause round, where the request is merely filed."""
    session_id, request_id, _paused = _pause(approval_env)
    _store_request(
        approval_env,
        request_id=request_id,
        status=RequestStatus.REJECTED.value,
        session_id=session_id,
    )
    gateway = StagedGateway([COMPOSED_REJECTION])
    tools = ScriptedToolCaller()

    state = _resume(approval_env, request_id=request_id, gateway=gateway, tools=tools)

    assert state.outcome is TurnOutcome.DONE
    assert state.reply == COMPOSED_REJECTION
    assert tools.names == []
    assert [result.tool for result in state.tool_results] == [CREATE_REQUEST, REJECT_REQUEST]
    assert state.tool_results[-1].payload["status"] == RequestStatus.REJECTED.value

    assert gateway.call_count == 1
    prompt = _prompt_text(gateway.requests[0])
    assert REJECT_REQUEST in prompt
    assert RequestStatus.REJECTED.value in prompt


def test_a_still_pending_request_is_refused_and_the_checkpoint_stays_paused(
    approval_env: ApprovalEnv,  # noqa: F811
) -> None:
    """Catches a resume that sends the `Command` first and inspects the row
    afterwards. Every other guard passes here on purpose — the row is in the
    caller's tenant, its `session_id` names a thread that IS paused, and its id
    IS the one the pause is waiting on — so the recorded-decision check is the
    only thing that can refuse, and deleting it is the only change that flips
    this case.

    The checkpoint assertion is the half a typed error alone does not give: a
    resume that drove the graph and then raised would leave the thread finished
    behind an exception, so the approver's real decision, when it arrives,
    would find nothing to resume. Both the outcome and the awaited request id
    are read back through a saver built after the refusal, which is where a
    thread mutated in passing shows up."""
    session_id, request_id, _paused = _pause(approval_env)
    _store_request(
        approval_env,
        request_id=request_id,
        status=RequestStatus.PENDING.value,
        session_id=session_id,
    )

    with pytest.raises(ResumeRefusedError) as exc:
        _resume(
            approval_env,
            request_id=request_id,
            gateway=StagedGateway([]),
            tools=ScriptedToolCaller(),
        )

    assert exc.value.code is ErrorCode.VALIDATION

    values = _checkpointed_state(approval_env, session_id)
    assert str(values["outcome"]) == AWAIT_APPROVAL_VALUE
    assert str(values["awaiting_request_id"]) == str(request_id)


def test_a_request_the_pause_is_not_waiting_on_is_refused(
    approval_env: ApprovalEnv,  # noqa: F811
) -> None:
    """a design note's check, arranged so nothing else can pass this case for it.

    Both requests belong to the SAME tenant, both are decided, and both carry
    the same `session_id` — so the tenant scope, the recorded-decision gate and
    the paused-thread lookup all say yes to the id being resumed. The only
    thing standing between the caller and a completed run is comparing the row
    against the checkpointed `awaiting_request_id`, which is why this catches a
    `resume_turn` that keys the resume on the SESSION alone.

    That wrong implementation is not hypothetical: the thread id comes from
    `session_id` (decision E), so an implementation that stopped there resumes
    whichever request the caller names with any decided sibling's outcome — one
    approval on one request silently deciding another.

    Nothing is staged on the gateway, so a run that started at all fails here
    rather than passing quietly, and the checkpoint is read back to prove the
    pause is untouched."""
    session_id, awaited_id, _paused = _pause(approval_env)
    other_id = uuid.uuid4()
    _store_request(
        approval_env,
        request_id=awaited_id,
        status=RequestStatus.APPROVED.value,
        session_id=session_id,
    )
    _store_request(
        approval_env,
        request_id=other_id,
        status=RequestStatus.APPROVED.value,
        session_id=session_id,
    )

    with pytest.raises(ResumeRefusedError) as exc:
        _resume(
            approval_env,
            request_id=other_id,
            gateway=StagedGateway([]),
            tools=ScriptedToolCaller(),
        )

    assert exc.value.code is ErrorCode.VALIDATION

    values = _checkpointed_state(approval_env, session_id)
    assert str(values["outcome"]) == AWAIT_APPROVAL_VALUE
    assert str(values["awaiting_request_id"]) == str(awaited_id)


def test_a_request_with_no_session_id_is_refused(
    approval_env: ApprovalEnv,  # noqa: F811
) -> None:
    """Decision E's NULL case. Catches an implementation that turns the column
    into a thread id without looking: `str(None)` is the thread `"None"`, which
    `check_slug` accepts, so the resume would address a checkpoint thread that
    can never be the one the request was filed on — and every request filed
    outside a turn would collide on that same invented thread.

    The row here is DECIDED and its id IS the id a real pause elsewhere is
    waiting on, so the recorded-decision gate and the id match cannot decide
    this case; removing the NULL check is the only change that flips it."""
    _session_id, request_id, _paused = _pause(approval_env)
    _store_request(
        approval_env,
        request_id=request_id,
        status=RequestStatus.APPROVED.value,
        session_id=None,
    )

    with pytest.raises(ResumeRefusedError) as exc:
        _resume(
            approval_env,
            request_id=request_id,
            gateway=StagedGateway([]),
            tools=ScriptedToolCaller(),
        )

    assert exc.value.code is ErrorCode.VALIDATION


def test_a_thread_with_no_paused_checkpoint_is_refused(
    approval_env: ApprovalEnv,  # noqa: F811
) -> None:
    """Catches a resume that sends `Command(resume=...)` to a thread it never
    read: LangGraph answers a `Command` on an unknown thread by starting the
    graph from nothing rather than by raising, so the caller would get a run
    with no user message, no citations and no request — a turn invented out of
    a decision.

    The row is decided and its `session_id` is present, so neither of those
    guards can refuse it; only reading the thread back and finding no paused
    checkpoint can. Nothing is staged on the gateway, so a run that started
    fails at its first model call instead of returning a plausible state."""
    request_id = uuid.uuid4()
    _store_request(
        approval_env,
        request_id=request_id,
        status=RequestStatus.APPROVED.value,
        session_id=uuid.uuid4(),
    )

    with pytest.raises(ResumeRefusedError) as exc:
        _resume(
            approval_env,
            request_id=request_id,
            gateway=StagedGateway([]),
            tools=ScriptedToolCaller(),
        )

    assert exc.value.code is ErrorCode.VALIDATION


def test_a_second_resume_of_the_same_run_is_refused(
    approval_env: ApprovalEnv,  # noqa: F811
) -> None:
    """The plan's second risk. Catches a resume that checks only that a
    checkpoint EXISTS: after the first resume the thread still has checkpoints,
    still names a decided request in the caller's tenant and still carries the
    matching `awaiting_request_id` — every other guard says yes — so the only
    thing that can refuse is reading the checkpointed OUTCOME and finding it is
    no longer `await_approval`.

    The wrong implementation this kills composes a second reply for one
    question, bills a second strong-alias round trip and stores a second
    assistant row, every time an approval webhook is delivered twice. The
    second gateway is staged with nothing and asserted to have received NO
    request at all, which is what tells a refusal before the `Command` from one
    raised after the run."""
    session_id, request_id, _paused = _pause(approval_env)
    _store_request(
        approval_env,
        request_id=request_id,
        status=RequestStatus.APPROVED.value,
        session_id=session_id,
    )
    first = _resume(
        approval_env,
        request_id=request_id,
        gateway=StagedGateway([COMPOSED_APPROVAL]),
        tools=ScriptedToolCaller(),
    )
    assert first.outcome is TurnOutcome.DONE, "the case needs a run that completed once"

    second_gateway = StagedGateway([COMPOSED_APPROVAL])
    with pytest.raises(ResumeRefusedError) as exc:
        _resume(
            approval_env,
            request_id=request_id,
            gateway=second_gateway,
            tools=ScriptedToolCaller(),
        )

    assert exc.value.code is ErrorCode.VALIDATION
    assert second_gateway.call_count == 0


def test_a_resumed_turn_leaves_exactly_one_pending_message(
    approval_env: ApprovalEnv,  # noqa: F811
) -> None:
    """Decision C, and the plan's first risk. Catches the `append_message` call
    staying inside `await_approval`: LangGraph replays the interrupting node
    from its start on resume, so the pending reply would be stored a SECOND
    time — and the user's history would then say a request is waiting for
    approval after the approver already decided it.

    The hand arithmetic, for the three rows this case
    expects. `run_turn` writes two: `load_session` stores the user message, and
    `run_turn` itself stores the pending reply after mapping the interrupt. The
    resumed run writes ONE: `await_approval` replays and stores nothing, and
    `compose_reply` stores the composed reply. Two plus one is three, and the
    duplicate this test exists for would make it four.

    Counting each content rather than only the total is what makes the failure
    readable: a duplicate pending reply and a duplicate composition are the
    same number and completely different bugs."""
    session_id, request_id, paused = _pause(approval_env)
    assert paused.reply is not None
    pending_reply_text = paused.reply
    assert pending_reply_text != COMPOSED_APPROVAL

    stored_at_pause = _messages(approval_env, session_id)
    assistant_at_pause = [
        content for role, content in stored_at_pause if role == MessageRole.ASSISTANT.value
    ]
    assert assistant_at_pause == [pending_reply_text], (
        "the pause itself must store exactly one assistant message"
    )

    _store_request(
        approval_env,
        request_id=request_id,
        status=RequestStatus.APPROVED.value,
        session_id=session_id,
    )
    _resume(
        approval_env,
        request_id=request_id,
        gateway=StagedGateway([COMPOSED_APPROVAL]),
        tools=ScriptedToolCaller(),
    )

    stored = _messages(approval_env, session_id)
    contents = [content for _role, content in stored]
    roles = [role for role, _content in stored]

    assert len(stored) == 3
    assert roles.count(MessageRole.USER.value) == 1
    assert contents.count(pending_reply_text) == 1
    assert contents.count(COMPOSED_APPROVAL) == 1


def test_resume_imports_no_framework() -> None:
    """The plan's third risk, as a static property. Catches `resume.py` reaching
    for `Command`, for `AsyncPostgresSaver` or for `sqlalchemy` directly: the
    boundary the whole graph package is arranged around is "LangGraph lives in
    `build.py`, `checkpointer.py` and `await_approval.py`'s `interrupt`", and a
    fourth importer ends it silently — nothing else in the suite would go red.

    `sqlalchemy` is forbidden for the second half of the same rule: the resume
    reaches the database only through `helpdesk.core.repository`, so a hand
    written `select()` here would be a tenant-scoping decision made outside the
    one place that makes it."""
    source = Path(str(resume_module.__file__))
    tree = ast.parse(source.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module.split(".")[0])

    forbidden = {
        "langgraph",
        "langchain",
        "langchain_core",
        "sqlalchemy",
        "httpx",
        "redis",
        "qdrant_client",
        "psycopg",
        "psycopg_pool",
    }
    assert imported & forbidden == set()
