"""The `list_requestable_roles` read tool, through the transport.

Surface under test (written before it exists):

``mcp_server.tools.read``
    ``list_requestable_roles(system?, limit?)`` — read-only, audited, every
    principal, own tenant only. ``system`` is at most 64 characters (the slug
    column); ``limit`` is 1 to 200 and defaults to 50. Output is
    ``{"roles": [row, ...]}`` and each row carries ``system_slug``,
    ``system_name``, ``system_sensitivity``, ``role_slug``, ``role_name``,
    ``privilege``, ``default_expiry_days``, ``in_my_template`` (the caller's
    own department template lists the role) and ``already_held`` (the caller
    has a live grant: status active, not revoked, not past its expiry).

Fixture cost: the session-scoped `mcp_env` seam (one seeded database, one
Redis index). Only Postgres shows the tenant filter and the template and grant
joins, so every case here carries the `db` marker. The grants probe user from
`seeded_mcp_db` is the caller where grants matter: it holds three live grants
and three hidden ones, each hidden one failing exactly one conjunct of "live".
"""

from typing import Any

import pytest

from helpdesk.shared.auth.rbac import Principal
from scripts.seed import CUAN, NORDLICHT, TenantSpec, generate_tenant
from tests.support.mcp import (  # noqa: F401 - fixtures are resolved by name
    SeededMcp,
    bearer,
    call_tool,
    error_code,
    list_tools,
    mcp_env,
    mcp_pg,
    mcp_redis_base_url,
    mcp_redis_url,
    seeded_mcp_db,
    token_for,
)

pytestmark = pytest.mark.db

TOOL = "list_requestable_roles"
SYSTEM_MAX = 64
LIMIT_MIN = 1
LIMIT_MAX = 200
LIMIT_DEFAULT = 50
SUCCEEDED_EVENT = "tool.succeeded"

_SPECS: dict[str, TenantSpec] = {CUAN.slug: CUAN, NORDLICHT.slug: NORDLICHT}
_EXPIRY_DAYS = {"read": 365, "write": 180, "admin": 90}


def _roles(result: Any) -> list[dict[str, Any]]:
    assert result.is_error is False, result.content
    assert result.structured_content is not None
    roles: list[dict[str, Any]] = result.structured_content["roles"]
    return roles


def _pairs(spec: TenantSpec) -> set[tuple[str, str]]:
    return {(role.system, role.slug) for role in spec.roles}


def _audit_count(env: SeededMcp) -> int:
    """How many `tool.succeeded` events this tool has filed, read back through
    `search_audit` as an auditor (its own call files an event under its own
    tool name, so it never moves this count)."""
    result = call_tool(
        bearer(token_for(env.tenant_a, principals=("auditor",))),
        "search_audit",
        {"event_type": SUCCEEDED_EVENT, "subject_type": "tool", "subject_id": TOOL, "limit": 200},
    )
    assert result.is_error is False, result.content
    assert result.structured_content is not None
    return len(result.structured_content["events"])


def test_lists_only_the_callers_tenant(mcp_env: SeededMcp) -> None:  # noqa: F811
    """Catches a query that forgets the tenant filter: the other tenant's
    system and role pairs would join the answer, so a caller would be offered
    (and told the names of) roles that exist only in another company."""
    caller = mcp_env.tenant_a
    own = _SPECS[caller.tenant_slug]
    other = _SPECS[mcp_env.tenant_b.tenant_slug]
    foreign_only = _pairs(other) - _pairs(own)
    assert foreign_only, "fixture premise: the other tenant has roles this one lacks"

    roles = _roles(call_tool(bearer(token_for(caller)), TOOL, {"limit": LIMIT_MAX}))

    seen = {(row["system_slug"], row["role_slug"]) for row in roles}
    assert seen == _pairs(own)
    assert seen.isdisjoint(foreign_only)


def test_every_row_carries_the_catalogue_values(mcp_env: SeededMcp) -> None:  # noqa: F811
    """Catches a row that reports a wrong or swapped column (the role's name
    for the system's, privilege for sensitivity, a made-up expiry): each field
    is compared with the seeded catalogue value, not with another output field."""
    caller = mcp_env.tenant_a
    spec = _SPECS[caller.tenant_slug]
    systems = {slug: (name, sensitivity) for slug, name, sensitivity, _ in spec.systems}

    roles = _roles(call_tool(bearer(token_for(caller)), TOOL, {"limit": LIMIT_MAX}))

    assert len(roles) == len(spec.roles)
    by_pair = {(row["system_slug"], row["role_slug"]): row for row in roles}
    for role in spec.roles:
        row = by_pair[(role.system, role.slug)]
        assert set(row) == {
            "system_slug",
            "system_name",
            "system_sensitivity",
            "role_slug",
            "role_name",
            "privilege",
            "default_expiry_days",
            "in_my_template",
            "already_held",
        }
        assert row["system_name"] == systems[role.system][0]
        assert row["system_sensitivity"] == systems[role.system][1]
        assert row["role_name"] == f"{role.system}.{role.slug}"
        assert row["privilege"] == role.privilege
        assert row["default_expiry_days"] == _EXPIRY_DAYS[role.privilege]


def test_marks_template_roles_and_held_roles(mcp_env: SeededMcp) -> None:  # noqa: F811
    """Catches a flag computed from the wrong source. `in_my_template` must
    come from the caller's own department template and `already_held` from
    live grants only: the probe holds exactly three live grants and three
    hidden ones (past expiry, revoked, status expired), each failing one
    clause of "live", so an implementation that drops any clause marks a
    hidden role as held, and one that reads the grant for the template flag
    (or the reverse) swaps the two sets."""
    probe = mcp_env.probe
    spec = _SPECS[probe.tenant_slug]
    department = next(
        name
        for name, ident in generate_tenant(spec).department_ids.items()
        if ident == probe.department_id
    )
    template = set(spec.templates[department])
    held = {(grant.system_slug, grant.role_slug) for grant in mcp_env.visible_grants}
    assert template and len(held) == 3, "fixture premise: a template and three live grants"
    assert template != held, "fixture premise: the two sets differ"

    roles = _roles(call_tool(bearer(token_for(probe)), TOOL, {"limit": LIMIT_MAX}))

    by_pair = {(row["system_slug"], row["role_slug"]): row for row in roles}
    assert held <= set(by_pair) and template <= set(by_pair)
    for pair, row in by_pair.items():
        assert row["in_my_template"] is (pair in template), pair
        assert row["already_held"] is (pair in held), pair
    assert sum(1 for row in roles if row["already_held"]) == 3


def test_filters_by_system(mcp_env: SeededMcp) -> None:  # noqa: F811
    """Catches a `system` argument that is accepted and then ignored: the
    answer would still list every system's roles."""
    caller = mcp_env.tenant_a
    own = _SPECS[caller.tenant_slug]
    wiki_roles = {role.slug for role in own.roles if role.system == "wiki"}
    assert wiki_roles and len(wiki_roles) < len(own.roles)

    roles = _roles(call_tool(bearer(token_for(caller)), TOOL, {"system": "wiki"}))

    assert {row["system_slug"] for row in roles} == {"wiki"}
    assert {row["role_slug"] for row in roles} == wiki_roles


def test_a_system_of_64_characters_is_accepted_and_65_refused(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a missing or wrong cap on `system`: 65 characters would reach
    the query (and the slug column is 64), and a cap of 63 would refuse a
    legal slug. The 64 case returns no rows, which also shows the filter is an
    exact match and not a prefix."""
    headers = bearer(token_for(mcp_env.tenant_a))

    accepted = call_tool(headers, TOOL, {"system": "a" * SYSTEM_MAX})
    refused = call_tool(headers, TOOL, {"system": "a" * (SYSTEM_MAX + 1)})

    assert _roles(accepted) == []
    assert error_code(refused) == "VALIDATION"


@pytest.mark.parametrize(
    ("limit", "accepted"),
    [(0, False), (1, True), (200, True), (201, False)],
)
def test_limit_bounds(mcp_env: SeededMcp, limit: int, accepted: bool) -> None:  # noqa: F811
    """Catches a `limit` range that is off by one at either end, or that is
    accepted and then not applied: 1 must return exactly one row."""
    result = call_tool(bearer(token_for(mcp_env.tenant_a)), TOOL, {"limit": limit})

    if accepted:
        assert len(_roles(result)) == min(limit, len(_SPECS[mcp_env.tenant_a.tenant_slug].roles))
    else:
        assert error_code(result) == "VALIDATION"


def test_the_advertised_schema_pins_the_bounds_and_the_default(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Catches a bound or default that behaves right today but is not the one
    clients are told (50 default, 1 to 200, slug at most 64): the schema is
    what the agent's catalogue is compared with."""
    advertised = {tool.name: tool for tool in list_tools(bearer(token_for(mcp_env.tenant_a))).tools}
    schema: dict[str, Any] = advertised[TOOL].input_schema
    limit = schema["properties"]["limit"]
    system = schema["properties"]["system"]
    branches = system.get("anyOf", [system])

    assert (limit["default"], limit["minimum"], limit["maximum"]) == (
        LIMIT_DEFAULT,
        LIMIT_MIN,
        LIMIT_MAX,
    )
    assert any(branch.get("maxLength") == SYSTEM_MAX for branch in branches)
    assert schema.get("required", []) == []


@pytest.mark.parametrize("principal", [p.value for p in Principal])
def test_every_principal_may_call_it_and_it_is_audited(
    mcp_env: SeededMcp,  # noqa: F811
    principal: str,
) -> None:
    """Catches a tool that is read-only in name but unaudited, or one wired to
    a narrower principal set than the matrix says: each principal must get
    rows, and each call must add exactly one `tool.succeeded` event."""
    caller = mcp_env.tenant_a
    before = _audit_count(mcp_env)

    result = call_tool(bearer(token_for(caller, principals=(principal,))), TOOL, {})

    assert _roles(result), principal
    assert _audit_count(mcp_env) == before + 1
