"""An OpenAI-shaped gateway that replays canned answers, for the e2e run only.

The flows exist to prove policy, RBAC, approvals, the kill switch and the
audit chain, none of which the model decides. Pointing them at a real model
would buy nothing and cost determinism, money and a second repo to run, so
exactly ONE component of the stack is replaced during `make e2e` and every
other process is the shipped code. Real-model behaviour is the conversation
eval cases; real-model timing is the load smoke, which does NOT use this stub.

The contract this has to honour is the agent loop's model client
(`ModelClient.complete_with_tools`):

* `POST {base}/chat/completions` with `model`, `messages`, and `tools` plus
  `tool_choice` when tools are offered. `tool_choice: "none"` is an
  answer-only step: only text is read.
* a 200 answering `{"choices": [{"message": {"content": ..., "tool_calls":
  [...]}}], "usage": {...}}`. Each tool call is `{"id", "type": "function",
  "function": {"name", "arguments"}}` with `arguments` a JSON object encoded
  as a string, as a real provider sends it.
* `POST {base}/redact`, which the policy ingest calls once per document. The
  text comes back unchanged with a made-up map id: the e2e corpus is fake.
* `GET /healthz`, which is what agent-api's `/readyz` probes.

An unmatched turn answers a LOUD 400 naming what it could not place. A stub
that silently answered the wrong turn would make a flow fail three assertions
later, in a component that is not at fault.

## How an answer is chosen

A fixture in `backend/scripts/e2e/fixtures/*.json` is picked by a PHRASE
found in the chat's user messages, newest first, so the "yes" turn after a
held change still finds the flow it belongs to.

A fixture carries `steps`, the tool calls of each model step of the flow's
first turn, in order, and `reply`, the text of every step that answers. When
the newest user message is the flow's own phrase, the step number is how many
tool-calling assistant turns already follow it. Past the last step, on an
answer-only step, or on any later turn (the step after a yes runs the held
change), the stub answers with `reply`.

## Placeholders

A canned call cannot hardcode a uuid: the ids are the seed's, and the loop
holds a change only when every id in it came from a tool result of the same
turn. So a string value in a step's arguments may be a placeholder, filled
from the REQUEST the stub is answering:

* `{<tool>.<path>}` - read out of that tool's result in this turn, for
  example `{get_me.user_id}` or `{find_role.roles.0.role_id}`. A number in
  the path picks a list item.
* `{accounts.<name>.<field>}` - read out of the accounts file Playwright uses.
* `{today+N}` - an ISO date N days from today, for dates that would
  otherwise go stale the day after they were written.
"""

import json
import os
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

_PLACEHOLDER = re.compile(r"^\{([a-z_]+(?:\.[A-Za-z0-9_]+)+|today\+\d+)\}$")

USAGE = {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140}
"""What every canned answer reports it cost.

Fixed and small on purpose: the loop stops a turn on its token budget, so a
stub reporting a large number would end a flow in the middle of its steps.
"""


class Fixture:
    """One flow's canned steps and reply, and the phrase that selects them."""

    def __init__(self, path: Path) -> None:
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.name = path.stem
        self.match: str = raw["match"]
        self.steps: list[list[dict[str, Any]]] = raw["steps"]
        self.reply: str = raw["reply"]

    def matches(self, message: str) -> bool:
        return self.match.lower() in message.lower()


def load_fixtures() -> list[Fixture]:
    return [Fixture(path) for path in sorted(FIXTURES_DIR.glob("*.json"))]


def _accounts() -> dict[str, Any]:
    """The accounts file, or an empty map when the run did not produce one."""
    path = os.environ.get("E2E_ACCOUNTS_FILE")
    if not path or not Path(path).is_file():
        return {}
    loaded: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
    return loaded


def _last_user_index(messages: list[dict[str, Any]]) -> int:
    """Where the newest user message is, or -1 when there is none."""
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            return index
    return -1


def _user_texts_newest_first(messages: list[dict[str, Any]]) -> list[str]:
    return [
        message["content"]
        for message in reversed(messages)
        if message.get("role") == "user" and isinstance(message.get("content"), str)
    ]


def tool_results(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Each tool's latest result in this turn, by tool name, read as JSON."""
    names: dict[str, str] = {}
    results: dict[str, Any] = {}
    for message in messages[_last_user_index(messages) + 1 :]:
        for call in message.get("tool_calls") or []:
            names[call["id"]] = call["function"]["name"]
        if message.get("role") == "tool" and message.get("tool_call_id") in names:
            try:
                results[names[message["tool_call_id"]]] = json.loads(message["content"])
            except (TypeError, ValueError):
                continue
    return results


def _walk(value: Any, path: list[str], token: str) -> Any:
    for part in path:
        if isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        elif isinstance(value, dict) and part in value:
            value = value[part]
        else:
            raise KeyError(f"no {token} in this turn's tool results")
    return value


def resolve_placeholder(token: str, messages: list[dict[str, Any]]) -> Any:
    """One `{...}` token's value, or a `KeyError` naming what was missing."""
    if token.startswith("today+"):
        return (date.today() + timedelta(days=int(token.removeprefix("today+")))).isoformat()
    head, *path = token.split(".")
    if head == "accounts":
        return _walk(_accounts(), path, token)
    results = tool_results(messages)
    if head not in results:
        raise KeyError(f"no {head} result in this turn to read {token} from")
    return _walk(results[head], path, token)


def substitute(value: Any, messages: list[dict[str, Any]]) -> Any:
    """Walk canned arguments, replacing every placeholder string in them."""
    if isinstance(value, str):
        found = _PLACEHOLDER.match(value)
        return resolve_placeholder(found.group(1), messages) if found else value
    if isinstance(value, list):
        return [substitute(item, messages) for item in value]
    if isinstance(value, dict):
        return {key: substitute(item, messages) for key, item in value.items()}
    return value


def _offered(body: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for tool in body.get("tools") or []:
        function = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            names.add(function["name"])
    return names


def answer(body: dict[str, Any], fixtures: list[Fixture]) -> dict[str, Any]:
    """The assistant message for this request, or a `LookupError` saying why
    there is none."""
    messages = [m for m in body.get("messages", []) if isinstance(m, dict)]
    users = _user_texts_newest_first(messages)
    fixture = next((f for text in users for f in fixtures if f.matches(text)), None)
    if fixture is None:
        newest = users[0] if users else ""
        raise LookupError(f"no fixture matches {newest!r}")

    answer_only = not body.get("tools") or body.get("tool_choice") == "none"
    turn = messages[_last_user_index(messages) + 1 :]
    step = sum(
        1 for message in turn if message.get("role") == "assistant" and message.get("tool_calls")
    )
    own_turn = bool(users) and fixture.matches(users[0])
    if answer_only or not own_turn or step >= len(fixture.steps):
        return {"role": "assistant", "content": fixture.reply}

    offered = _offered(body)
    calls = []
    for index, call in enumerate(fixture.steps[step]):
        if call["name"] not in offered:
            raise LookupError(f"{fixture.name} step {step}: {call['name']} is not offered")
        try:
            arguments = substitute(call.get("arguments", {}), messages)
        except KeyError as exc:
            raise LookupError(f"{fixture.name} step {step}: {exc.args[0]}") from exc
        calls.append(
            {
                "id": f"call_{step}_{index}",
                "type": "function",
                "function": {"name": call["name"], "arguments": json.dumps(arguments)},
            }
        )
    return {"role": "assistant", "content": None, "tool_calls": calls}


def build_app() -> FastAPI:
    app = FastAPI(title="e2e gateway stub")
    app.state.fixtures = load_fixtures()

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """What agent-api's `/readyz` probes before it calls itself ready."""
        return {"status": "ok"}

    @app.post("/v1/redact")
    @app.post("/redact")
    async def redact(request: Request) -> dict[str, str]:
        """The text unchanged: the e2e corpus holds nothing to hide."""
        body = await request.json()
        return {"text": str(body.get("text", "")), "redaction_map_id": f"e2e-{uuid.uuid4()}"}

    @app.post("/v1/chat/completions")
    @app.post("/chat/completions")
    async def completions(request: Request) -> JSONResponse:
        body = await request.json()
        try:
            message = answer(body, app.state.fixtures)
        except LookupError as exc:
            return JSONResponse(
                status_code=400, content={"error": f"e2e gateway stub: {exc.args[0]}"}
            )
        return JSONResponse(
            content={
                "id": f"e2e-{datetime.now(UTC).timestamp()}",
                "model": body.get("model"),
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                "usage": USAGE,
            }
        )

    return app


app = build_app()


def main() -> None:
    port = int(os.environ.get("E2E_STUB_GATEWAY_PORT", "8099"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
