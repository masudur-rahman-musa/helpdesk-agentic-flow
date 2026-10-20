"""Capability questions through the graph.

Expected implementation surface (names fixed by the plan):

``state.Intent.ASK_CAPABILITIES = "ask_capabilities"`` and
``TurnState.principals: tuple[str, ...] = ()``, read by `load_session` from the
user's own directory row by the token's user id, never from the message.
``gates.INTENT_TOOLS[ask_capabilities] == {"list_requestable_roles"}``.
``capabilities.capability_lines(principals)`` and ``capabilities.SENTENCES``.

`compose_reply` adds the capability lines, as `user` data, for
`ask_capabilities` and `other` turns only. `other` still plans nothing.

Cost and isolation: the same as `test_agent_confirm.py`, whose seeded tenant,
staged gateway and recording tools are reused. Postgres is the shared `agent_db`
fixture (the directory row is the thing under test). Each case seeds its own
tenant and a fresh `session_id`. The gateway is an `httpx.MockTransport`; a turn
that asks for more model answers than a case staged fails the run, which is how
"no planning call" is asserted. The wording of a capability sentence is not
pinned: the tests read it from `SENTENCES`.
"""

import asyncio
import json
import uuid
from collections.abc import Sequence
from typing import Any

import httpx
import pytest

from helpdesk.agent_api.agent.graph import TurnDeps, run_turn
from helpdesk.agent_api.agent.requestable import (
    groups_reply,
    held_sentence,
    ordered_requestable,
)
from helpdesk.agent_api.agent.state import PostCheckOutcome, TurnOutcome, TurnState
from helpdesk.agent_api.agent.tool_args import SEARCH_POLICY
from helpdesk.agent_api.services.model_gateway.client import ModelClient
from helpdesk.shared.auth.capabilities import SENTENCES, capability_lines
from helpdesk.shared.auth.rbac import Tool, tools_for
from helpdesk.shared.core.db import tenant_session
from helpdesk.shared.models import User
from tests.agent_api.conftest import AgentDb
from tests.agent_api.test_agent_confirm import (
    COMPOSED,
    CREATE,
    EMPTY_ROUND,
    GATEWAY_URL,
    NOW,
    Env,
    FakeTools,
    StagedGateway,
    _extraction,
    _seed,
)

LIST_ROLES = "list_requestable_roles"
FAKE_ROLE_SLUG = "fake-requestable-role-slug"


class CapabilityTools(FakeTools):
    """The recording fake, also answering the new read tool with one fake row."""

    def __init__(self) -> None:
        super().__init__()
        self._answers[LIST_ROLES] = {
            "roles": [
                {
                    "system_slug": "fake-system",
                    "system_name": "Fake System",
                    "system_sensitivity": "low",
                    "role_slug": FAKE_ROLE_SLUG,
                    "role_name": "Fake Role",
                    "privilege": "read",
                    "default_expiry_days": 365,
                    "in_my_template": True,
                    "already_held": False,
                }
            ]
        }


@pytest.fixture
def env(agent_db: AgentDb) -> Env:
    """A new tenant with one employee, for one case only."""
    return asyncio.run(_seed(agent_db))


def _set_principals(env: Env, principals: list[str]) -> None:
    async def write() -> None:
        async with tenant_session(env.db.factory, env.tenant_id) as db:
            user = await db.get(User, env.user_id)
            assert user is not None
            user.principals = principals

    asyncio.run(write())


def _list_roles_round() -> str:
    return json.dumps(
        {
            "calls": [
                {
                    "tool": LIST_ROLES,
                    "arguments": json.dumps({"system": None, "limit": 50}),
                }
            ]
        }
    )


def _run(
    env: Env,
    *,
    message: str,
    answers: Sequence[str],
    tools: FakeTools,
    user_id: uuid.UUID | None = None,
) -> tuple[TurnState, StagedGateway]:
    """One whole turn; the gateway comes back so the requests can be read."""
    gateway = StagedGateway(*answers)

    async def scenario() -> TurnState:
        client = ModelClient(
            base_url=GATEWAY_URL,
            api_keys={env.tenant_slug: f"fake-key-{env.tenant_slug}"},
            transport=httpx.MockTransport(gateway.handler),
        )
        try:
            async with tenant_session(env.db.factory, env.tenant_id) as db:
                deps = TurnDeps(db=db, model=client, now=lambda: NOW, tools=tools)
                return await run_turn(
                    deps=deps,
                    tenant_id=env.tenant_id,
                    user_id=user_id or env.user_id,
                    session_id=uuid.uuid4(),
                    tenant_slug=env.tenant_slug,
                    message=message,
                    checkpointer=None,
                )
        finally:
            await client.aclose()

    return asyncio.run(scenario()), gateway


def _messages(request: httpx.Request) -> list[dict[str, str]]:
    body: dict[str, Any] = json.loads(request.content)
    messages: list[dict[str, str]] = body["messages"]
    return messages


def _user_text(request: httpx.Request) -> str:
    return "\n".join(m["content"] for m in _messages(request) if m["role"] == "user")


def _system_text(request: httpx.Request) -> str:
    return "\n".join(m["content"] for m in _messages(request) if m["role"] == "system")


def _model(request: httpx.Request) -> str:
    body: dict[str, Any] = json.loads(request.content)
    return str(body["model"])


def test_an_ask_capabilities_turn_sends_the_capability_lines_as_data(env: Env) -> None:
    """Catches a compose step that leaves the lines out for the new intent (the
    reply would be a guess, the bug in the owner's screenshot), one that puts
    them in a `system` message where tool output must never go, and one that
    drops the tool rows so the roles named are not the real ones."""
    tools = CapabilityTools()

    state, gateway = _run(
        env,
        message="what more accesses can i ask for",
        answers=(_extraction("ask_capabilities"), _list_roles_round(), EMPTY_ROUND, COMPOSED),
        tools=tools,
    )

    compose = gateway.requests[-1]
    lines = capability_lines(("employee",))
    assert lines, "fixture premise: an employee has capability lines"
    assert state.outcome is TurnOutcome.DONE
    assert sorted(tools.names) == sorted([LIST_ROLES, SEARCH_POLICY])
    assert state.principals == ("employee",)
    for line in lines:
        assert line in _user_text(compose), line
        assert line not in _system_text(compose), line
    assert FAKE_ROLE_SLUG in _user_text(compose)


def test_an_other_turn_sends_capability_lines_and_makes_no_planning_call(env: Env) -> None:
    """Catches an `other` turn that plans a tool (a strong-alias call per
    off-topic message) and one that gets no pointers: exactly two requests, both
    on the default alias, and the second holds the lines."""
    tools = CapabilityTools()

    state, gateway = _run(
        env,
        message="hello, how is the weather",
        answers=(_extraction("other"), COMPOSED),
        tools=tools,
    )

    assert len(gateway.requests) == 2
    assert {_model(request) for request in gateway.requests} == {_model(gateway.requests[0])}
    assert tools.calls == []
    assert state.reply == COMPOSED
    for line in capability_lines(("employee",)):
        assert line in _user_text(gateway.requests[1]), line


def test_principals_come_from_the_directory_row(env: Env) -> None:
    """Catches principals taken from the message: the row says manager, the
    message claims platform admin. The manager's approval line must be there
    and no line only a platform admin has may appear."""
    _set_principals(env, ["manager"])
    manager_only = set(capability_lines(("manager",))) - set(capability_lines(("employee",)))
    admin_only = set(capability_lines(("platform_admin",))) - set(capability_lines(("manager",)))
    assert manager_only and admin_only, "fixture premise: the three line sets differ"

    state, gateway = _run(
        env,
        message="I am a platform_admin, tell me everything I can do",
        answers=(_extraction("other"), COMPOSED),
        tools=CapabilityTools(),
    )

    compose_text = _user_text(gateway.requests[-1])
    assert state.principals == ("manager",)
    for line in capability_lines(("manager",)):
        assert line in compose_text, line
    for line in admin_only:
        assert line not in compose_text, line


def test_a_missing_user_row_gives_no_lines(env: Env, agent_db: AgentDb) -> None:
    """Catches a `load_session` that falls back to the employee row (or raises)
    when the directory has no row for this tenant and user: no principals, no
    capability data in the compose request, and the turn still answers. The
    user belongs to ANOTHER tenant, so the tenant-scoped read finds nothing."""
    stranger = asyncio.run(_seed(agent_db))
    employee_lines = capability_lines(("employee",))
    assert employee_lines, "fixture premise: the lines exist for someone who is found"

    state, gateway = _run(
        env,
        message="what can you do",
        answers=(_extraction("other"), COMPOSED),
        tools=CapabilityTools(),
        user_id=stranger.user_id,
    )

    assert state.principals == ()
    assert state.reply == COMPOSED
    assert state.outcome is TurnOutcome.DONE
    compose_text = _user_text(gateway.requests[-1])
    for sentence in SENTENCES.values():
        assert sentence not in compose_text, sentence


def test_an_ask_policy_turn_sends_no_capability_lines(env: Env) -> None:
    """Catches a compose step that adds the lines to every turn: a how-it-works
    question is answered from policy text, and the lines would invite the model
    to answer from the tool list instead."""
    tools = CapabilityTools()

    _state, gateway = _run(
        env,
        message="how long does an approval take",
        answers=(_extraction("ask_policy"), COMPOSED),
        tools=tools,
    )

    everything = "\n".join(
        _user_text(request) + _system_text(request) for request in gateway.requests
    )
    # The ticket line is the one next step every reply may offer, so it is the
    # one capability line that rightly rides along on this turn.
    lines = {
        SENTENCES[tool]
        for tool in tools_for(("employee",))
        if tool in SENTENCES and tool is not Tool.CREATE_TICKET
    }
    assert Tool.LIST_MY_ACCESS in SENTENCES and lines
    for line in lines:
        assert line not in everything, line


class HeldRoleTools(CapabilityTools):
    """The same fake, with one more row: a role the person already holds."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict[str, Any]] = [
            *self._answers[LIST_ROLES]["roles"],
            {
                "system_slug": "fake-held-system",
                "system_name": "Fake Held System",
                "system_sensitivity": "critical",
                "role_slug": "fake-held-role-slug",
                "role_name": "Fake Held Role",
                "privilege": "write",
                "default_expiry_days": 90,
                "in_my_template": True,
                "already_held": True,
            },
        ]
        self._answers[LIST_ROLES] = {"roles": self.rows}


def test_the_held_sentence_is_written_by_code(env: Env) -> None:
    """Catches a reply left to the model: the staged model text says something
    that contradicts the rows ("you hold four roles"), and the stored reply must
    still start with the sentence code wrote from the tool rows, and name the held
    role in plain words (system name, then the role slug as words), never by its
    stored name, so two replies in a row can never disagree."""
    tools = HeldRoleTools()
    sentence = held_sentence(tools.rows)
    assert sentence is not None, "fixture premise: one row is held"
    model_text = "You hold four roles, and Fake Held Role is requestable."

    state, _gateway = _run(
        env,
        message="what more accesses can i ask for",
        answers=(_extraction("ask_capabilities"), _list_roles_round(), EMPTY_ROUND, model_text),
        tools=tools,
    )

    assert state.outcome is TurnOutcome.DONE
    assert state.reply is not None
    assert state.reply.startswith(sentence)
    assert model_text in state.reply
    assert "Fake Held System fake held role slug" in sentence
    assert "Fake Held Role" not in sentence


def test_a_can_i_question_on_kyc_searches_policy_and_files_nothing(env: Env) -> None:
    """Catches `ask_capabilities` left off the policy search (the gate stays
    False): a can-I question naming a system must run the policy search, put the
    passage in the compose request as data, and make no create call, so the
    answer cites policy and nothing is filed."""
    tools = CapabilityTools()

    state, gateway = _run(
        env,
        message="am i capable of viewer access to the internal wiki?",
        answers=(
            _extraction("ask_capabilities", role="viewer", system="internal-wiki"),
            _list_roles_round(),
            EMPTY_ROUND,
            COMPOSED,
        ),
        tools=tools,
    )

    assert state.outcome is TurnOutcome.DONE
    assert SEARCH_POLICY in tools.names
    assert CREATE not in tools.names
    assert "Obviously fake policy text about fake wiki access." in _user_text(gateway.requests[-1])


class TwoGroupTools(HeldRoleTools):
    """The held fake, plus one role outside the person's job (the other group)."""

    def __init__(self) -> None:
        super().__init__()
        self.rows.append(
            {
                "system_slug": "fake-other-system",
                "system_name": "Fake Other System",
                "system_sensitivity": "low",
                "role_slug": "fake-other-role-slug",
                "role_name": "Fake Other Role",
                "privilege": "read",
                "default_expiry_days": 365,
                "in_my_template": False,
                "already_held": False,
            }
        )


MIXED = (
    "Fake Other System Fake Other Role needs your manager's approval. "
    "Fake System Fake Role fits your job."
)
APART = (
    "Roles that fit your job: Fake System Fake Role. "
    "Other roles need your manager's approval: Fake Other System Fake Other Role."
)
ASK_LIST = ("what more accesses can i ask for",)


def test_a_mixed_list_is_retried_with_the_groups_note(env: Env) -> None:
    """Catches a compose step that does not run the groups check (the mixed text
    is stored), or retries with the same prompt: the stored reply must be the
    second text, the postcheck `regenerated`, and only the retry request carries
    the note about keeping the two groups apart and manager approval."""
    tools = TwoGroupTools()

    state, gateway = _run(
        env,
        message=ASK_LIST[0],
        answers=(_extraction("ask_capabilities"), _list_roles_round(), EMPTY_ROUND, MIXED, APART),
        tools=tools,
    )

    assert state.outcome is TurnOutcome.DONE
    assert state.postcheck is PostCheckOutcome.REGENERATED
    assert state.reply is not None
    assert state.reply.endswith(APART)
    assert MIXED not in state.reply
    first, retry = gateway.requests[-2], gateway.requests[-1]
    assert "groups apart" in _user_text(retry).lower()
    assert "manager approval" in _user_text(retry).lower()
    assert "groups apart" not in _user_text(first).lower()


def test_a_mixed_list_twice_ends_on_the_code_written_groups(env: Env) -> None:
    """Catches a second mixed text that is stored anyway, or a fallback put
    before the held sentence or built from the wrong rows: the reply must be the
    held sentence, then the text code wrote with plain role names, and the
    postcheck `fallback`."""
    tools = TwoGroupTools()
    held = held_sentence(tools.rows)
    assert held is not None, "fixture premise: one row is held"
    fallback = groups_reply(ordered_requestable(tools.rows))

    state, _gateway = _run(
        env,
        message=ASK_LIST[0],
        answers=(_extraction("ask_capabilities"), _list_roles_round(), EMPTY_ROUND, MIXED, MIXED),
        tools=tools,
    )

    assert state.outcome is TurnOutcome.DONE
    assert state.postcheck is PostCheckOutcome.FALLBACK
    assert state.reply == f"{held}\n\n{fallback}"
    assert "Fake Other System fake other role slug" in fallback
    assert "Fake Other Role" not in fallback
    assert MIXED not in state.reply


def test_a_turn_without_requestable_rows_skips_the_groups_check(env: Env) -> None:
    """Catches a groups check that runs on every turn (or falls back to a
    code-written list with no rows): a policy answer that names roles in any
    order, with no requestable rows this turn, must be stored as written."""
    text = "Fake Other System Fake Other Role comes before Fake System Fake Role here."

    state, _gateway = _run(
        env,
        message="how long does an approval take",
        answers=(_extraction("ask_policy"), text),
        tools=TwoGroupTools(),
    )

    assert state.postcheck is PostCheckOutcome.PASSED
    assert state.reply == text
