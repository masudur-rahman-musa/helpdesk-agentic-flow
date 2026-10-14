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

An earlier change ADDS the case at the bottom and moves nothing above it. The
surface it expects: ``run_turn`` opens the turn's trace and REBINDS the
telemetry context for the turn — ``tenant_id``, ``session_id`` and
``principal_kind`` read back off ``current_context()``, ``trace_id`` from
``helpdesk.obs.tracing.current_trace_id()`` — so every line emitted inside the
turn carries that turn's trace id and a line emitted outside one still carries
``null``. Both halves are one case, because a turn's trace id and the absence
of one after it are the same bind seen from two sides. It rides
``test_agent_turn.py``'s seeded database and stubbed gateway, so it is the
only case in this file that needs the compose stack.
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
from test_agent_turn import (
    COMPOSED_REPLY,
    EMPTY_PLAN,
    StagedGateway,
    StubToolCaller,
    TurnEnv,
    _citation,
    _page,
    _payroll_answer,
    _run_turn,
    pg,  # noqa: F401
    turn_env,  # noqa: F401
)

from helpdesk.agent_api.agent import graph as build_module
from helpdesk.agent_api.agent.graph import TurnDeps
from helpdesk.agent_api.agent.nodes.load_session import load_session as real_load_session
from helpdesk.agent_api.agent.state import TurnState
from helpdesk.agent_api.api.router import app
from helpdesk.agent_api.api.routes.admin import audit as admin_routes
from helpdesk.agent_api.services.tracing import configure_tracing
from helpdesk.shared.auth.jwt import TokenClaims, issue_token
from helpdesk.shared.core.observability.context import bind_context
from helpdesk.shared.core.observability.logging import FORBIDDEN_KEYS, REDACTED, JsonFormatter
from tests.agent_api.services.test_obs_tracing import TRACE_ID_PATTERN, FakeTraceClient

# Obviously fake, tests only; >= 32 bytes so the settings length gate passes.
TEST_JWT_SIGNING_PHRASE = "logging-wiring-test-jwt-secret-0123456789"

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
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SIGNING_PHRASE)

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
        TEST_JWT_SIGNING_PHRASE,
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


# --- an earlier change: the turn's trace id on every line of that turn -----------------


TRACE_LOGGER_NAME = "test.wiring.agent_api.tracing"
INSIDE_MARKER = "a record emitted inside the traced turn"
OUTSIDE_MARKER = "a record emitted outside any turn"


def test_a_record_logged_inside_a_traced_turn_carries_that_turns_trace_id(
    turn_env: TurnEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a `run_turn` that opens the trace but never rebinds the
    telemetry context: the trace and the logs would then be two records of the
    same turn with nothing joining them, which is the one question an
    on-call reader asks of both ("show me the logs of this trace").

    It also catches a rebind that keeps the trace id and DROPS the rest.
    `bind_context` REPLACES rather than merges, so an implementation calling
    it with the trace id alone silently unbinds the tenant and the session for
    the whole turn — the outer bind here is what makes that visible, and no
    other case in the tree binds a context around a turn.

    The record is emitted from a stand-in for `load_session`, the FIRST node,
    so it is inside the graph invocation and inside whatever the turn bound;
    a trace opened around only part of the turn fails here."""
    fake = FakeTraceClient()
    outer_tenant = str(turn_env.tenant_a.tenant_id)
    session_id = uuid.uuid4()

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger(TRACE_LOGGER_NAME)
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)

    async def _logging_load_session(state: TurnState, deps: TurnDeps) -> TurnState:
        logger.info(INSIDE_MARKER)
        return await real_load_session(state, deps)

    monkeypatch.setattr(build_module, "load_session", _logging_load_session)
    configure_tracing(client=fake)
    try:
        with bind_context(
            tenant_id=outer_tenant,
            session_id=str(session_id),
            principal_kind="employee",
        ):
            state = _run_turn(
                turn_env,
                turn_env.tenant_a,
                session_id=session_id,
                message="I need payroll access",
                gateway=StagedGateway([_payroll_answer(), EMPTY_PLAN, COMPOSED_REPLY]),
                tools=StubToolCaller(payload=_page(_citation("fake-access-policy", score=0.9))),
            )
        logger.info(OUTSIDE_MARKER)
    finally:
        configure_tracing()
        logger.removeHandler(handler)
        handler.close()

    # Premise, read off the turn and off the fake: one turn ran, and it opened
    # exactly one trace, so "that turn's trace id" names one value.
    assert state.session_id == session_id
    assert len(fake.roots) == 1

    lines: list[dict[str, Any]] = [
        json.loads(raw) for raw in stream.getvalue().splitlines() if raw.strip()
    ]
    inside = [line for line in lines if line["message"] == INSIDE_MARKER]
    outside = [line for line in lines if line["message"] == OUTSIDE_MARKER]
    assert len(inside) == 1
    assert len(outside) == 1

    assert inside[0]["trace_id"] == fake.roots[0].trace_id
    assert TRACE_ID_PATTERN.fullmatch(inside[0]["trace_id"]) is not None
    assert inside[0]["tenant_id"] == outer_tenant
    assert inside[0]["session_id"] == str(session_id)
    assert inside[0]["principal_kind"] == "employee"

    # The line outside every turn: the field is present and null, never a
    # stale id left bound by the turn that has ended.
    assert outside[0]["trace_id"] is None
