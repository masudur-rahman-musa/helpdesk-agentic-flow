"""What a turn's nodes are allowed to reach outside themselves.

Its own module, not `build.py`, so a node can be typed against it without
importing the module that imports every node. Re-exported from `build` for
callers, which is the only name a route needs.

Everything a node touches that is not pure is here and nowhere else: the
tenant-bound database session, the model client, the MCP tool caller and the
clock. The clock is injected rather than read, exactly as `helpdesk.auth.jwt`
and the audit writer do it, so `started_at` and `last_seen_at` are judged
against one clock a test controls.

`tools` is the `ToolCaller` PROTOCOL, never the concrete client: every node
test runs against a stub, and only `test_agent_mcp_client.py` opens a real MCP
session. The session's lifetime is the turn's, and
`run_turn` is what enters and exits it — no node owns a connection.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from helpdesk.agent_api.services.mcp_client import ToolCaller
from helpdesk.agent_api.services.model_gateway.client import ModelClient


@dataclass(frozen=True)
class TurnDeps:
    """The I/O one turn is given. Frozen: a node may use these, never swap them.

    `db` is already bound to the caller's tenant by `tenant_session`, which is
    what makes every read in this package tenant-scoped without a single node
    naming a tenant id (a project rule, and plan §5's third risk).
    """

    db: AsyncSession
    model: ModelClient
    tools: ToolCaller
    now: Callable[[], datetime]
