"""The strict-mode schema checker: what OpenAI's `strict: true` refuses, found here first.

`complete_json` asks for strict mode, and a provider in strict mode answers a
schema that breaks its rules with a 400 for the whole request. Three rules,
each checked on every node the walk reaches:

- every object carries `additionalProperties: false`;
- every object's `required` lists every key of its `properties`, no more and
  no fewer, so an optional field is `anyOf [X, null]` and still required;
- no node carries a `default` key, which Pydantic emits for any field with a
  default and strict mode rejects.

The walk covers the root, `$defs`, `properties`, `items` and `anyOf`, the
constructs Pydantic generates for this repo's models. Each problem names its
JSON path rooted at `$`, so a refusal says which field to fix. Pinned by
`test_gateway_strict_schema.py::test_a_problem_nested_in_defs_items_and_any_of_is_found`,
`::test_a_default_key_is_a_problem`, `::test_a_property_left_out_of_required_is_a_problem`,
`::test_additional_properties_true_is_a_problem` and
`::test_an_object_without_additional_properties_is_a_problem`; a legal
`$ref` or nullable branch is not flagged, pinned by
`::test_a_compliant_schema_has_no_problems`.

Pure: no I/O. The client runs it before sending
(`test_gateway_client.py::test_a_non_strict_schema_is_refused_before_any_request`),
and the test fakes and the e2e stub run the same function to answer 400.
"""

from collections.abc import Mapping
from typing import Any


def strict_schema_problems(schema: Mapping[str, Any]) -> list[str]:
    """Every strict-mode rule `schema` breaks, one string each; empty when it meets them all."""
    problems: list[str] = []
    _walk(schema, "$", problems)
    return problems


def _is_object(node: Mapping[str, Any]) -> bool:
    """An object node: typed `object`, or carrying `properties` without a type."""
    return node.get("type") == "object" or "properties" in node


def _walk(node: Any, path: str, problems: list[str]) -> None:
    """Check one node, then every child node the three rules can apply to."""
    if not isinstance(node, Mapping):
        return

    if "default" in node:
        problems.append(f"{path}: carries a default, which strict mode refuses")

    properties = node.get("properties")
    properties = properties if isinstance(properties, Mapping) else {}
    if _is_object(node):
        if node.get("additionalProperties") is not False:
            problems.append(f"{path}: additionalProperties must be false")
        required = node.get("required")
        listed = set(required) if isinstance(required, list) else set()
        for key in properties:
            if key not in listed:
                problems.append(f"{path}.properties.{key}: missing from required")
        for key in sorted(listed - set(properties)):
            problems.append(f"{path}: required names {key}, which is not a property")

    for key, child in properties.items():
        _walk(child, f"{path}.properties.{key}", problems)

    definitions = node.get("$defs")
    if isinstance(definitions, Mapping):
        for key, child in definitions.items():
            _walk(child, f"{path}.$defs.{key}", problems)

    _walk(node.get("items"), f"{path}.items", problems)

    branches = node.get("anyOf")
    if isinstance(branches, list):
        for index, child in enumerate(branches):
            _walk(child, f"{path}.anyOf[{index}]", problems)
