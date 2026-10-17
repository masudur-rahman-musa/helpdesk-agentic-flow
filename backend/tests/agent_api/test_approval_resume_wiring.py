"""The approval routes call the resume seam after a final decision.

What is under test (does NOT exist yet; every case below is red until it
lands):

``agent_api.api.routes.approvals`` gains a module-level seam::

    resume_after_decision = approvals_service.resume_after_decision

Inside ``approve`` and ``reject``, after ``proxy_tool`` returns the tool body,
the route reads the ``status`` field from that body. If the status is one of
the four decided values in ``gates.DECIDED_RESULTS`` (``approved``,
``executed``, ``rejected``, ``failed``) it calls the seam. A ``pending``
status -- a non-final partial approval -- does not trigger the seam at all.
A ``ResumeRefusedError`` from the seam is swallowed so the approver still gets
the tool's own 200 back.

Security: the seam is called with the PATH ``request_id`` and the route's own
``Request`` object; the decision body is read ONLY for its ``status``, never
for an id, so a wrong or lying body cannot redirect the resume.

Cost: no backing service. Every case stubs the decision tool caller and the
resume seam; nothing opens a socket, a pool or Redis. No ``db`` marker.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from helpdesk.agent_api.agent.resume import ResumeRefusedError
from helpdesk.agent_api.api.router import app
from helpdesk.agent_api.api.routes import approvals as approvals_routes
from helpdesk.agent_api.services import tool_proxy
from helpdesk.agent_api.services.mcp_client import ToolCaller
from helpdesk.shared.auth.jwt import issue_token
from helpdesk.shared.core.config import Settings

# ----- constants ----------------------------------------------------------------

# Obviously fake, tests only: nothing here connects to any host.
TEST_JWT_SIGNING_PHRASE = "resume-wiring-test-jwt-secret-0123456789"
MCP_URL = "http://mcp.invalid:8081/mcp"
DATABASE_URL = "postgresql+asyncpg://fake:fake@db.invalid:5432/fake_resume"
REDIS_URL = "redis://redis.invalid:6379/0"
QDRANT_URL = "http://qdrant.invalid:6333"

TENANT_ID = uuid.UUID("11111111-1111-5111-8111-111111111111")
APPROVER_ID = uuid.UUID("22222222-2222-5222-8222-222222222222")
DEPARTMENT_ID = uuid.UUID("33333333-3333-5333-8333-333333333333")
SESSION_ID = uuid.UUID("44444444-4444-5444-8444-444444444444")
REQUEST_ID = uuid.UUID("66666666-6666-5666-8666-666666666666")
OTHER_REQUEST_ID = uuid.UUID("6a6a6a6a-6a6a-5a6a-8a6a-6a6a6a6a6a6a")

APPROVE_PATH = f"/approvals/{REQUEST_ID}/approve"
REJECT_PATH = f"/approvals/{REQUEST_ID}/reject"

# ----- scripted seam helpers ---------------------------------------------------


class ScriptedToolCaller:
    """Returns one scripted payload per call, in order.

    Same shape as the one in ``test_approval_routes.py``.
    """

    def __init__(self, *answers: Mapping[str, Any]) -> None:
        self._answers = list(answers)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.entered = 0

    async def __aenter__(self) -> ToolCaller:
        self.entered += 1
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def call_tool(self, name: str, arguments: BaseModel) -> Mapping[str, Any]:
        self.calls.append((name, arguments.model_dump(mode="json")))
        index = len(self.calls) - 1
        if index >= len(self._answers):
            raise AssertionError(
                f"the route made {len(self.calls)} tool calls; "
                f"only {len(self._answers)} were scripted"
            )
        return self._answers[index]


class CallerSpy:
    """Records the bearer ``build_tool_caller`` was handed, and hands back a stub."""

    def __init__(self, caller: ToolCaller) -> None:
        self._caller = caller
        self.bearer_tokens: list[str] = []

    def build(self, settings: Settings, bearer_token: str) -> ToolCaller:
        self.bearer_tokens.append(bearer_token)
        return self._caller


# ----- fixtures ----------------------------------------------------------------


@pytest.fixture
def approvals_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Obviously fake settings. No lifespan is entered."""
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setenv("QDRANT_URL", QDRANT_URL)
    monkeypatch.setenv("JWT_SECRET", TEST_JWT_SIGNING_PHRASE)
    monkeypatch.setenv("MCP_URL", MCP_URL)


def _token() -> str:
    """One verified-looking token for the fake approver and conversation."""
    return issue_token(
        {
            "sub": str(APPROVER_ID),
            "tenant_id": str(TENANT_ID),
            "department_id": str(DEPARTMENT_ID),
            "principals": ["manager"],
            "sid": str(SESSION_ID),
        },
        TEST_JWT_SIGNING_PHRASE,
        datetime.now(UTC),
    )


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ----- transport helpers -------------------------------------------------------


def _drive(scenario: Callable[[httpx.AsyncClient], Awaitable[httpx.Response]]) -> httpx.Response:
    async def runner() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await scenario(client)

    return asyncio.run(runner())


def _request(
    *,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    caller: ToolCaller,
    body: Mapping[str, Any] | None = None,
) -> tuple[httpx.Response, CallerSpy]:
    """One call against the app with the tool caller replaced at the seam.

    The lifespan is NOT entered: the resume seam is also patched by each test,
    so nothing here reads ``app.state``.
    """
    spy = CallerSpy(caller)
    monkeypatch.setattr(tool_proxy, "build_tool_caller", spy.build)

    async def scenario(client: httpx.AsyncClient) -> httpx.Response:
        return await client.request(
            method,
            path,
            headers=_auth(_token()),
            json=None if body is None else dict(body),
        )

    return _drive(scenario), spy


# ----- the wiring tests --------------------------------------------------------


def test_a_final_approval_resumes_the_filed_turn(
    approvals_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a route that never calls the resume seam after a final approval,
    leaving the requester's chat turn paused with no reply.
    """
    calls: list[tuple[uuid.UUID, str]] = []

    async def spy(request: object, request_id: uuid.UUID, status: str) -> None:
        calls.append((request_id, status))

    monkeypatch.setattr(approvals_routes, "resume_after_decision", spy)
    caller = ScriptedToolCaller(
        {"request_id": str(REQUEST_ID), "status": "approved", "chain_complete": True}
    )

    response, _c = _request(
        monkeypatch=monkeypatch,
        method="POST",
        path=APPROVE_PATH,
        caller=caller,
        body={"comment": "Obviously fake comment."},
    )

    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0][0] == REQUEST_ID


def test_a_rejection_resumes_the_filed_turn(
    approvals_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a route that never calls the resume seam after a rejection,
    leaving the requester with no result in chat.
    """
    calls: list[tuple[uuid.UUID, str]] = []

    async def spy(request: object, request_id: uuid.UUID, status: str) -> None:
        calls.append((request_id, status))

    monkeypatch.setattr(approvals_routes, "resume_after_decision", spy)
    caller = ScriptedToolCaller({"request_id": str(REQUEST_ID), "status": "rejected"})

    response, _c = _request(
        monkeypatch=monkeypatch,
        method="POST",
        path=REJECT_PATH,
        caller=caller,
        body={"reason": "Obviously fake reason."},
    )

    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0][0] == REQUEST_ID


def test_a_failed_execution_still_resumes_so_the_requester_hears_it_failed(
    approvals_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a route that skips the resume for a ``failed`` status, leaving
    the requester believing the grant is pending when the fresh policy check
    refused it.
    """
    calls: list[tuple[uuid.UUID, str]] = []

    async def spy(request: object, request_id: uuid.UUID, status: str) -> None:
        calls.append((request_id, status))

    monkeypatch.setattr(approvals_routes, "resume_after_decision", spy)
    caller = ScriptedToolCaller({"request_id": str(REQUEST_ID), "status": "failed"})

    response, _c = _request(
        monkeypatch=monkeypatch,
        method="POST",
        path=APPROVE_PATH,
        caller=caller,
        body={"comment": "Obviously fake comment."},
    )

    assert response.status_code == 200
    assert len(calls) == 1


def test_the_resume_acts_on_the_path_request_id_not_the_body(
    approvals_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a route that reads the ``request_id`` to resume from the tool body
    rather than the URL path, which would let a wrong or lying body redirect the
    resume to a different request.
    """
    calls: list[tuple[uuid.UUID, str]] = []

    async def spy(request: object, request_id: uuid.UUID, status: str) -> None:
        calls.append((request_id, status))

    monkeypatch.setattr(approvals_routes, "resume_after_decision", spy)
    # Body carries a DIFFERENT request_id to the one in the path.
    caller = ScriptedToolCaller(
        {"request_id": str(OTHER_REQUEST_ID), "status": "approved", "chain_complete": True}
    )

    response, _c = _request(
        monkeypatch=monkeypatch,
        method="POST",
        path=APPROVE_PATH,
        caller=caller,
        body={"comment": "Obviously fake comment."},
    )

    assert response.status_code == 200
    assert len(calls) == 1
    # Must use the path id, not the body id.
    assert calls[0][0] == REQUEST_ID
    assert calls[0][0] != OTHER_REQUEST_ID


def test_a_non_final_approval_resumes_nothing(
    approvals_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a route that calls the resume seam for a ``pending`` status, which
    would try to resume a turn that is still waiting for more approvals.
    """
    calls: list[tuple[uuid.UUID, str]] = []

    async def spy(request: object, request_id: uuid.UUID, status: str) -> None:
        calls.append((request_id, status))

    monkeypatch.setattr(approvals_routes, "resume_after_decision", spy)
    # "pending" means one seat was filled but the chain is not yet complete.
    caller = ScriptedToolCaller(
        {"request_id": str(REQUEST_ID), "status": "pending", "chain_complete": False}
    )

    response, _c = _request(
        monkeypatch=monkeypatch,
        method="POST",
        path=APPROVE_PATH,
        caller=caller,
        body={"comment": "Obviously fake comment."},
    )

    assert response.status_code == 200
    assert len(calls) == 0


def test_a_refused_resume_still_answers_the_approver_the_decision(
    approvals_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a route that lets ``ResumeRefusedError`` propagate, which would
    give the approver an error response instead of the decision confirmation they
    earned.
    """
    tool_body = {"request_id": str(REQUEST_ID), "status": "approved", "chain_complete": True}

    async def raising_spy(request: object, request_id: uuid.UUID, status: str) -> None:
        raise ResumeRefusedError("no paused turn for this request")

    monkeypatch.setattr(approvals_routes, "resume_after_decision", raising_spy)
    caller = ScriptedToolCaller(tool_body)

    response, _c = _request(
        monkeypatch=monkeypatch,
        method="POST",
        path=APPROVE_PATH,
        caller=caller,
        body={"comment": "Obviously fake comment."},
    )

    assert response.status_code == 200
    assert response.json() == tool_body
