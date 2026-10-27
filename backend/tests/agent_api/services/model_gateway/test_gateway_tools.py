"""The tool-calling chat call of the model client.

Expected implementation surface:

``helpdesk.agent_api.services.model_gateway.client``
    ``@dataclass(frozen=True) class ToolCall`` with ``id``, ``name`` and
        ``arguments`` (a dict, already read from the JSON string the gateway
        sends).
    ``@dataclass(frozen=True) class ToolCallAnswer`` with ``text`` (or
        ``None``), ``calls`` (a tuple of ``ToolCall``) and ``usage`` (the
        ``ModelAnswer`` the plain call would give).
    ``ModelClient.complete_with_tools(messages, tools, *, alias, tenant_slug,
        session_id=None, traceparent=None, max_tokens=None, temperature=None)``

On the wire: the body holds ``model``, ``messages`` exactly as given,
``stream: false``, and, when ``tools`` is not empty, ``tools`` as given and
``tool_choice: "auto"``. With no tools neither key is sent. Arguments that are
not one JSON object are ``ModelUnavailableError`` (``INTERNAL``).

No socket is opened: every case drives ``httpx.MockTransport`` under one
``asyncio.run``. Nothing here needs a database.
"""

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import httpx
import pytest

from helpdesk.agent_api.services.model_gateway.client import (
    ModelAlias,
    ModelClient,
    ModelUnavailableError,
    ToolCall,
    ToolCallAnswer,
)
from helpdesk.shared.core.errors import ErrorCode, ToolError

BASE_URL = "http://gateway.invalid:8080"
TENANT = "nordlicht"
# Obviously fake, tests only: this is not a credential.
API_KEYS: dict[str, str] = {TENANT: "fake-key-nordlicht"}

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_policy",
            "description": "Search the policy text.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_my_access",
            "description": "List the access I hold.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

MESSAGES: list[dict[str, Any]] = [
    {"role": "system", "content": "You help with access questions."},
    {"role": "user", "content": "Can I read the fake payroll report?"},
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "search_policy", "arguments": '{"query": "payroll"}'},
            }
        ],
    },
    {"role": "tool", "tool_call_id": "call_1", "content": '{"citations": [], "refused": true}'},
]

USAGE = {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}


def _text_body(text: str) -> dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": text}}], "usage": USAGE}


def _calls_body(*calls: Mapping[str, Any]) -> dict[str, Any]:
    message = {"role": "assistant", "content": None, "tool_calls": list(calls)}
    return {"choices": [{"message": message}], "usage": USAGE}


def _wire_call(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def _complete(
    handler: Callable[[httpx.Request], httpx.Response],
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]],
) -> ToolCallAnswer:
    async def scenario() -> ToolCallAnswer:
        client = ModelClient(
            base_url=BASE_URL, api_keys=API_KEYS, transport=httpx.MockTransport(handler)
        )
        try:
            return await client.complete_with_tools(
                messages, tools, alias=ModelAlias.AGENT_STRONG, tenant_slug=TENANT
            )
        finally:
            await client.aclose()

    return asyncio.run(scenario())


def test_tool_call_posts_tools_and_turns() -> None:
    """Catches a client that drops the assistant `tool_calls` or the `tool`
    turns from the messages (the model would never see its own earlier calls
    or their results), that rewrites them into plain text, that forgets `tools`
    or `tool_choice: "auto"`, or that sends the keys when no tool is offered
    (the last step of a turn must go out with neither).

    The body is compared whole with what was given, so any rewrite shows.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_text_body("fine"))

    _complete(handler, MESSAGES, TOOLS)
    _complete(handler, MESSAGES, [])

    with_tools = json.loads(seen[0].content)
    assert seen[0].url.path == "/chat/completions"
    assert with_tools["model"] == "agent-strong"
    assert with_tools["stream"] is False
    assert with_tools["messages"] == MESSAGES
    assert with_tools["tools"] == TOOLS
    assert with_tools["tool_choice"] == "auto"

    without_tools = json.loads(seen[1].content)
    assert without_tools["messages"] == MESSAGES
    assert "tools" not in without_tools
    assert "tool_choice" not in without_tools


def test_tool_call_reads_calls_or_text() -> None:
    """Catches a client that returns only text (the calls are lost and the turn
    ends empty), that leaves the arguments as the raw JSON string, that mixes
    up the ids of two calls, or that fails on a null `content`.

    One answer holds two calls and a null content; the other holds only text.
    The usage must be the answer's own token counts.
    """
    calls_answer = _complete(
        lambda request: httpx.Response(
            200,
            json=_calls_body(
                _wire_call("call_a", "search_policy", '{"query": "vpn", "top_k": 3}'),
                _wire_call("call_b", "list_my_access", "{}"),
            ),
        ),
        MESSAGES,
        TOOLS,
    )
    text_answer = _complete(
        lambda request: httpx.Response(200, json=_text_body("hello there")), MESSAGES, TOOLS
    )

    assert isinstance(calls_answer, ToolCallAnswer)
    assert calls_answer.text is None
    assert calls_answer.calls == (
        ToolCall(id="call_a", name="search_policy", arguments={"query": "vpn", "top_k": 3}),
        ToolCall(id="call_b", name="list_my_access", arguments={}),
    )
    assert calls_answer.usage.alias is ModelAlias.AGENT_STRONG
    assert (
        calls_answer.usage.prompt_tokens,
        calls_answer.usage.completion_tokens,
        calls_answer.usage.total_tokens,
    ) == (3, 4, 7)
    assert text_answer.text == "hello there"
    assert text_answer.calls == ()


@pytest.mark.parametrize(
    "arguments",
    ['{"query": "vpn"', "not json at all", '["query", "vpn"]', '"vpn"', "null", "42"],
    ids=["cut_off", "words", "list", "string", "null", "number"],
)
def test_tool_call_bad_arguments_json_is_a_typed_error(arguments: str) -> None:
    """Unhappy path. Catches a client that lets `json.JSONDecodeError` escape
    (a crash with no typed code), that passes a list, string, null or number on
    as if it were the arguments (a later `**arguments` or model check then
    breaks far from the cause), or that answers with an empty call instead.

    The code must be the client's own `INTERNAL`: the model's output was
    unusable, and the caller cannot fix it by editing its input.
    """
    with pytest.raises(ModelUnavailableError) as excinfo:
        _complete(
            lambda request: httpx.Response(
                200, json=_calls_body(_wire_call("call_a", "search_policy", arguments))
            ),
            MESSAGES,
            TOOLS,
        )

    assert excinfo.value.code is ErrorCode.INTERNAL
    assert isinstance(excinfo.value, ToolError)


def test_answer_only_sends_the_tools_with_choice_none() -> None:
    """Catches a client that drops the tools when `answer_only` is set (the tool
    part of the request would differ from every other call), that still says
    `tool_choice: "auto"`, or that sends `tool_choice` with no tools (the
    gateway rejects that).

    With tools, the body holds the same tools and "none". With no tools, even
    with `answer_only`, neither key is sent.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_text_body("fine"))

    async def scenario() -> None:
        client = ModelClient(
            base_url=BASE_URL, api_keys=API_KEYS, transport=httpx.MockTransport(handler)
        )
        try:
            await client.complete_with_tools(
                MESSAGES, TOOLS, alias=ModelAlias.AGENT_STRONG, tenant_slug=TENANT, answer_only=True
            )
            await client.complete_with_tools(
                MESSAGES, [], alias=ModelAlias.AGENT_STRONG, tenant_slug=TENANT, answer_only=True
            )
        finally:
            await client.aclose()

    asyncio.run(scenario())

    with_tools = json.loads(seen[0].content)
    assert with_tools["tools"] == TOOLS
    assert with_tools["tool_choice"] == "none"
    without_tools = json.loads(seen[1].content)
    assert "tools" not in without_tools
    assert "tool_choice" not in without_tools
