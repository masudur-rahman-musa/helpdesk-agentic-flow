"""An earlier change, sections 1 and 6: `list_sod_rules`, through the transport.

Surface under test (written before it exists):

``mcp_server.tools.rules``
    ``ListSodRulesInput(BaseModel)`` - NO fields at all: no tenant, no user,
    no rule id. The tenant is the token's and the rules are that tenant's own
    rows, so there is nothing for a caller to choose.
    ``ListSodRulesOutput(BaseModel)`` - one field, ``rules``, a list of rows
    carrying exactly ``rule_id``, ``role_a_id``, ``role_a``, ``role_b_id``,
    ``role_b``, ``rationale`` and ``policy_ref``. Each resolved side is
    ``null`` or a block of exactly ``role_id``, ``slug``, ``name``,
    ``system_slug`` and ``system_name``.
    ``list_sod_rules`` - a registered READ tool (never mutating, no
    idempotency key) whose wire name is ``list_sod_rules`` and whose registry
    ``event_type`` is the generic ``tool.succeeded``: the board writes
    nothing, so there is no domain event to append.

``helpdesk.access.rules``
    ``read_sod_rules(session)``, the one read behind it. Its own cases live in
    ``packages/helpdesk/tests/test_access_sod_rules.py`` - the order, the
    three statements and the unresolvable side are pinned there, against the
    engine, and this file pins what reaches the WIRE.

SPEC section 14 gives ``/admin/rules`` to ``it_admin`` and ``platform_admin``,
so SPEC section 7's row for this tool has exactly those two cells at
``Scope.ANY`` (decision E). There is NO scope helper on this tool, so the
pipeline's matrix stage IS the whole permission check and the five-principal
case below is its kill (a project rule - the handler never re-runs
``rbac.authorize``, so there is no second copy of that guard to hide behind).

Everything here is a READ: the tool takes no arguments, no key is derived from
client input and nothing is written.

Audit deltas are asserted as exact ordered event-type lists against a before
snapshot, never as counts: a duplicated summary, a domain event this tool has
no business writing, or a denial filed as an outage is invisible to a count.

Fixture cost: everything rides an earlier change's session-scoped
``mcp_fixtures`` seam - one seeded throwaway database, one dedicated Redis
index flushed per test. The two tenants and their six seeded rules each come
from ``mcp_env`` itself and no rule is planted by name; the
one case that needs a tenant with NO rules plants a bare tenant of its own and
removes it again, because per-test isolation here is a namespace and never a
rebuilt database.

No pytest-asyncio: each case is a sync function driving one ``asyncio.run``.
"""

import asyncio
import uuid
from datetime import date
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from helpdesk.mcp_server.tools.registry import REGISTRY
from tests.support.mcp import (  # noqa: F401 - fixtures are resolved by name
    SeededMcp,
    SeededUser,
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

LIST_SOD_RULES = "list_sod_rules"
SET_KILL_SWITCH = "set_kill_switch"

# The audit contract as literals, never imported from the module under test:
# reading these strings off the code would let a rename pass unnoticed.
SUCCEEDED_EVENT = "tool.succeeded"
DENIED_EVENT = "tool.denied"

# SPEC section 8.1's kill switch key shape, as a literal for the same reason.
KEY_PREFIX_LITERAL = "killswitch"

# The envelope, the row and the block, written out: what
# 24eb parses and 24f renders. An extra key is as much a defect as a missing
# one - an `unresolvable` flag beside a null block would be a second, ambiguous
# answer to the question nullability already answers (decision B).
ENVELOPE_KEYS = {"rules"}
ROW_KEYS = {
    "rule_id",
    "role_a_id",
    "role_a",
    "role_b_id",
    "role_b",
    "rationale",
    "policy_ref",
}
BLOCK_KEYS = {"role_id", "slug", "name", "system_slug", "system_name"}

# SPEC section 7's `list_sod_rules` row: the two cells SPEC section 14 gives
# `/admin/rules`. Checked against `rollback_request`'s row in
# `helpdesk/auth/rbac.py`, which carries the same pair.
ALLOWED_PRINCIPALS = ("it_admin", "platform_admin")
DENIED_PRINCIPALS = ("employee", "manager", "system_owner", "hr", "auditor")

# The seeded corpus this file reads back. `scripts/seed.py` plants six SoD
# rules per tenant; the assertions below compare against whatever the database
# actually holds and use this only as the floor that makes the comparison
# worth making (a project rule: nothing is addressed by name).
MINIMUM_SEEDED_RULES = 4

_RULES_SQL = (
    "SELECT r.id AS rule_id, r.role_a_id, r.role_b_id, r.rationale, r.policy_ref,"
    " ra.slug AS a_slug, ra.name AS a_name,"
    " sa.slug AS a_system_slug, sa.name AS a_system_name,"
    " rb.slug AS b_slug, rb.name AS b_name,"
    " sb.slug AS b_system_slug, sb.name AS b_system_name"
    " FROM sod_rules r"
    " JOIN roles ra ON ra.id = r.role_a_id"
    " JOIN roles rb ON rb.id = r.role_b_id"
    " JOIN systems sa ON sa.id = ra.system_id"
    " JOIN systems sb ON sb.id = rb.system_id"
    " WHERE r.tenant_id = :tenant"
    " ORDER BY r.policy_ref, r.id"
)


def _structured(result: Any) -> dict[str, Any]:
    assert result.is_error is False, result.content
    assert result.structured_content is not None, "a successful call carries structured content"
    content: dict[str, Any] = result.structured_content
    return content


def _token(env: SeededMcp, principal: str) -> dict[str, str]:
    """A tenant-A token carrying ONE of SPEC section 7's two cells on this row."""
    return bearer(token_for(env.tenant_a, principals=(principal,)))


def _rules(env: SeededMcp, principal: str = "it_admin") -> list[dict[str, Any]]:
    """The whole board as one permitted principal reads it."""
    payload = _structured(call_tool(_token(env, principal), LIST_SOD_RULES, {}))
    assert set(payload) == ENVELOPE_KEYS, payload
    rows: list[dict[str, Any]] = payload["rules"]
    return rows


# --- probes around the tool, never through it ------------------------------------


def _query(database_url: str, sql: str, params: dict[str, Any]) -> list[Any]:
    async def scenario() -> list[Any]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as conn:
                return list(await conn.execute(text(sql), params))
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _execute(database_url: str, statements: list[tuple[str, dict[str, Any]]]) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as conn:
                for sql, params in statements:
                    await conn.execute(text(sql), params)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def _expected_rows(database_url: str, tenant_id: uuid.UUID) -> list[dict[str, Any]]:
    """Every rule of one tenant, resolved by the database rather than the tool.

    Built with a join written here so the expectation is independent of the
    implementation: a handler that resolved the wrong side, echoed the rule's
    ids into the blocks or read the system off the other role answers rows
    this comparison does not contain.
    """
    rows = _query(database_url, _RULES_SQL, {"tenant": tenant_id})
    return [
        {
            "rule_id": str(row.rule_id),
            "role_a_id": str(row.role_a_id),
            "role_a": {
                "role_id": str(row.role_a_id),
                "slug": str(row.a_slug),
                "name": str(row.a_name),
                "system_slug": str(row.a_system_slug),
                "system_name": str(row.a_system_name),
            },
            "role_b_id": str(row.role_b_id),
            "role_b": {
                "role_id": str(row.role_b_id),
                "slug": str(row.b_slug),
                "name": str(row.b_name),
                "system_slug": str(row.b_system_slug),
                "system_name": str(row.b_system_name),
            },
            "rationale": str(row.rationale),
            "policy_ref": str(row.policy_ref),
        }
        for row in rows
    ]


def _audit_event_types(database_url: str, tenant_id: uuid.UUID) -> list[str]:
    rows = _query(
        database_url,
        "SELECT event_type FROM audit_events WHERE tenant_id = :tenant ORDER BY seq",
        {"tenant": tenant_id},
    )
    return [str(row[0]) for row in rows]


def _audit_rows(database_url: str, tenant_id: uuid.UUID) -> list[Any]:
    """Every (event_type, actor_ref, subject_type, subject_id) of one tenant."""
    return _query(
        database_url,
        "SELECT event_type, actor_ref, subject_type, subject_id FROM audit_events"
        " WHERE tenant_id = :tenant ORDER BY seq",
        {"tenant": tenant_id},
    )


def _delete_key(redis_url: str, key: str) -> None:
    """Clean-up around the tool: no case may leave a stopped tool behind."""

    async def scenario() -> None:
        client: Redis = Redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
        try:
            await client.delete(key)
        finally:
            await client.aclose()

    asyncio.run(scenario())


def _tenant_key(tenant_id: uuid.UUID, tool_name: str) -> str:
    return f"{KEY_PREFIX_LITERAL}:{tenant_id}:{tool_name}"


def _plant_ruleless_tenant(database_url: str) -> SeededUser:
    """A tenant with a department and one admin, and no rules of any kind.

    The seeded tenants both carry six SoD rules, so the empty answer has
    nowhere else to come from. Planted rather than fixtured, and removed again
    by `_remove_tenant`: the database is session-scoped and shared (standing
    rule 7), so a case that needs a different shape makes its own namespace
    inside it instead of building a second database.
    """
    marker = uuid.uuid4().hex[:10]
    tenant_id, department_id, user_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    _execute(
        database_url,
        [
            (
                "INSERT INTO tenants (id, slug, name, residency, settings)"
                " VALUES (:id, :slug, :name, 'eu', '{}'::jsonb)",
                {
                    "id": tenant_id,
                    "slug": f"fake-ruleless-{marker}",
                    "name": "Fake Tenant (no SoD rules)",
                },
            ),
            (
                "INSERT INTO departments (id, tenant_id, name, manager_user_id)"
                " VALUES (:id, :tenant, :name, NULL)",
                {"id": department_id, "tenant": tenant_id, "name": "Fake Department"},
            ),
            (
                "INSERT INTO users (id, tenant_id, department_id, email, display_name,"
                " password_hash, principals, status, start_date, end_date)"
                " VALUES (:id, :tenant, :department, :email, :display_name, :password_hash,"
                " ARRAY['it_admin']::varchar[], 'active', :start_date, NULL)",
                {
                    "id": user_id,
                    "tenant": tenant_id,
                    "department": department_id,
                    "email": f"fake.admin.{marker}@fake.invalid",
                    "display_name": "Fake Ruleless Admin",
                    # Not a hash and not a secret: this user never
                    # authenticates, tokens are minted directly.
                    "password_hash": "not-a-real-hash-this-user-never-logs-in",
                    "start_date": date(2026, 1, 1),
                },
            ),
        ],
    )
    return SeededUser(
        tenant_id=tenant_id,
        tenant_slug=f"fake-ruleless-{marker}",
        user_id=user_id,
        email=f"fake.admin.{marker}@fake.invalid",
        display_name="Fake Ruleless Admin",
        department_id=department_id,
        principals=("it_admin",),
        status="active",
    )


def _remove_tenant(database_url: str, tenant: SeededUser) -> None:
    """Take the planted tenant back out, children first."""
    _execute(
        database_url,
        [
            ("DELETE FROM audit_events WHERE tenant_id = :tenant", {"tenant": tenant.tenant_id}),
            ("DELETE FROM users WHERE tenant_id = :tenant", {"tenant": tenant.tenant_id}),
            ("DELETE FROM departments WHERE tenant_id = :tenant", {"tenant": tenant.tenant_id}),
            ("DELETE FROM tenants WHERE id = :tenant", {"tenant": tenant.tenant_id}),
        ],
    )


# --- the happy path (criteria 3 and 4) -------------------------------------------


@pytest.mark.parametrize("principal", ALLOWED_PRINCIPALS)
def test_the_board_answers_the_envelope_and_an_exact_seven_key_row(
    mcp_env: SeededMcp,  # noqa: F811
    principal: str,
) -> None:
    """Criteria 3 and 4, as the literal 24eb parses and 24f renders, for BOTH
    cells of SPEC section 7's row.

    KILL: a handler answering a bare list or a mapping of rule id to rule; a
    row that drops the two top-level ids once the blocks resolve (decision B
    keeps them always present, and an unresolvable row is then unactionable);
    a block short of `system_slug`, which the page needs to say WHICH system
    the role belongs to; and a resolution that reads the system off the other
    side of the pair - the seeded corpus pairs roles from two different
    systems, so a block filled from the wrong role answers a real name that
    this comparison does not contain.

    The whole ordered list is compared against a join written in this file,
    so the values are pinned by the database rather than by the handler, and
    the key sets are asserted exactly on every row and every block: an eighth
    field or a sixth block field is as red as a missing one.

    Parametrised over both permitted principals so the second cell is pinned
    by a SUCCESS and not only by the denial sweep: a matrix row carrying
    `platform_admin` alone passes every other case in this file.
    """
    expected = _expected_rows(mcp_env.database_url, mcp_env.tenant_a.tenant_id)
    assert len(expected) >= MINIMUM_SEEDED_RULES, (
        f"the seeded tenant must carry at least {MINIMUM_SEEDED_RULES} SoD rules, "
        f"found {len(expected)}"
    )

    rules = _rules(mcp_env, principal)

    for row in rules:
        assert set(row) == ROW_KEYS, row
        for side in ("role_a", "role_b"):
            assert row[side] is not None, row
            assert set(row[side]) == BLOCK_KEYS, row[side]
    assert rules == expected


def test_the_rows_arrive_in_the_policy_ref_then_rule_id_order(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Criterion 5 on the wire: the order the helper applies survives the tool.

    KILL: a handler that RE-ORDERS what the helper handed it - a sort by the
    resolved system or role name (decision C rejects it: a rename would move
    the rows and an unresolvable rule has no name to sort by), or a mapping
    keyed by rule id rebuilt into a list. The seeded corpus pairs roles whose
    systems and names sort differently from their `policy_ref`s, so either
    answers a sequence this comparison rejects.

    NOT a kill for a missing ORDER BY: `scripts/seed.py` inserts the six rules
    in `policy_ref` order, so an unordered read comes back sorted here by
    coincidence. That control is pinned where it lives, against the engine and
    against a fixture planted in the reverse order, by
    `test_access_sod_rules.py::test_the_rows_are_ordered_by_policy_ref_then_rule_id`,
    which is also where the tie-break is exercised: the seeded corpus carries
    one `policy_ref` per rule and cannot exercise it at all.
    """
    rules = _rules(mcp_env)

    keys = [(row["policy_ref"], row["rule_id"]) for row in rules]
    assert keys == sorted(keys)
    assert len(keys) == len(set(keys))


def test_the_tool_takes_no_arguments_at_all(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Criterion 2 and a project rule: the input model carries NO fields.

    KILL: an input that accepts a `tenant_id` (or a `rule_id`, or a `limit`)
    so a caller can choose whose rules they read. The declared field set is
    asserted EMPTY, which no later field can ride along into, and the wire
    half proves `extra="forbid"` rather than `ignore`: an ignored `tenant_id`
    is a caller who believes they are reading another tenant's rules and is
    being answered with their own.

    The `tenant_id` sent below is the caller's OWN, so the
    refusal cannot be the tenant guard answering on the model's behalf - this
    caller holds a cell SPEC section 7 gives the row, every other stage
    passes, and nothing but the empty model can refuse it.
    """
    spec = REGISTRY[LIST_SOD_RULES]

    assert spec.input_model.model_fields == {}

    result = call_tool(
        _token(mcp_env, "it_admin"),
        LIST_SOD_RULES,
        {"tenant_id": str(mcp_env.tenant_a.tenant_id)},
    )

    assert error_code(result) == "VALIDATION"


# --- SPEC section 7's five empty cells (criterion 9) -----------------------------


@pytest.mark.parametrize("principal", DENIED_PRINCIPALS)
def test_the_five_denied_principals_read_no_rules(
    mcp_env: SeededMcp,  # noqa: F811
    principal: str,
) -> None:
    """SPEC section 7's five empty cells on this row, and the record they leave.

    KILL: adding this principal to the `list_sod_rules` cell of the RBAC
    matrix. This handler runs no scope helper and therefore never re-runs
    `rbac.authorize` (plan section 2), so the pipeline's matrix stage is the
    only guard that can refuse here and deleting it hands every one of these
    five the tenant's SoD configuration (a project rule does not apply:
    there is no second copy of the guard). `auditor` is the cell that matters
    most - it holds one on `search_audit` and none here, because the rules are
    configuration and not a record of what happened (decision E).

    The call carries no arguments at all, which are the only valid arguments
    this tool has, so no validation stage can produce this answer.
    `tool.denied` rather than `tool.failed`: a permission decision filed as an
    outage is a false record, and a count-only assertion cannot tell them
    apart.
    """
    tenant_id = mcp_env.tenant_a.tenant_id
    events_before = _audit_event_types(mcp_env.database_url, tenant_id)

    result = call_tool(
        bearer(token_for(mcp_env.tenant_a, principals=(principal,))),
        LIST_SOD_RULES,
        {},
    )

    assert error_code(result) == "FORBIDDEN"
    assert _audit_event_types(mcp_env.database_url, tenant_id) == [
        *events_before,
        DENIED_EVENT,
    ]


# --- the token's tenant and no other (criterion 7) -------------------------------


def test_another_tenants_rules_are_invisible_to_this_caller(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Criterion 7 on the wire: the rules are the token's tenant's own rows.

    KILL: a handler that opens its own session, or one that binds a tenant
    that came from anywhere but the token. Both seeded tenants
    carry six rules of the same shape, so a read that lost the tenant filter
    answers twelve plausible rows and only a comparison against the OTHER
    tenant's ids can see it. The two id sets are asserted disjoint, and
    tenant B's set is asserted non-empty first, so a fixture with nothing to
    leak could not make this pass.

    Both tenants are read through their own token in the same case, so a
    handler that answered tenant B's rules to everybody fails on one side or
    the other.
    """
    rules_a = _rules(mcp_env)
    ids_a = {row["rule_id"] for row in rules_a}
    ids_b = {
        str(row.rule_id)
        for row in _query(
            mcp_env.database_url,
            "SELECT id AS rule_id FROM sod_rules WHERE tenant_id = :tenant",
            {"tenant": mcp_env.tenant_b.tenant_id},
        )
    }

    assert ids_b, "tenant B must hold rules for this case to prove anything"
    assert ids_a == {
        str(row.rule_id)
        for row in _query(
            mcp_env.database_url,
            "SELECT id AS rule_id FROM sod_rules WHERE tenant_id = :tenant",
            {"tenant": mcp_env.tenant_a.tenant_id},
        )
    }
    assert ids_a.isdisjoint(ids_b)


# --- a tenant with no rules (criterion 10) ---------------------------------------


def test_a_tenant_with_no_rules_answers_an_empty_list_as_a_success(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """Criterion 10: an unconfigured tenant is answered, never refused.

    KILL: a handler that turns the empty read into `NOT_FOUND` (the shape
    every id-taking tool in this suite uses, and the easiest thing to copy),
    or one that lets the empty resolution raise. Either gives a new tenant's
    admin a broken `/admin/rules` page instead of an empty one, and no other
    case here can see it because both seeded tenants carry six rules.

    The envelope is asserted present with an empty list rather than merely
    "not an error": a success carrying no `rules` key at all would leave 24eb
    parsing `undefined`.
    """
    ruleless = _plant_ruleless_tenant(mcp_env.database_url)

    try:
        result = call_tool(bearer(token_for(ruleless)), LIST_SOD_RULES, {})

        payload = _structured(result)
        assert set(payload) == ENVELOPE_KEYS, payload
        assert payload["rules"] == []
    finally:
        _remove_tenant(mcp_env.database_url, ruleless)


# --- the board can be stopped like any other tool (criterion 11) -----------------


def test_a_switch_on_the_rules_board_answers_tool_disabled(
    mcp_env: SeededMcp,  # noqa: F811
    mcp_redis_url: str,  # noqa: F811
) -> None:
    """Criterion 11: this read is not in `PROTECTED_TOOLS` and is not special.

    The switch is set through `set_kill_switch` rather than planted in Redis,
    which is what makes this two kills in one:

    * adding `list_sod_rules` to `PROTECTED_TOOLS` - the plan adds nothing to
      that frozenset - turns the trip itself into `POLICY_BLOCKED` and this
      case red;
    * a board wired around the pipeline (its own route, its own handler, a
      registry entry the switch stage never sees) answers the second call with
      data, where every other registered tool would answer `TOOL_DISABLED`.

    The trip is made by `platform_admin`, which holds the only cell
    `set_kill_switch` has, and the stopped call is made by `it_admin`, which
    holds one on the tool under test: so a `FORBIDDEN` here would mean the
    matrix ran instead of the switch stage, and `TOOL_DISABLED` is asserted
    exactly rather than "some error".
    """
    tenant_id = mcp_env.tenant_a.tenant_id
    key = _tenant_key(tenant_id, LIST_SOD_RULES)

    try:
        trip = call_tool(
            _token(mcp_env, "platform_admin"),
            SET_KILL_SWITCH,
            {
                "tool_name": LIST_SOD_RULES,
                "enabled": True,
                "reason": f"fake rules board stop {uuid.uuid4().hex}",
                "scope": str(tenant_id),
            },
        )
        assert trip.is_error is False, trip.content

        stopped = call_tool(_token(mcp_env, "it_admin"), LIST_SOD_RULES, {})

        assert error_code(stopped) == "TOOL_DISABLED"
    finally:
        _delete_key(mcp_redis_url, key)


# --- what the record says -------------------------------------


def test_one_call_writes_exactly_one_event_naming_the_tool_and_the_caller(
    mcp_env: SeededMcp,  # noqa: F811
) -> None:
    """a project rule: what the audit log carries for one rules-board read.

    KILL: a read that declares a domain event type of its own, or appends one
    beside the summary. A `sod.rule.read` per render would put a governance
    record in the hash chain for a call that changed nothing, and
    `search_audit` would show the rules being touched every time an admin
    opened the page. The delta is the exact ordered list, so a duplicated
    summary fails too.

    The subject is `list_sod_rules` and the actor is the caller's user id: an
    event whose subject is some other tool, or whose actor is the tenant,
    answers the wrong question for an auditor asking who read the
    configuration and when.
    """
    tenant_id = mcp_env.tenant_a.tenant_id
    events_before = _audit_rows(mcp_env.database_url, tenant_id)

    _rules(mcp_env)

    delta = _audit_rows(mcp_env.database_url, tenant_id)[len(events_before) :]

    assert [str(row.event_type) for row in delta] == [SUCCEEDED_EVENT]
    assert str(delta[0].subject_type) == "tool"
    assert str(delta[0].subject_id) == LIST_SOD_RULES
    assert str(delta[0].actor_ref) == str(mcp_env.tenant_a.user_id)
