"""Read a turn back out of its checkpoints, for debugging. Read-only.

Nothing runs: no node, no row, no model or tool call. Rerunning the graph is
left out on purpose, since nodes write rows and spend model calls. The graph
is used rather than the saver alone because only the graph knows which node
was about to run.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict

from helpdesk.agent_api.agent.checkpointer import thread_config
from helpdesk.agent_api.agent.graph import Saver, build_graph
from helpdesk.agent_api.agent.state import TurnState

__all__ = ["CheckpointEntry", "checkpoint_history", "state_at"]

_CHECKPOINT_ID_KEY = "checkpoint_id"


class CheckpointEntry(BaseModel):
    """One saved step: its id, and the node that was about to run.

    The next node, not the finished one, so a paused thread's newest entry names
    `await_approval`.
    """

    model_config = ConfigDict(frozen=True)

    checkpoint_id: str
    next_nodes: tuple[str, ...]


async def checkpoint_history(*, checkpointer: Saver, thread_id: str) -> tuple[CheckpointEntry, ...]:
    """Every checkpoint of one thread, newest first, since debugging walks back
    from where the turn stopped. An unknown thread gives `()`, not an error: a
    mistyped or purged id is the normal case."""
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
    """One named checkpoint as the turn state it holds, or `None`.

    The checkpoint id must go into the config, or this reads the latest state.
    LangGraph answers a missing checkpoint with an empty snapshot, which becomes
    `None` here rather than a state for a turn that never ran.
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
