"""Reading a turn back out of its checkpoints, for debugging (the design).

Two functions, both READ-ONLY. They answer the question a
person actually asks of a saved turn — which node did this diverge at, and what
did the state look like there — and they answer it without running anything: no
node executes, no row is written, no gateway call is made and no tool is
called. A checkpoint is a record, and reading a record must not repeat what
made it.

Re-INVOKING the graph from a checkpoint is deliberately not here. It would
re-run nodes that write rows and spend gateway calls, and the only caller that
would justify that blast radius is the eval runner of a later row, which does
not exist yet.

LangGraph appears in this module the way the design's boundary rule requires:
not at all. The compiled graph comes from `build.py`, the thread config from
`checkpointer.py`, and the saver is typed through `build.Saver`. `build_graph`
is used rather than the saver's own listing because the NODE a checkpoint was
written before is a property of the graph, not of the store: the saver knows
what was saved, only the graph knows what was going to run next.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict

from helpdesk.agent_api.checkpointer import thread_config
from helpdesk.agent_api.graph.build import Saver, build_graph
from helpdesk.agent_api.graph.state import TurnState

__all__ = ["CheckpointEntry", "checkpoint_history", "state_at"]

_CHECKPOINT_ID_KEY = "checkpoint_id"


class CheckpointEntry(BaseModel):
    """One saved step of one thread: its id, and what was about to run.

    `next_nodes` is the node or nodes the checkpoint was written BEFORE, never
    the one just finished. That is the direction a debugger reads: a paused
    thread's newest entry names `await_approval`, the node that has not run to
    completion, and reporting the finished node instead would point a reader one
    step away from the pause every time. Pinned by
    `test_agent_replay.py::test_checkpoint_history_lists_the_threads_checkpoints_newest_first`.

    Frozen, like everything else this package hands out: a record read for
    debugging is not something a caller should be able to edit in passing.
    """

    model_config = ConfigDict(frozen=True)

    checkpoint_id: str
    next_nodes: tuple[str, ...]


async def checkpoint_history(*, checkpointer: Saver, thread_id: str) -> tuple[CheckpointEntry, ...]:
    """Every checkpoint of one thread, NEWEST FIRST.

    Newest first because "which node diverged" is answered by walking back from
    where the turn stopped; an oldest-first list read by a caller that trusts
    this order points at `load_session` for every turn, the one node that
    decides nothing.

    An unknown thread answers `()` — never an exception (decision D): a mistyped
    or already-purged session id is the ordinary case for an operator's "show me
    this turn", not an incident. Pinned by
    `test_agent_replay.py::test_an_unknown_thread_has_an_empty_history_and_no_state`.
    """
    graph = build_graph(checkpointer)
    entries: list[CheckpointEntry] = []
    async for snapshot in graph.aget_state_history(thread_config(thread_id)):
        configurable: dict[str, Any] = dict(snapshot.config.get("configurable", {}))
        checkpoint_id = configurable.get(_CHECKPOINT_ID_KEY)
        if not isinstance(checkpoint_id, str):
            continue
        entries.append(
            CheckpointEntry(checkpoint_id=checkpoint_id, next_nodes=tuple(snapshot.next))
        )
    return tuple(entries)


async def state_at(*, checkpointer: Saver, thread_id: str, checkpoint_id: str) -> TurnState | None:
    """One NAMED checkpoint of one thread, as the turn state it holds.

    The checkpoint id goes into the config, which is what makes this a read of
    that step rather than of the thread's latest state — the shape a config
    carrying only the thread id has, and a one-line mistake every "does it
    return a `TurnState`" assertion would pass. Pinned by
    `test_agent_replay.py::test_state_at_returns_one_checkpoint_as_a_turn_state`,
    which reads two steps whose `tool_results` differ.

    `None` for a thread or a checkpoint that does not exist: LangGraph answers a
    missing one with an empty snapshot rather than by raising, and passing that
    on as a state with no values would be a record of a turn that never ran.
    """
    config = thread_config(thread_id)
    config["configurable"][_CHECKPOINT_ID_KEY] = checkpoint_id
    snapshot = await build_graph(checkpointer).aget_state(config)
    values: Any = snapshot.values
    if not isinstance(values, dict) or not values:
        return None
    return TurnState.model_validate(
        {key: value for key, value in values.items() if not str(key).startswith("__")}
    )
