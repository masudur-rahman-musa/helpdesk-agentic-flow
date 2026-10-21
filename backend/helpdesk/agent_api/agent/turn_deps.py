"""What a turn may reach outside itself.

The tenant-bound database session, the model client, the MCP tool caller and
the clock. The clock is injected so tests control time. `tools` is the
`ToolCaller` protocol so tests use a stub; the route owns the session.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.agent_api.services.mcp_client import ToolCaller
from helpdesk.agent_api.services.model_gateway.client import ModelClient


@dataclass(frozen=True)
class TurnDeps:
    """The I/O one turn is given. `db` is already bound to the caller's tenant,
    so no node ever names a tenant id."""

    db: AsyncSession
    model: ModelClient
    tools: ToolCaller
    now: Callable[[], datetime]
