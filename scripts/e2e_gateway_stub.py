"""An OpenAI-shaped gateway that replays canned answers, for the e2e run only.

An earlier change step 5 and decision C. The flows exist to prove policy, RBAC,
approvals, the kill switch and the audit chain, none of which the model
decides. Pointing them at a real model would buy nothing and cost
determinism, money and a cross-repo prerequisite, so exactly ONE component of
the stack is replaced during `make e2e` and every other process is the shipped
code. Real-model behaviour is row 26's golden set; real-model timing is this
plan's load smoke, which does NOT use this stub (decision D).

The contract this has to honour is `helpdesk.gateway.client.ModelClient`:

* `POST {base}/chat/completions`, a body carrying `model`, `messages` and
  optionally `response_format.json_schema.name`
* a 200 answering `{"choices": [{"message": {"content": ...}}], "usage":
  {"prompt_tokens": ..., "completion_tokens": ..., "total_tokens": ...}}`,
  because the client reads all four and raises `ModelUnavailableError` on
  anything it cannot
* `GET /healthz`, which is what agent-api's `/readyz` probes.
* a 400 for a schema strict mode would refuse, found by the same
  `strict_schema_problems` the client runs, so `make e2e` cannot pass on a
  schema a real provider in strict mode answers with a 400.

An unmatched turn answers a LOUD 400 naming the schema and the message it
could not place. A stub that silently answered the wrong turn would make a
flow fail three assertions later, in a component that is not at fault, and the
whole point of a canned harness is that its failures accuse it and not the
product.

## How an answer is chosen

By the response schema's NAME (`IntentExtraction`, `ToolPlanAnswer`, or the absence
of one, which is the reply composition's plain call) crossed with a PHRASE
matched against the turn's last user message. `scripts/e2e_fixtures/*.json`
carries one file per flow.

The planner is called once per round, so `ToolPlanAnswer` is a LIST: the answers are
served in order per session and the last one, always an empty `calls` list,
repeats. That is the ordinary end of a turn rather than a fallback - a planner
with nothing left to do proposes nothing - and repeating it is what lets a
retry or an extra round terminate instead of 400ing.

## Placeholders

A canned plan cannot hardcode a uuid: the ids are the seed's, and a tool call
carrying yesterday's uuid is not a test of anything. So a string value in a
fixture may be a placeholder, substituted from the REQUEST the stub is
answering:

* `{resolved.role_id}`, `{resolved.system_id}` - read out of the planner's own
  `resolved directory ids` data block, so the id the plan proposes is the id
  the running product resolved a moment earlier.
* `{me.user_id}` - read out of the `tool results so far` block, which is where
  a `get_me` result lands. A fixture using it therefore has to call `get_me`
  in an earlier round, exactly as the planning prompt tells a real model to.
* `{accounts.<name>.userId}` - read out of the same accounts file Playwright
  uses. Only the offboard flow needs it: `Intent.OFFBOARD`'s allowed tools are
  `offboard_user` and `create_ticket` with no directory search among them, so
  a turn naming a leaver by name has no in-turn way to reach their id. That is
  a product gap this stub papers over deliberately and visibly, recorded in
  the plan's Deviations.
* `{today+N}` - an ISO date N days from today, for `end_date` arguments that
  would otherwise go stale the day after they were written.

A planned call's `arguments` is one JSON object encoded as a string, as the
strict planner schema asks of a real model. Its placeholders are filled by
decoding it, substituting, and encoding it again.
"""

import json
import os
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from helpdesk.shared.gateway.strict_schema import strict_schema_problems

FIXTURES_DIR = Path(__file__).resolve().parent / "e2e_fixtures"

PLAIN_REPLY_KEY = "text"
"""The key a fixture answers the reply composition's unstructured call with.

`compose_reply` calls `complete`, not `complete_json`, so its request carries
no `response_format` and there is no schema name to key on.
"""

_PLACEHOLDER = re.compile(r"^\{([a-z_]+(?:\.[A-Za-z0-9_]+)*|today\+\d+)\}$")

_RESOLVED_LABEL = "resolved directory ids:"
_TOOL_RESULTS_LABEL = "tool results so far:"

USAGE = {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140}
"""What every canned answer reports it cost.

Fixed and small on purpose: `gates.may_plan_another_round` stops a turn on
`tokens_used`, so a stub reporting a large number would end flows 2 and 4 in
the middle of their second round and look like a planning bug.
"""


class Fixture:
    """One flow's canned answers, and the phrase that selects them."""

    def __init__(self, path: Path) -> None:
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.name = path.stem
        self.match: str = raw["match"]
        self.answers: dict[str, Any] = raw["answers"]

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


def _labelled_block(messages: list[dict[str, Any]], label: str) -> Any:
    """The JSON `plan_tools._as_data` put under this label, or None."""
    for message in messages:
        content = message.get("content")
        if isinstance(content, str) and content.startswith(label):
            try:
                return json.loads(content[len(label) :].strip())
            except ValueError:
                return None
    return None


def _me_user_id(messages: list[dict[str, Any]]) -> str | None:
    """The `user_id` of a `get_me` result already in this turn's results."""
    results = _labelled_block(messages, _TOOL_RESULTS_LABEL)
    if not isinstance(results, list):
        return None
    for result in results:
        if not isinstance(result, dict):
            continue
        payload = result.get("output") if isinstance(result.get("output"), dict) else result
        user_id = payload.get("user_id") if isinstance(payload, dict) else None
        if isinstance(user_id, str):
            return user_id
    return None


def resolve_placeholder(token: str, messages: list[dict[str, Any]]) -> Any:
    """One `{...}` token's value, or a `KeyError` naming what was missing."""
    if token.startswith("today+"):
        return (date.today() + timedelta(days=int(token.removeprefix("today+")))).isoformat()

    head, _, tail = token.partition(".")
    if head == "resolved":
        resolved = _labelled_block(messages, _RESOLVED_LABEL) or {}
        if tail not in resolved or resolved[tail] is None:
            raise KeyError(f"the turn resolved no {tail}")
        return resolved[tail]
    if head == "me":
        user_id = _me_user_id(messages)
        if user_id is None:
            raise KeyError("no get_me result in this turn to read a user_id from")
        return user_id
    if head == "accounts":
        name, _, field = tail.partition(".")
        principal = _accounts().get(name)
        if not isinstance(principal, dict) or field not in principal:
            raise KeyError(f"no account {name}.{field} in E2E_ACCOUNTS_FILE")
        return principal[field]
    raise KeyError(f"unknown placeholder {token}")


def substitute(value: Any, messages: list[dict[str, Any]]) -> Any:
    """Walk a canned answer, replacing every placeholder string in it."""
    if isinstance(value, str):
        found = _PLACEHOLDER.match(value)
        return resolve_placeholder(found.group(1), messages) if found else value
    if isinstance(value, list):
        return [substitute(item, messages) for item in value]
    if isinstance(value, dict):
        return {key: _substituted_item(key, item, messages) for key, item in value.items()}
    return value


def _substituted_item(key: str, item: Any, messages: list[dict[str, Any]]) -> Any:
    """One dict value substituted; an encoded `arguments` string is filled inside."""
    if key == "arguments" and isinstance(item, str):
        return json.dumps(substitute(json.loads(item), messages))
    return substitute(item, messages)


def _schema_name(body: dict[str, Any]) -> str:
    response_format = body.get("response_format")
    if not isinstance(response_format, dict):
        return PLAIN_REPLY_KEY
    schema = response_format.get("json_schema")
    name = schema.get("name") if isinstance(schema, dict) else None
    return name if isinstance(name, str) else PLAIN_REPLY_KEY


def _schema(body: dict[str, Any]) -> dict[str, Any]:
    """The request's JSON schema, or an empty one for the plain reply call."""
    response_format = body.get("response_format")
    json_schema = response_format.get("json_schema") if isinstance(response_format, dict) else None
    schema = json_schema.get("schema") if isinstance(json_schema, dict) else None
    return schema if isinstance(schema, dict) else {}


def _last_user_message(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        content = message.get("content")
        if message.get("role") == "user" and isinstance(content, str):
            return content
    return ""


def build_app() -> FastAPI:
    app = FastAPI(title="e2e gateway stub")
    served: dict[tuple[str, str], int] = {}
    app.state.fixtures = load_fixtures()
    app.state.served = served

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """What agent-api's `/readyz` probes before it calls itself ready."""
        return {"status": "ok"}

    @app.post("/v1/chat/completions")
    @app.post("/chat/completions")
    async def completions(request: Request) -> JSONResponse:
        body = await request.json()
        messages = [m for m in body.get("messages", []) if isinstance(m, dict)]
        schema = _schema_name(body)
        message = _last_user_message(messages)

        problems = strict_schema_problems(_schema(body))
        if problems:
            return JSONResponse(
                status_code=400,
                content={"error": f"e2e gateway stub: {schema} breaks strict mode: {problems}"},
            )

        fixture = next((f for f in app.state.fixtures if f.matches(message)), None)
        if fixture is None or schema not in fixture.answers:
            return _no_answer(schema, message, fixture)

        answer = fixture.answers[schema]
        if isinstance(answer, list):
            key = (fixture.name, schema)
            index = min(app.state.served.get(key, 0), len(answer) - 1)
            app.state.served[key] = index + 1
            answer = answer[index]

        try:
            filled = substitute(answer, messages)
        except KeyError as exc:
            return JSONResponse(
                status_code=400,
                content={"error": f"{fixture.name}/{schema}: {exc.args[0]}"},
            )

        content = filled if isinstance(filled, str) else json.dumps(filled)
        return JSONResponse(
            content={
                "id": f"e2e-{datetime.now(UTC).timestamp()}",
                "model": body.get("model"),
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
                "usage": USAGE,
            }
        )

    return app


def _no_answer(schema: str, message: str, fixture: Fixture | None) -> JSONResponse:
    """The loud 400 (plan step 5), naming what could not be placed."""
    reason = (
        f"no fixture matches {message!r}"
        if fixture is None
        else f"fixture {fixture.name} has no {schema} answer"
    )
    return JSONResponse(status_code=400, content={"error": f"e2e gateway stub: {reason}"})


app = build_app()


def main() -> None:
    port = int(os.environ.get("E2E_STUB_GATEWAY_PORT", "8099"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
