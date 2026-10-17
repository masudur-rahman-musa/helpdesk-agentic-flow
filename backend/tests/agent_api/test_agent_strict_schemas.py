"""Every schema the agent sends to `complete_json` meets strict mode.

Expected implementation surface (the implementer must provide exactly this):

``agent_api.graph.state``
    ``IntentEntities`` / ``IntentExtraction`` — ``extra="forbid"``, every
        field required, no defaults; an optional field is ``X | None``.
    ``class PlannedCallAnswer(BaseModel)`` — ``extra="forbid"``, exactly two
        fields: ``tool: str`` and ``arguments: str``. A field validator
        decodes ``arguments`` as JSON and refuses anything but an object, so
        after validation the field holds the decoded dict.
    ``class ToolPlanAnswer(BaseModel)`` — ``extra="forbid"``,
        ``calls: list[PlannedCallAnswer]``, no default.
``helpdesk.gateway.strict_schema``
    ``def strict_schema_problems(schema: Mapping[str, Any]) -> list[str]``

Cost: pure model and file reads, no backing service.
"""

import re
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from helpdesk.agent_api.agent.state import IntentExtraction, PlannedCallAnswer, ToolPlanAnswer
from helpdesk.agent_api.services.model_gateway.strict_schema import strict_schema_problems

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SWEPT_TREES: tuple[Path, ...] = (_REPO_ROOT / "backend" / "helpdesk",)
_CALL_SITE = re.compile(r"\.complete_json\(")

SCHEMA_MODELS: tuple[type[BaseModel], ...] = (IntentExtraction, ToolPlanAnswer)
"""The schema model of each `complete_json` caller: `extract_intent` asks for
`IntentExtraction`, `plan_tools` for `ToolPlanAnswer`. The sweep below keeps
this list honest: a third caller turns it red before its schema goes unread."""

KNOWN_CALLERS = frozenset({"extract_intent.py", "plan_tools.py"})


@pytest.mark.parametrize("model", SCHEMA_MODELS, ids=lambda model: model.__name__)
def test_every_schema_sent_to_complete_json_meets_strict_mode(model: type[BaseModel]) -> None:
    """Catches a schema model left with a default or without
    `extra="forbid"` (today's `IntentExtraction` with its `None` defaults is
    one), which the client would refuse before sending and so fail every
    chat turn at intent extraction or at planning."""
    assert strict_schema_problems(model.model_json_schema()) == []


def _callers() -> set[str]:
    """Every non-test source file under the swept trees calling `complete_json`."""
    return {
        path.name
        for tree in _SWEPT_TREES
        for path in tree.rglob("*.py")
        if "tests" not in path.parts
        and ".venv" not in path.parts
        and _CALL_SITE.search(path.read_text())
    }


def test_the_complete_json_callers_are_exactly_the_known_two() -> None:
    """Catches a third `complete_json` caller added without its schema model
    joining `SCHEMA_MODELS`, which is how a non-strict schema would reach the
    gateway with no case here reading it. Exact equality, so a sweep reading
    zero files fails too."""
    assert _callers() == KNOWN_CALLERS


def test_planned_call_arguments_decode_to_a_dict() -> None:
    """Catches a validator that leaves `arguments` as the raw string, which
    `plan_tools` would then hand to `ProposedCall` and `validate_tool_calls`
    as text rather than as the object they check."""
    answer = PlannedCallAnswer.model_validate({"tool": "t", "arguments": '{"role_id": "x"}'})

    # The field is declared `str` on the wire and holds the decoded dict
    # after validation, so it is read through `object` for the comparison.
    decoded: object = answer.arguments
    assert decoded == {"role_id": "x"}


@pytest.mark.parametrize(
    "arguments",
    ["not json", "[1]", '"x"', "3", "null"],
    ids=["not-json", "array", "string", "number", "null"],
)
def test_planned_call_arguments_that_are_not_a_json_object_fail_validation(
    arguments: str,
) -> None:
    """Catches a validator that only decodes (`not json` alone would fail,
    the other four would pass as a list, a string, a number or `None`), and
    one that checks truthiness rather than type, which lets `[1]` through.
    The failure must be a `ValidationError` so `complete_json` takes its one
    retry rather than ending the turn on the first bad answer."""
    with pytest.raises(ValidationError):
        PlannedCallAnswer.model_validate({"tool": "t", "arguments": arguments})
