"""An earlier change, section 1 criterion 9, section 6: every tool x every principal.

An earlier change owed item (a): the matrix was proved through `authorize`, never
through the transport. A guard that holds in the pure function and is not
wired into the pipeline is not a guard, so every row below is driven through
a real MCP client session over the real streamable HTTP transport.

The expectation is the design written out literally here, the same way
``packages/helpdesk/tests/test_rbac.py`` writes it: importing ``_MATRIX`` and
reimplementing ``authorize`` would make the test drift together with the code
it is supposed to hold still.

Exhaustiveness is over the tools the registry HOLDS.
the design asks for every tool; the rest land as the tools land (an earlier change,
scope note 2). What is exhaustive here is the principal axis: all seven,
plus the two ways a token can carry no principal the matrix recognises.

A permitted call must actually succeed for the assertion to pin RBAC rather
than a later stage, so each mutating tool is called with minimally valid
arguments (a fresh idempotency key per call). Denials need no arguments at
all: RBAC runs before input validation, which is itself pinned by
``test_mcp_pipeline.py``'s criterion-5 pair.

No pytest-asyncio: each case is a sync function driving one ``asyncio.run``.
"""

import uuid
from functools import cache
from typing import Any

import pytest
from helpdesk.auth.rbac import Principal
from mcp_fixtures import (  # noqa: F401 - fixtures are resolved by name
    SeededMcp,
    bearer,
    call_tool,
    error_code,
    mcp_env,
    mcp_pg,
    mcp_redis_base_url,
    mcp_redis_url,
    seeded_mcp_db,
    token_for,
)
from mcp_server.registry import REGISTRY

from scripts.seed import CUAN, NORDLICHT, TenantRows, generate_tenant

PRINCIPALS: tuple[str, ...] = (
    "employee",
    "manager",
    "system_owner",
    "hr",
    "it_admin",
    "auditor",
    "platform_admin",
)

# the design, written out per tool and per principal. True = the matrix permits
# this principal to call this tool. All seven registered tools are granted to
# every principal by the design (the reads are self-scoped, the three helpdesk
# mutations self-only or any, and every principal may classify and list their
# own requests); a future tool with a narrower row goes here as an explicit
# dict, never as `dict.fromkeys(..., True)` copied by habit.
MATRIX: dict[str, dict[str, bool]] = {
    "get_me": dict.fromkeys(PRINCIPALS, True),
    "list_my_access": dict.fromkeys(PRINCIPALS, True),
    "reset_own_password": dict.fromkeys(PRINCIPALS, True),
    "unlock_own_account": dict.fromkeys(PRINCIPALS, True),
    "create_ticket": dict.fromkeys(PRINCIPALS, True),
    "classify_access_request": dict.fromkeys(PRINCIPALS, True),
    "list_my_requests": dict.fromkeys(PRINCIPALS, True),
}


@cache
def _tenant_rows(slug: str) -> TenantRows:
    """The deterministic seed rows for one tenant, computed once: a classify
    call needs a real role id in the caller's own tenant."""
    return generate_tenant({CUAN.slug: CUAN, NORDLICHT.slug: NORDLICHT}[slug])


def _arguments_for(tool_name: str, env: SeededMcp) -> dict[str, Any]:
    """Minimally valid arguments, so a permitted call exercises RBAC, not
    validation. Mutating tools need a fresh idempotency key per call; the
    ticket title is unique so no case replays another's row; classify names
    the caller as target with a real seeded role (both tenants seed
    wiki.viewer), so a permitted call actually succeeds."""
    if tool_name in {"reset_own_password", "unlock_own_account"}:
        return {"idempotency_key": f"fake-key-{uuid.uuid4().hex}"}
    if tool_name == "create_ticket":
        return {
            "kind": "general",
            "title": f"fake matrix ticket {uuid.uuid4().hex}",
            "body": "fake body",
            "idempotency_key": f"fake-key-{uuid.uuid4().hex}",
        }
    if tool_name == "classify_access_request":
        rows = _tenant_rows(env.tenant_a.tenant_slug)
        assert rows.tenant_id == env.tenant_a.tenant_id
        return {
            "target_user_id": str(env.tenant_a.user_id),
            "role_id": str(rows.role_ids[("wiki", "viewer")]),
        }
    return {}


# The two shapes of a token that carries nothing the matrix recognises.
# `authorize` ignores principal strings outside the enum, so both deny.
NO_PRINCIPAL_TOKENS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("no principals at all", ()),
    ("only a principal outside the enum", ("not_a_principal",)),
    ("only a near-miss of a real principal", ("Employee",)),
)

CASES = [(tool_name, principal) for tool_name in sorted(REGISTRY) for principal in PRINCIPALS]


def test_the_principal_axis_is_the_whole_enum() -> None:
    """A row missing from the enum list would silently shrink the matrix."""
    assert set(PRINCIPALS) == {principal.value for principal in Principal}
    assert len(PRINCIPALS) == 7


def test_every_registered_tool_has_a_matrix_row() -> None:
    """A tool registered without a row here would go untested rather than fail."""
    assert set(REGISTRY) == set(MATRIX)


@pytest.mark.parametrize(("tool_name", "principal"), CASES, ids=[f"{t}-{p}" for t, p in CASES])
def test_matrix_expectation_holds_through_the_transport(
    mcp_env: SeededMcp,  # noqa: F811
    tool_name: str,
    principal: str,
) -> None:
    """A token carrying ONLY this principal: refused FORBIDDEN exactly when
    the matrix denies, and not refused when it allows."""
    token = token_for(mcp_env.tenant_a, principals=(principal,))

    result = call_tool(bearer(token), tool_name, _arguments_for(tool_name, mcp_env))

    if MATRIX[tool_name][principal]:
        assert result.is_error is False, (
            f"{principal} is permitted to call {tool_name} by SPEC §7, "
            f"but the transport refused: {result.content!r}"
        )
    else:
        assert error_code(result) == "FORBIDDEN", (
            f"{principal} is denied {tool_name} by SPEC §7 and must be refused FORBIDDEN"
        )


@pytest.mark.parametrize(
    ("label", "principals"), NO_PRINCIPAL_TOKENS, ids=[label for label, _ in NO_PRINCIPAL_TOKENS]
)
@pytest.mark.parametrize("tool_name", sorted(REGISTRY))
def test_a_token_with_no_recognised_principal_is_refused(
    mcp_env: SeededMcp,  # noqa: F811
    tool_name: str,
    label: str,
    principals: tuple[str, ...],
) -> None:
    """The refusal direction, through the transport. Every registered tool
    allows every real principal, so this is where FORBIDDEN is actually
    reachable — without it the matrix test would assert only that nothing is
    ever refused. No arguments on purpose: RBAC answers before validation."""
    token = token_for(mcp_env.tenant_a, principals=principals)

    result = call_tool(bearer(token), tool_name, {})

    assert error_code(result) == "FORBIDDEN", label
