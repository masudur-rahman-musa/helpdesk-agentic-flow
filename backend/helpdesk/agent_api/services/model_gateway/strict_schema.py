"""Find what OpenAI's strict mode would refuse in a schema, before sending it.

Strict mode answers a bad schema with a 400 for the whole request. The rules:
every object has `additionalProperties: false`, lists every property in
`required` (an optional field is `anyOf [X, null]`), and no node has a
`default`. Each problem names its JSON path so the refusal says what to fix.
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
