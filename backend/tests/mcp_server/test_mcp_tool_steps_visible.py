"""`set_tool_steps_visible` and the `show_tool_steps` answer of `get_me`, through the transport.

Surface under test (written before it exists):

``mcp_server.tools.tenant_settings``
    ``SetToolStepsVisibleInput(MutatingToolInput)``: one field, ``show_tool_steps: bool``,
    plus the required ``idempotency_key`` every write carries.
    ``set_tool_steps_visible``: a registered MUTATING tool, allowed for
    ``platform_admin`` alone. It stores the flag as ``show_tool_steps`` in the
    caller's own tenant's ``settings`` JSON and keeps every other key there.

``get_me``
    Answers ``show_tool_steps: bool``. A tenant whose ``settings`` has no such key
    reads ``false``.

Cost: the tool's write and the ``get_me`` read need the real tenant row, so these
cases ride the session ``mcp_fixtures`` seam (one seeded database). Every case
starts from a tenant pair with no ``show_tool_steps`` key, set by the fixture
below, so no case depends on the order the file runs in.

No pytest-asyncio: each case is a sync function driving one ``asyncio.run``.
"""

import asyncio
import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.support.mcp import (  # noqa: F401 - fixtures are resolved by name
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

SET_TOOL_STEPS_VISIBLE = "set_tool_steps_visible"
GET_ME = "get_me"

SUCCEEDED_EVENT = "tool.succeeded"
DENIED_EVENT = "tool.denied"
REPLAYED_EVENT = "tool.replayed"
FAILED_EVENT = "tool.failed"

ALLOWED_PRINCIPAL = "platform_admin"
DENIED_PRINCIPALS = ("employee", "system_owner", "auditor", "it_admin", "manager", "hr")


# --- probes around the tool, never through it ---------------------------------


def _run_sql(database_url: str, sql: str, params: dict[str, Any]) -> list[Any]:
    async def scenario() -> list[Any]:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as conn:
                result = await conn.execute(text(sql), params)
                return list(result) if result.returns_rows else []
        finally:
            await engine.dispose()

    return asyncio.run(scenario())


def _set_settings(database_url: str, tenant_id: uuid.UUID, settings: dict[str, Any]) -> None:
    _run_sql(
        database_url,
        "UPDATE tenants SET settings = CAST(:settings AS jsonb) WHERE id = :tenant",
        {"settings": json.dumps(settings), "tenant": tenant_id},
    )


def _stored_settings(database_url: str, tenant_id: uuid.UUID) -> dict[str, Any]:
    rows = _run_sql(
        database_url, "SELECT settings FROM tenants WHERE id = :tenant", {"tenant": tenant_id}
    )
    stored: dict[str, Any] = dict(rows[0][0])
    return stored


def _audit_rows(database_url: str, tenant_id: uuid.UUID) -> list[Any]:
    return _run_sql(
        database_url,
        "SELECT event_type, subject_id FROM audit_events WHERE tenant_id = :tenant ORDER BY seq",
        {"tenant": tenant_id},
    )


def _event_types(database_url: str, tenant_id: uuid.UUID) -> list[str]:
    return [str(row.event_type) for row in _audit_rows(database_url, tenant_id)]


@pytest.fixture
def clean_settings(mcp_env: SeededMcp) -> SeededMcp:  # noqa: F811
    """Both tenants start with empty settings, so no case inherits another's flag."""
    _set_settings(mcp_env.database_url, mcp_env.tenant_a.tenant_id, {})
    _set_settings(mcp_env.database_url, mcp_env.tenant_b.tenant_id, {})
    return mcp_env


def _admin_token(env: SeededMcp) -> dict[str, str]:
    return bearer(token_for(env.tenant_a, principals=(ALLOWED_PRINCIPAL,)))


def _args(show: bool, key: str | None = None) -> dict[str, Any]:
    return {
        "show_tool_steps": show,
        "idempotency_key": key if key is not None else f"fake-key-{uuid.uuid4().hex}",
    }


def _structured(result: Any) -> dict[str, Any]:
    assert result.is_error is False, result.content
    assert result.structured_content is not None, "a successful call carries structured content"
    content: dict[str, Any] = result.structured_content
    return content


def _me_flag(user: Any) -> Any:
    return _structured(call_tool(bearer(token_for(user)), GET_ME, {}))["show_tool_steps"]


# --- the happy path -----------------------------------------------------------


def test_platform_admin_turns_steps_on_and_get_me_shows_it(
    clean_settings: SeededMcp,
) -> None:
    """Catches a handler that answers success without writing the tenant row, or
    writes under another key name, so `get_me` keeps saying false.

    The reader is the probe user (a different person from the admin), so a flag
    that only the author sees fails. The audit delta is the exact pair of
    before/after event types, so a write that leaves no record fails too.
    """
    env = clean_settings
    tenant = env.tenant_a.tenant_id
    before = _event_types(env.database_url, tenant)
    assert _me_flag(env.probe) is False

    result = call_tool(_admin_token(env), SET_TOOL_STEPS_VISIBLE, _args(True))

    assert result.is_error is False, result.content
    assert _stored_settings(env.database_url, tenant)["show_tool_steps"] is True
    assert _me_flag(env.probe) is True
    delta = _audit_rows(env.database_url, tenant)[len(before) :]
    # The two `get_me` reads add their own `tool.succeeded` rows; only the write's counts here.
    assert [
        str(row.subject_id)
        for row in delta
        if str(row.event_type) == SUCCEEDED_EVENT and str(row.subject_id) != GET_ME
    ] == [SET_TOOL_STEPS_VISIBLE]


def test_turning_steps_off_again_is_stored_as_false(clean_settings: SeededMcp) -> None:
    """Catches a handler that only ever writes true (or deletes nothing), so an
    admin could turn the steps on and never off again.

    The setting starts at true, planted around the tool, so the flip to false is
    the only thing that can make `get_me` say false.
    """
    env = clean_settings
    _set_settings(env.database_url, env.tenant_a.tenant_id, {"show_tool_steps": True})

    result = call_tool(_admin_token(env), SET_TOOL_STEPS_VISIBLE, _args(False))

    assert result.is_error is False, result.content
    assert _me_flag(env.probe) is False


def test_other_settings_keys_survive_the_write(clean_settings: SeededMcp) -> None:
    """Catches a handler that replaces the whole `settings` JSON with
    `{"show_tool_steps": ...}`, wiping every other tenant setting.

    The planted key is one this tool knows nothing about, so only a merge keeps it.
    """
    env = clean_settings
    tenant = env.tenant_a.tenant_id
    _set_settings(env.database_url, tenant, {"fake_other_setting": "kept"})

    result = call_tool(_admin_token(env), SET_TOOL_STEPS_VISIBLE, _args(True))

    assert result.is_error is False, result.content
    assert _stored_settings(env.database_url, tenant) == {
        "fake_other_setting": "kept",
        "show_tool_steps": True,
    }


def test_default_is_off_when_the_key_is_missing(clean_settings: SeededMcp) -> None:
    """Catches a `get_me` that reads the key with a true default, or raises on a
    tenant whose settings have no `show_tool_steps`: every existing tenant would
    start with the steps box showing.

    The settings are empty (set by the fixture), so the default is the only
    thing that can decide the answer, for both seeded tenants.
    """
    env = clean_settings

    assert _stored_settings(env.database_url, env.tenant_a.tenant_id) == {}
    assert _me_flag(env.tenant_a) is False
    assert _me_flag(env.tenant_b) is False


# --- the refusals ---------------------------------------------------------------


@pytest.mark.parametrize("principal", DENIED_PRINCIPALS)
def test_every_other_principal_is_refused(clean_settings: SeededMcp, principal: str) -> None:
    """Catches adding this principal to the `set_tool_steps_visible` cell of the
    matrix (or a row copied from a wider admin tool such as `rollback_request`).

    This handler runs no scope helper of its own, so the pipeline's matrix stage
    is the only guard. The arguments are valid and the tenant is the caller's own,
    so nothing else can refuse the call. The stored settings are asserted
    unchanged: a refusal that still wrote leaves a flag set by a denied person.
    `tool.denied` and not `tool.failed`: a permission decision filed as an outage
    is a false record.
    """
    env = clean_settings
    tenant = env.tenant_a.tenant_id
    before = _event_types(env.database_url, tenant)

    result = call_tool(
        bearer(token_for(env.tenant_a, principals=(principal,))),
        SET_TOOL_STEPS_VISIBLE,
        _args(True),
    )

    assert error_code(result) == "FORBIDDEN"
    assert _stored_settings(env.database_url, tenant) == {}
    assert _event_types(env.database_url, tenant) == [*before, DENIED_EVENT]


def test_a_call_without_an_idempotency_key_is_validation_and_writes_nothing(
    clean_settings: SeededMcp,
) -> None:
    """Catches an `idempotency_key` that is optional on `SetToolStepsVisibleInput`:
    a keyless call would write, so a retried click is unguarded.

    The caller holds the row's one cell and the flag is a valid bool, so the
    missing key is the only thing that can produce `VALIDATION`.
    """
    env = clean_settings
    arguments = _args(True)
    del arguments["idempotency_key"]

    result = call_tool(_admin_token(env), SET_TOOL_STEPS_VISIBLE, arguments)

    assert error_code(result) == "VALIDATION"
    assert _stored_settings(env.database_url, env.tenant_a.tenant_id) == {}


# --- the tenant ---------------------------------------------------------------


def test_the_change_is_only_for_the_callers_tenant(clean_settings: SeededMcp) -> None:
    """Catches a handler that updates every tenant row (an `UPDATE` with no
    `WHERE`), or one that honours a tenant named in the arguments.

    The extra `tenant_id` in the arguments names tenant B. The tool forbids
    extras, so the call is either refused or ignores it: in both cases tenant B
    must still read false and its stored settings stay empty. Tenant A, the
    token's tenant, is read back as true only when the call went through, so a
    refusal of the whole call is not enough to pass this case.
    """
    env = clean_settings

    call_tool(
        _admin_token(env),
        SET_TOOL_STEPS_VISIBLE,
        {**_args(True), "tenant_id": str(env.tenant_b.tenant_id)},
    )
    clean_call = call_tool(_admin_token(env), SET_TOOL_STEPS_VISIBLE, _args(True))

    assert clean_call.is_error is False, clean_call.content
    assert _me_flag(env.tenant_a) is True
    assert _me_flag(env.tenant_b) is False
    assert _stored_settings(env.database_url, env.tenant_b.tenant_id) == {}


# --- the key ------------------------------------------------------------------


def test_same_idempotency_key_replays(clean_settings: SeededMcp) -> None:
    """Catches a `set_tool_steps_visible` registered as non-mutating, so dispatch
    never reaches `run_once`: the second call with the same key would write again.

    Between the two calls the flag is flipped back to false around the tool. A
    replay returns the stored answer and leaves it false; a second run would set
    it true again. The audit delta holds one success and one `tool.replayed`.
    """
    env = clean_settings
    tenant = env.tenant_a.tenant_id
    arguments = _args(True, key="fake-key-tool-steps-replay")
    before = _event_types(env.database_url, tenant)

    first = call_tool(_admin_token(env), SET_TOOL_STEPS_VISIBLE, arguments)
    _set_settings(env.database_url, tenant, {"show_tool_steps": False})
    second = call_tool(_admin_token(env), SET_TOOL_STEPS_VISIBLE, arguments)

    assert _structured(second) == _structured(first)
    assert _stored_settings(env.database_url, tenant) == {"show_tool_steps": False}
    delta = _event_types(env.database_url, tenant)[len(before) :]
    assert delta.count(SUCCEEDED_EVENT) == 1
    assert delta.count(REPLAYED_EVENT) == 1
    assert FAILED_EVENT not in delta
