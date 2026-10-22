"""What a turn may reach outside itself.

The tenant-bound database session, the model client, the MCP tool caller, the
clock and the loop's compiled graph. The clock is injected so tests control time. `tools` is the
`ToolCaller` protocol so tests use a stub; the route owns the session.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.agent_api.services.mcp_client import ToolCaller
from helpdesk.agent_api.services.model_gateway.client import ModelClient

if TYPE_CHECKING:
    from helpdesk.agent_api.agent.graph import LoopGraph


@dataclass(frozen=True)
class TurnDeps:
    """The I/O one turn is given. `db` is already bound to the caller's tenant,
    so no node ever names a tenant id."""

    db: AsyncSession
    model: ModelClient
    tools: ToolCaller
    now: Callable[[], datetime]
    graph: "LoopGraph | None" = None
    """The graph compiled at app start with the saver; `None` runs one that
    saves nothing."""
