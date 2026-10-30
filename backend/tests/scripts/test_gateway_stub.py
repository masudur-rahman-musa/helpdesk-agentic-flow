"""The e2e gateway stub answers what the agent loop's model client sends.

The stub is driven through the real `ModelClient` and `RedactionClient` over
an in-process transport, so a shape the clients cannot read fails here, not
in a browser run three services away. No network, no database.
"""

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest

from helpdesk.agent_api.agent.loop_tools import offered_tools
from helpdesk.agent_api.agent.tool_args import IDEMPOTENCY_KEY, TOOL_INPUTS
from helpdesk.agent_api.services.model_gateway.client import (
    ModelAlias,
    ModelClient,
    ModelUnavailableError,
    ToolCall,
    ToolCallAnswer,
)
from helpdesk.mcp_server.services.rag.redaction_client import RedactionClient
from scripts.e2e import gateway_stub

TENANT = "nordlicht"
KEYS = {TENANT: "stub-test-key"}
WIKI_ASK = "Please give me wiki.viewer access to the internal wiki."
ME = str(uuid.uuid4())
ROLE = str(uuid.uuid4())
LEAVER = str(uuid.uuid4())


def _client() -> ModelClient:
    transport = httpx.ASGITransport(app=gateway_stub.build_app())
    return ModelClient(base_url="http://stub", api_keys=KEYS, transport=transport)


def _tools(principals: tuple[str, ...] = ("employee",)) -> list[dict[str, Any]]:
    return [tool.wire() for tool in offered_tools(principals)]


def _ask(
    messages: list[dict[str, Any]],
    *,
    answer_only: bool = False,
    principals: tuple[str, ...] = ("employee",),
) -> ToolCallAnswer:
    async def run() -> ToolCallAnswer:
        client = _client()
        try:
            return await client.complete_with_tools(
                messages,
                _tools(principals),
                alias=ModelAlias.AGENT_DEFAULT,
                tenant_slug=TENANT,
                answer_only=answer_only,
            )
        finally:
            await client.aclose()

    return asyncio.run(run())


def _assistant(calls: tuple[ToolCall, ...]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }
            for call in calls
        ],
    }


def _results(calls: tuple[ToolCall, ...], payloads: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"role": "tool", "tool_call_id": call.id, "content": json.dumps(payloads.get(call.name))}
        for call in calls
    ]


def _first_turn() -> list[dict[str, Any]]:
    return [{"role": "system", "content": "prompt"}, {"role": "user", "content": WIKI_ASK}]


def test_the_first_step_asks_for_lookups_as_tool_calls() -> None:
    """Catches a stub still answering in the old JSON plan format: the loop
    reads tool calls off the message, and an answer with none ends the turn."""
    answer = _ask(_first_turn())

    assert [call.name for call in answer.calls] == ["search_policy", "find_role", "get_me"]
    assert answer.calls[1].arguments == {"words": "wiki.viewer"}


def test_the_second_step_files_with_ids_from_this_turns_results() -> None:
    """Catches a change whose ids are not the ones the lookups returned; the
    loop refuses to hold such a change."""
    messages = _first_turn()
    first = _ask(messages).calls
    messages += [
        _assistant(first),
        *_results(
            first,
            {
                "search_policy": {"citations": []},
                "find_role": {"roles": [{"role_id": ROLE, "name": "Wiki viewer"}]},
                "get_me": {"user_id": ME},
            },
        ),
    ]

    (call,) = _ask(messages).calls

    assert call.name == "create_access_request"
    assert call.arguments["target_user_id"] == ME
    assert call.arguments["role_id"] == ROLE
    TOOL_INPUTS[call.name].model_validate({**call.arguments, IDEMPOTENCY_KEY: "k"})


def test_an_answer_only_step_gets_text_and_no_calls() -> None:
    answer = _ask(_first_turn(), answer_only=True)

    assert answer.calls == ()
    assert answer.text


def test_the_step_after_a_yes_gets_text() -> None:
    """Catches a stub that replays the first turn's calls on the yes turn."""
    messages = [
        *_first_turn(),
        {"role": "assistant", "content": "I'll file this. Is that right?"},
        {"role": "user", "content": "Yes, that is right."},
    ]

    answer = _ask(messages, answer_only=True)

    assert answer.calls == ()
    assert answer.text


def test_the_offboard_step_reads_the_found_person() -> None:
    messages = [
        {"role": "system", "content": "prompt"},
        {"role": "user", "content": "Pat Example is leaving today, offboard them."},
    ]
    found = (ToolCall(id="call_0_0", name="find_person", arguments={"words": "x"}),)
    messages += [
        _assistant(found),
        *_results(found, {"find_person": {"people": [{"user_id": LEAVER, "name": "x"}]}}),
    ]

    (call,) = _ask(messages, principals=("hr",)).calls

    assert call.name == "offboard_user"
    assert call.arguments["user_id"] == LEAVER
    TOOL_INPUTS[call.name].model_validate({**call.arguments, IDEMPOTENCY_KEY: "k"})


def test_an_unmatched_message_is_a_loud_refusal() -> None:
    messages = [{"role": "user", "content": "something no flow says"}]

    with pytest.raises(ModelUnavailableError):
        _ask(messages)


@pytest.mark.parametrize("fixture", gateway_stub.load_fixtures(), ids=lambda f: f.name)
def test_every_canned_call_names_a_loop_tool_and_its_own_fields(
    fixture: gateway_stub.Fixture,
) -> None:
    """Catches a fixture naming a tool or an argument the loop no longer offers."""
    offered = {tool.name: tool for tool in offered_tools(("employee", "hr"))}
    for step in fixture.steps:
        for call in step:
            assert call["name"] in offered, f"{fixture.name}: {call['name']} is not a loop tool"
            fields = set(offered[call["name"]].parameters.get("properties", {}))
            assert set(call["arguments"]) <= fields, f"{fixture.name}: {call['name']}"


def test_redact_answers_what_the_ingest_reads() -> None:
    async def run() -> str:
        transport = httpx.ASGITransport(app=gateway_stub.build_app())
        client = RedactionClient(base_url="http://stub", api_keys=KEYS, transport=transport)
        try:
            return (await client.redact_document("Some policy text.", TENANT)).text
        finally:
            await client.aclose()

    assert asyncio.run(run()) == "Some policy text."
