"""Create LangGraph's checkpoint tables in the configured database.

    uv run python scripts/checkpointer_setup.py

Run beside `alembic upgrade head` from `make migrate`, because that is where
every other schema change lands. An earlier change decision B: the tables are the
library's own and it versions them itself, so an Alembic revision copying its
DDL would drift on every upgrade; running the library's `setup()` from the
migrate target rather than from application startup is also what keeps several
starting processes from racing on `CREATE TABLE`.

Argument parsing there is none, and no logic either: everything happens in
`helpdesk.agent_api.checkpointer`, so a later admin route can do exactly what this
script does by calling the same two functions. Exits 1 on a typed refusal,
printing its code and message — the case being a `DATABASE_URL` the
checkpointer will not convert.
"""

import asyncio
import sys

from helpdesk.agent_api.checkpointer import build_pool, psycopg_dsn, setup_checkpointer
from helpdesk.shared.core.settings import load_settings
from helpdesk.shared.errors import ToolError


async def _setup(dsn: str) -> None:
    """Open the pool, create the tables, close the pool.

    The explicit close is the point of the `async with`: this script is run
    from a migrate target that may be followed by anything, and a pool left
    open would hold its connections until the interpreter exited.
    """
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
