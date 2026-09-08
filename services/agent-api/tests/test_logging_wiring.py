"""An earlier change section 6: agent-api binds the telemetry context per request.

Written before the middleware exists. Surface under test:

``agent_api.main.app`` carries a middleware that, for every request, binds
``helpdesk.obs.context`` from a BEST-EFFORT ``verify_token`` of the bearer
header — it never refuses and never authorizes, it only says which tenant and
session the lines of this request belong to. Authorization stays where it is,
on the route.

The request driven here is ``GET /admin/audit/verify`` with a valid token
whose principals are outside ``AUDIT_VERIFY_PRINCIPALS``, so the route answers
403 before it opens a database session: this test needs no backing service.
``_is_permitted`` is wrapped with a logging stand-in so there is a record
emitted INSIDE the request, at a point every guard before it has really run.

The line is captured through a `JsonFormatter` handler attached here rather
than through the service's own handler: what is under test is the BIND, and
`test_obs_logging.py` already owns the formatter.
"""

import asyncio
import io
import json
import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from agent_api import admin_routes
from agent_api.main import app
from helpdesk.auth.jwt import TokenClaims, issue_token
from helpdesk.obs.logging import FORBIDDEN_KEYS, REDACTED, JsonFormatter

# Obviously fake, tests only; >= 32 bytes so the settings length gate passes.
TEST_JWT_SECRET = "logging-wiring-test-jwt-secret-0123456789"

# Obviously fake identity, planted to prove it cannot reach a line.
PROBE_EMAIL = "logging.wiring.probe@fake.invalid"

LOGGER_NAME = "test.wiring.agent_api"
MARKER = "a record emitted inside the request"


def _get(path: str, headers: Mapping[str, str]) -> httpx.Response:
    async def _run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path, headers=dict(headers))

    return asyncio.run(_run())


def test_a_record_logged_during_a_request_carries_that_call_s_tenant_and_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a service that calls `configure_logging()` but never binds the
    context, so every line is context-free: the logs parse, look structured,
    and answer none of the questions ("what happened on this session, for this
    tenant") the whole of the design is built around.

    A bind that read identity from somewhere other than this call's own token
    dies here too: the tenant and session asserted are the ones minted for
    this request and nothing else in the process holds them.
    """
    tenant_id = uuid.uuid4()
    session_id = uuid.uuid4()
    user_id = uuid.uuid4()
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SECRET)

    token = issue_token(
        {
            "sub": str(user_id),
            "tenant_id": str(tenant_id),
            "department_id": str(uuid.uuid4()),
            # Outside AUDIT_VERIFY_PRINCIPALS, so the route refuses with 403
            # before it reaches Postgres.
            "principals": ["employee"],
            "sid": str(session_id),
        },
        TEST_JWT_SECRET,
        datetime.now(UTC),
    )

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger(LOGGER_NAME)
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)

    def _logging_is_permitted(claims: TokenClaims) -> bool:
        logger.info(MARKER, extra={"user_id": str(user_id), "email": PROBE_EMAIL})
        return False

    monkeypatch.setattr(admin_routes, "_is_permitted", _logging_is_permitted)

    try:
        response = _get("/admin/audit/verify", {"Authorization": f"Bearer {token}"})
    finally:
        logger.removeHandler(handler)
        handler.close()

    # Premise, read off the response: the request really reached the route, so
    # the record below was emitted inside the middleware's bound block.
    assert response.status_code == 403

    raw = stream.getvalue()
    assert raw.count("\n") == 1
    line: dict[str, Any] = json.loads(raw)

    assert line["message"] == MARKER
    assert line["tenant_id"] == str(tenant_id)
    assert line["session_id"] == str(session_id)

    # the design on the wired path: identity handed in by a careless caller is
    # redacted in place, and neither value survives anywhere in the line.
    for key in ("user_id", "email"):
        assert key in FORBIDDEN_KEYS
        assert line[key] == REDACTED
    assert str(user_id) not in raw
    assert PROBE_EMAIL not in raw
