"""The turn's gateway user id rides every model request of the turn.

Expected implementation surface: ``run_loop_turn`` and ``stream_loop_turn``
take ``gateway_user: str | None = None``; ``TurnState.gateway_user`` holds it;
each model step passes it to the client as ``user=``.

The gateway, tools and chat row are the fakes of ``test_agent_loop.py``. No
database, no socket, no waiting.
"""

import asyncio
import re

import httpx

from helpdesk.agent_api.agent.loop import run_loop_turn
from helpdesk.agent_api.agent.state import TurnState
from helpdesk.agent_api.services.model_gateway.client import ModelClient
from tests.agent_api.test_agent_loop import (
    API_KEYS,
    GATEWAY_URL,
    MESSAGE,
    SESSION_ID,
    TENANT_ID,
    TENANT_SLUG,
    USER_ID,
    Clock,
    FakeStore,
    FakeTools,
    ScriptedGateway,
    _deps,
    answer_body,
    wire_call,
)

GATEWAY_USER = "u_8f3a1c0e5b7d4a2f9e6c3b1a0d8f7e6c"


def _run(gateway: ScriptedGateway, gateway_user: str | None) -> TurnState:
    """One loop turn over the fakes, started with the given gateway user."""

    async def scenario() -> TurnState:
        model = ModelClient(
            base_url=GATEWAY_URL,
            api_keys=API_KEYS,
            transport=httpx.MockTransport(gateway.handler),
        )
        try:
            return await run_loop_turn(
                deps=_deps(model, FakeTools({"list_my_requests": {"requests": []}}), Clock()),
                tenant_id=TENANT_ID,
                user_id=USER_ID,
                session_id=SESSION_ID,
                tenant_slug=TENANT_SLUG,
                message=MESSAGE,
                store=FakeStore(),
                gateway_user=gateway_user,
            )
        finally:
            await model.aclose()

    return asyncio.run(scenario())


def _two_steps() -> ScriptedGateway:
    return ScriptedGateway(
        answer_body(None, wire_call("call_a", "list_my_requests", {})),
        answer_body("You have nothing open."),
    )


def test_every_step_of_a_turn_sends_the_turns_user() -> None:
    """Catches a loop that passes the user to the first model step only (or to
    none, or builds a different id), so the later steps of a turn would be
    charged to nobody on the gateway.

    A lookup then an answer makes two model requests; both must carry the id
    the turn was started with, and it is the well formed one.
    """
    gateway = _two_steps()

    state = _run(gateway, GATEWAY_USER)

    assert state.gateway_user == GATEWAY_USER
    assert re.fullmatch(r"u_[0-9a-f]{32}", GATEWAY_USER)
    assert len(gateway.headers) == 2
    assert [h.get("X-User") for h in gateway.headers] == [GATEWAY_USER, GATEWAY_USER]


def test_a_turn_with_no_user_sends_no_user_header() -> None:
    """Catches a loop that sends an empty or "None" `X-User` when the turn has no
    gateway user, instead of leaving the header out.

    The turn is started with none; neither of its two requests may carry the key.
    """
    gateway = _two_steps()

    state = _run(gateway, None)

    assert state.gateway_user is None
    assert len(gateway.headers) == 2
    assert all("X-User" not in h for h in gateway.headers)
