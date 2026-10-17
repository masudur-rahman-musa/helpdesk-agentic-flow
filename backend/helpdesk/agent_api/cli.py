"""Create LangGraph's checkpoint tables in the configured database.

    python -m helpdesk.agent_api.cli

The library owns and versions these tables, so an Alembic copy of its DDL
would drift. Running it from `make migrate`, not app startup, keeps several
starting processes from racing on `CREATE TABLE`.
"""

import asyncio
import sys

from helpdesk.agent_api.agent.checkpointer import build_pool, psycopg_dsn, setup_checkpointer
from helpdesk.shared.core.config import load_settings
from helpdesk.shared.core.errors import ToolError


async def _setup(dsn: str) -> None:
    """Create the tables and close the pool so no connections linger."""
    pool = build_pool(dsn)
    async with pool:
        await setup_checkpointer(pool)


def main() -> int:
    settings = load_settings()
    try:
        dsn = psycopg_dsn(settings.database_url)
    except ToolError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 1
    asyncio.run(_setup(dsn))
    print("checkpoint tables are up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
