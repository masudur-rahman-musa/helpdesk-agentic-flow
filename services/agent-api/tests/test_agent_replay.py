"""An earlier change §6: reading a turn back out of its checkpoints (decision D).

Expected implementation surface (the implementer must provide exactly this):

``agent_api.replay``
    ``class CheckpointEntry(BaseModel)`` — frozen, with ``checkpoint_id: str``
        and ``next_nodes: tuple[str, ...]``, the node(s) the checkpoint was
        written BEFORE.
    ``async def checkpoint_history(*, checkpointer, thread_id) -> tuple[CheckpointEntry, ...]``
        — NEWEST FIRST.
    ``async def state_at(*, checkpointer, thread_id, checkpoint_id) -> TurnState | None``
    Both READ-ONLY (decision D): no invocation, no node re-run, no row written
    and no gateway call — an unknown thread answers ``()`` and ``None`` rather
    than raising.

Cost and isolation. No new backing service at all: this file drives
``test_agent_approval_interrupt.py``'s session-scoped ``approval_env``, so the
throwaway database, its seeded fake tenant and LangGraph's checkpoint tables
are the same ones an earlier change built, created once for the whole session (standing
rule 7). Per-case isolation is a fresh ``session_id`` — the thread id — never a
new database. Nothing here stages a gateway answer, because a read that
reached a model would be a re-run.
"""

import uuid
from typing import Any

from agent_api.checkpointer import build_checkpointer
from agent_api.graph.build import AWAIT_APPROVAL, EXECUTE_TOOLS
from agent_api.graph.state import TurnOutcome, TurnState
from agent_api.replay import CheckpointEntry, checkpoint_history, state_at

# Imported, not redefined: the same database, tenant and checkpoint tables as
# `test_agent_approval_interrupt.py` and `test_agent_approval_resume.py`
# (a project rule, and the plan's §6 cost paragraph).
from test_agent_approval_interrupt import (
    CREATE_REQUEST,
    ApprovalEnv,
    ScriptedToolCaller,
    StagedGateway,
    _extraction_answer,
    _page,
    _payload,
    _plan_answer,
    _run_paused_turn,
    _with_pool,
    approval_env,  # noqa: F401
)


def _pause(env: ApprovalEnv) -> tuple[uuid.UUID, uuid.UUID]:
    """One paused turn, and the thread and request id it left behind."""
    session_id = uuid.uuid4()
    request_id = uuid.uuid4()
    gateway = StagedGateway([_extraction_answer(), _plan_answer(env)])
    tools = ScriptedToolCaller(_page(), _payload(decision="approval", request_id=str(request_id)))

    paused: TurnState = _run_paused_turn(env, session_id=session_id, gateway=gateway, tools=tools)

    assert paused.outcome is TurnOutcome.AWAIT_APPROVAL, "the case needs a paused thread"
    return session_id, request_id


def _history(env: ApprovalEnv, thread_id: str) -> tuple[CheckpointEntry, ...]:
    """The thread's checkpoints, read by a saver built after the run is gone."""

    async def scenario(pool: Any) -> tuple[CheckpointEntry, ...]:
        return await checkpoint_history(checkpointer=build_checkpointer(pool), thread_id=thread_id)

    return _with_pool(env.dsn, scenario)


def _state(env: ApprovalEnv, thread_id: str, checkpoint_id: str) -> TurnState | None:
    """One named checkpoint of that thread, as the state it stands for."""

    async def scenario(pool: Any) -> TurnState | None:
        return await state_at(
            checkpointer=build_checkpointer(pool),
            thread_id=thread_id,
            checkpoint_id=checkpoint_id,
        )

    return _with_pool(env.dsn, scenario)


def test_checkpoint_history_lists_the_threads_checkpoints_newest_first(
    approval_env: ApprovalEnv,  # noqa: F811
) -> None:
    """Catches a history returned in the library's own storage order rather
    than newest first, and a history that reports only the latest checkpoint.

    The order matters because "which node diverged" is answered by walking back
    from where the turn stopped: an oldest-first list read by a debugger that
    trusts the documented order points at `load_session` for every turn, which
    is the one node that never decides anything.

    The order assertion cannot pass on a reversed list, and that is deliberate
    — it is not `entries[0].checkpoint_id != entries[-1].checkpoint_id`, which
    a reversal satisfies. `state_at` is asked to read `entries[0]` back and it
    has to be the PAUSED state: on an oldest-first list that first entry is the
    start of the turn, whose outcome is `continue` and whose
    `awaiting_request_id` is `None`.

    `next_nodes` is asserted on the paused entry for the same reason the plan
    gives it a field at all: the checkpoint records the node it was written
    BEFORE, so a paused thread names the node that has not run to completion.
    An implementation that reported the node just FINISHED would name
    `execute_tools` here, which reads plausibly and points a reader one step
    away from the pause."""
    session_id, request_id = _pause(approval_env)
    thread_id = str(session_id)

    history = _history(approval_env, thread_id)

    assert len(history) > 1
    assert all(entry.checkpoint_id for entry in history)
    assert len({entry.checkpoint_id for entry in history}) == len(history)
    assert history[0].next_nodes == (AWAIT_APPROVAL,)

    newest = _state(approval_env, thread_id, history[0].checkpoint_id)
    assert newest is not None
    assert newest.outcome is TurnOutcome.AWAIT_APPROVAL
    assert newest.awaiting_request_id == request_id


def test_state_at_returns_one_checkpoint_as_a_turn_state(
    approval_env: ApprovalEnv,  # noqa: F811
) -> None:
    """Catches a `state_at` that ignores the checkpoint id it was given and
    returns the thread's latest state — the shape `aget_tuple(config)` has by
    default, so leaving the id out of the config is a one-line mistake that
    every "does it return a TurnState" assertion passes.

    The two checkpoints are chosen so that only reading the NAMED one can tell
    them apart: the step written before `execute_tools` carries no tool results
    at all, and the paused step carries the `create_access_request` result the
    pause was decided from. A reader answering "the latest" gives the second
    for both, which is exactly the debugging answer that would make a
    divergence invisible.

    It is also the read-only half of decision D: `state_at` returns a state, it
    does not re-run the node that produced it — the tool caller and the gateway
    of the paused run are long gone, so a version that re-invoked the graph has
    nothing to invoke with."""
    session_id, _request_id = _pause(approval_env)
    thread_id = str(session_id)
    history = _history(approval_env, thread_id)

    before_execution = [entry for entry in history if entry.next_nodes == (EXECUTE_TOOLS,)]
    assert len(before_execution) == 1, "the paused turn executes exactly one round"

    paused = _state(approval_env, thread_id, history[0].checkpoint_id)
    assert paused is not None
    assert [result.tool for result in paused.tool_results] == [CREATE_REQUEST]

    earlier = _state(approval_env, thread_id, before_execution[0].checkpoint_id)
    assert earlier is not None
    assert earlier.tool_results == ()


def test_an_unknown_thread_has_an_empty_history_and_no_state(
    approval_env: ApprovalEnv,  # noqa: F811
) -> None:
    """The read-only contract's unhappy half. Catches a replay that raises on a
    thread nobody ever ran: these two are what an operator's "show me this
    turn" calls, and a mistyped or already-purged session id is the ordinary
    case, not an incident. An implementation indexing `[0]` into an empty list,
    or dereferencing `aget_tuple`'s `None`, turns that into a 500.

    The empty tuple is asserted rather than falsiness, because `None` and `[]`
    are both falsy and only one of them is the contract: a caller iterating the
    answer breaks on `None`."""
    unknown_thread = str(uuid.uuid4())
    unknown_checkpoint = str(uuid.uuid4())

    assert _history(approval_env, unknown_thread) == ()
    assert _state(approval_env, unknown_thread, unknown_checkpoint) is None
