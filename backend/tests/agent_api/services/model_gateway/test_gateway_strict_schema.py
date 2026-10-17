"""The strict-mode schema checker in `helpdesk/gateway/strict_schema.py`.

Expected implementation surface (the implementer must provide exactly this):

``helpdesk.gateway.strict_schema``
    ``def strict_schema_problems(schema: Mapping[str, Any]) -> list[str]`` —
        pure, walks the root, ``$defs``, ``properties``, ``items`` and
        ``anyOf``, and returns one problem string per broken rule. Each string
        names the JSON path of the node it is about, rooted at ``$``
        (``$``, ``$.properties.b``, ``$.$defs.X``, ``$.properties.p.items``,
        ``$.properties.q.anyOf[0]``).

The three rules are OpenAI strict mode's, as it states them: every
object carries ``additionalProperties: false``; every object's ``required``
lists every key of its ``properties``; no node carries a ``default`` key.

Cost: pure function over hand-written dicts, no backing service.
"""

from typing import Any

from helpdesk.agent_api.services.model_gateway.strict_schema import strict_schema_problems


def _compliant() -> dict[str, Any]:
    """A two-level schema meeting every rule, shaped like Pydantic's output.

    It carries each construct the checker must walk: a `$defs` object reached
    by `$ref` from an array's `items`, a nullable field as `anyOf [X, null]`,
    and a nested object inline. A fresh dict per call, so a case that breaks
    one node cannot leak its breach into another case.
    """
    return {
        "type": "object",
        "title": "Plan",
        "additionalProperties": False,
        "properties": {
            "name": {"type": "string"},
            "note": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            "calls": {"type": "array", "items": {"$ref": "#/$defs/Call"}},
            "owner": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
        "required": ["name", "note", "calls", "owner"],
        "$defs": {
            "Call": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"tool": {"type": "string"}, "arguments": {"type": "string"}},
                "required": ["tool", "arguments"],
            }
        },
    }


def test_a_compliant_schema_has_no_problems() -> None:
    """Catches a checker that flags a legal construct: a nullable `anyOf`
    branch, a `$ref` node with no `type`, or an array's `items` read as an
    object missing its keys. Any of those would make every real schema fail
    the client's pre-check and stop every chat turn."""
    assert strict_schema_problems(_compliant()) == []


def test_an_object_without_additional_properties_is_a_problem() -> None:
    """Catches a checker that only rejects an explicit `true` and lets an
    absent `additionalProperties` pass, which is exactly what Pydantic emits
    for a model without `extra="forbid"`. Only the root breaks the rule, so a
    problem naming a nested path is wrong."""
    schema = _compliant()
    del schema["additionalProperties"]

    problems = strict_schema_problems(schema)

    assert len(problems) == 1
    assert "$" in problems[0]
    assert "$.properties" not in problems[0]
    assert "$.$defs" not in problems[0]


def test_additional_properties_true_is_a_problem() -> None:
    """Catches a checker that tests for the key's PRESENCE rather than its
    value being `false`: here the key is present and set to `true`."""
    schema = _compliant()
    schema["additionalProperties"] = True

    problems = strict_schema_problems(schema)

    assert len(problems) == 1


def test_a_property_left_out_of_required_is_a_problem() -> None:
    """Catches a checker that compares `required` only to itself (non-empty,
    or present) instead of to every key of `properties`: `a` is listed, `b`
    is not, and the object is otherwise legal."""
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
        "required": ["a"],
    }

    problems = strict_schema_problems(schema)

    assert len(problems) == 1
    assert "$.properties.b" in problems[0]


def test_a_default_key_is_a_problem() -> None:
    """Catches a checker that knows only the two object rules and misses
    the third rule: a nullable field still listed in `required` but
    carrying Pydantic's `"default": null`, which strict mode rejects."""
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "a": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
        },
        "required": ["a"],
    }

    problems = strict_schema_problems(schema)

    assert len(problems) == 1
    assert "$.properties.a" in problems[0]


def test_a_problem_nested_in_defs_items_and_any_of_is_found() -> None:
    """Catches a checker that walks only the root and its `properties`: each
    of the three breaches sits under exactly one of `$defs`, `items` and
    `anyOf`, so a walker missing any one of the three returns two problems
    rather than three, and the missing path names which."""
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "p": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": True,
                    "properties": {"x": {"type": "string"}},
                    "required": ["x"],
                },
            },
            "q": {
                "anyOf": [
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"y": {"type": "string"}, "z": {"type": "string"}},
                        "required": ["y"],
                    },
                    {"type": "null"},
                ]
            },
        },
        "required": ["p", "q"],
        "$defs": {
            "X": {
                "type": "object",
                "properties": {"w": {"type": "string"}},
                "required": ["w"],
            }
        },
    }

    problems = strict_schema_problems(schema)

    assert len(problems) == 3
    for path in ("$.$defs.X", "$.properties.p.items", "$.properties.q.anyOf[0]"):
        assert sum(path in problem for problem in problems) == 1, (path, problems)
