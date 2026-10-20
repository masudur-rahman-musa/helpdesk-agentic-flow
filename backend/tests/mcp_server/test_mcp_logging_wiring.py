"""`pipeline.dispatch` binds the telemetry context.

Written before the bind exists. Surface under test: `dispatch` binds
`helpdesk.obs.context` from the identity `identity_from_header` already
produced — after verification, from the token and from nothing else,
so every line a tool body logs names the tenant and session of that
call.

Named `test_mcp_logging_wiring.py`, not `test_logging_wiring.py`:
pytest imports these non-package test files by basename, and
agent-api's file of that name would collide.
Same content, service-prefixed name, the way `test_mcp_health.py` already is.

Rides the session-scoped fixtures of this suite: the seeded
throwaway database and the dedicated Redis index are built once, and this test
adds one dispatch. The synthetic spec is built directly rather than
registered, the same way `test_mcp_pipeline.py` does it, so the pipeline's
binding can be tested with a handler that does nothing but log.
"""

import asyncio
import io
import json
import logging
import uuid
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel
from starlette.requests import Request

from helpdesk.mcp_server.tools import dispatch as pipeline
from helpdesk.mcp_server.tools.context import ToolContext
from helpdesk.mcp_server.tools.registry import ToolSpec
from helpdesk.shared.auth import rbac
from helpdesk.shared.core.observability.logging import FORBIDDEN_KEYS, REDACTED, JsonFormatter
from tests.support.mcp import (
    SeededMcp,
    bearer,
    token_with_claims,
)

# Every principal may call `get_me`, so RBAC cannot be what decides this case.
SPEC_TOOL = rbac.Tool.GET_ME

# Obviously fake identity, planted to prove it cannot reach a line.
PROBE_EMAIL = "mcp.logging.probe@fake.invalid"

LOGGER_NAME = "test.wiring.mcp_server"
MARKER = "a record emitted inside the tool body"


class NoArgs(BaseModel):
    pass


class Echo(BaseModel):
    ok: bool


def _request(headers: Mapping[str, str]) -> Request:
    """A Starlette request carrying exactly these headers, no server needed."""
    scope: dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "root_path": "",
        "headers": [(name.lower().encode(), value.encode()) for name, value in headers.items()],
        "client": ("127.0.0.1", 54321),
        "server": ("127.0.0.1", 8000),
    }
    return Request(scope)


def test_a_record_logged_during_a_dispatched_call_carries_its_tenant_and_session(
    mcp_env: SeededMcp,
    mcp_redis_url: str,
) -> None:
    """Catches an MCP server that configures logging but never binds, so every
    line of every tool call is context-free and no line can be attributed to
    the session or tenant it belongs to.

    A bind taken from anywhere but this call's verified identity dies here as
    well: the session id asserted is minted for this token alone, and the
    tenant is the one the token names.
    """
    session_id = uuid.uuid4()
    token = token_with_claims(mcp_env.tenant_a, {"sid": str(session_id)})

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger(LOGGER_NAME)
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)

    async def handler_body(ctx: ToolContext, args: Any) -> Echo:
        logger.info(
            MARKER,
            extra={"user_id": str(ctx.identity.user_id), "email": PROBE_EMAIL},
        )
        return Echo(ok=True)

    spec: ToolSpec[Any, Any] = ToolSpec(
        name=SPEC_TOOL.value,
        tool=SPEC_TOOL,
        description="synthetic spec, tests only",
        input_model=NoArgs,
        output_model=Echo,
        event_type="tool.succeeded",
        mutating=False,
        handler=handler_body,
    )

    async def scenario() -> Any:
        try:
            return await pipeline.dispatch(spec, _request(bearer(token)), {})
        finally:
            # The loop dies with this call; its pooled connections must not
            # outlive it (see `pipeline.close_shared_connections`).
            await pipeline.close_shared_connections()

    try:
        result = asyncio.run(scenario())
    finally:
        logger.removeHandler(handler)
        handler.close()

    # Premise, read off the result: the call really reached the tool body, so
    # the record below was emitted inside whatever `dispatch` had bound.
    assert result.is_error is False

    raw = stream.getvalue()
    assert raw.count("\n") == 1
    line: dict[str, Any] = json.loads(raw)

    assert line["message"] == MARKER
    assert line["tenant_id"] == str(mcp_env.tenant_a.tenant_id)
    assert line["session_id"] == str(session_id)

    # On the wired path: the user id the handler holds is redacted
    # in place, and neither identity value survives anywhere in the line.
    for key in ("user_id", "email"):
        assert key in FORBIDDEN_KEYS
        assert line[key] == REDACTED
    assert str(mcp_env.tenant_a.user_id) not in raw
    assert PROBE_EMAIL not in raw
