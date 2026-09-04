"""An earlier change section 6: per-tool kill switches, written before they exist.

Expected implementation surface (`helpdesk/killswitch/switch.py`, re-exported
from `helpdesk.killswitch`):

``SwitchState``
    Frozen Pydantic v2 model: ``reason: str``, ``actor: str``, ``at: datetime``.
    Frozen because a switch's provenance is audit evidence; nothing downstream
    may edit who tripped it or why.

``GLOBAL``
    The sentinel naming the platform-wide scope, so a caller cannot express it
    as a free-form string that could collide with a tenant id.

``KillSwitch(client)``
    Built over an async ``redis.asyncio.Redis`` client (injected, so tests can
    point it at a dedicated db index).

    * ``await check(tenant_id, tool) -> None`` — returns when allowed, raises
      ``ToolDisabledError`` when tripped. Reads ``killswitch:global:{tool}``
      first, then ``killswitch:{tenant_id}:{tool}``; first hit wins (an earlier change
      decision D: a global switch is the platform's emergency stop and must not
      be overridable per tenant).
    * ``await trip(scope, tool, reason, actor) -> SwitchState``
    * ``await clear(scope, tool) -> None``
    * ``await status(tenant_id, tool) -> SwitchState | None``

Fail closed (decision C): an unreadable value denies with ``TOOL_DISABLED`` and
a message saying the switch is unreadable; an unreachable Redis denies with
``INTERNAL``. A switch that cannot be read is never treated as absent.

Style follows the existing DB tests: no pytest-asyncio in this project, so each
case is a sync function driving one ``asyncio.run`` scenario, and every client
lives and dies inside that one loop.
"""

import asyncio
import json
import socket
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from helpdesk.auth.rbac import Tool
from helpdesk.errors import ErrorCode, ToolDisabledError, ToolError
from helpdesk.killswitch import (
    ACTOR_MAX_LENGTH,
    GLOBAL,
    REASON_MAX_LENGTH,
    TOOL_NAME_MAX_LENGTH,
    InvalidSwitchTargetError,
    KillSwitch,
    SwitchState,
)
from pydantic import ValidationError
from redis.asyncio import Redis

TOOL = "revoke_access"
OTHER_TOOL = "grant_access"

# Obviously fake identities: two unrelated tenants and one fake admin.
TENANT_A = uuid.UUID(int=0xA11CE)
TENANT_B = uuid.UUID(int=0xB0B)
ACTOR = "fake-admin@example.invalid"
REASON = "fake incident 0001, access grants paused"
OTHER_REASON = "fake tenant-local pause, unrelated to the incident"


@asynccontextmanager
async def _client(url: str) -> AsyncIterator[Redis]:
    client: Redis = Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
    try:
        yield client
    finally:
        await client.aclose()


def _state_json(reason: str = REASON, actor: str = ACTOR) -> str:
    """A hand-written switch value, so the stored shape is pinned independently
    of whatever `trip` happens to serialise."""
    return json.dumps({"reason": reason, "actor": actor, "at": "2026-09-04T10:00:00+00:00"})


def _unused_port() -> int:
    """A port nothing is listening on: bind, read the port, close it again."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


# --- the key shape, pinned from both sides ------------------------------------
# the design names these exact keys. Drift would silently disable nothing: the
# switch would appear to trip while `check` kept allowing the call.


def test_global_key_string_is_exactly_the_spec_shape(redis_url: str) -> None:
    """Written by hand, read through `check`: proves `check` looks where an
    operator (or the future admin route) writes."""

    async def scenario() -> None:
        async with _client(redis_url) as client:
            await client.set(f"killswitch:global:{TOOL}", _state_json())
            with pytest.raises(ToolDisabledError):
                await KillSwitch(client).check(TENANT_A, TOOL)

    asyncio.run(scenario())


def test_tenant_key_string_is_exactly_the_spec_shape(redis_url: str) -> None:
    """The tenant key interpolates the uuid in its canonical hyphenated form."""

    async def scenario() -> None:
        async with _client(redis_url) as client:
            await client.set(f"killswitch:{TENANT_A}:{TOOL}", _state_json())
            with pytest.raises(ToolDisabledError):
                await KillSwitch(client).check(TENANT_A, TOOL)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("scope", "expected_key"),
    [
        pytest.param(GLOBAL, f"killswitch:global:{TOOL}", id="global"),
        pytest.param(TENANT_A, f"killswitch:{TENANT_A}:{TOOL}", id="tenant"),
    ],
)
def test_trip_writes_exactly_the_spec_key(redis_url: str, scope: Any, expected_key: str) -> None:
    """The other side of the pin: what `trip` writes is the raw key an operator
    inspecting Redis, or the row 24 admin toggle, will expect."""

    async def scenario() -> None:
        async with _client(redis_url) as client:
            await KillSwitch(client).trip(scope, TOOL, REASON, ACTOR)
            raw = await client.get(expected_key)
            assert raw is not None, f"trip did not write {expected_key}"
            payload: Mapping[str, Any] = json.loads(raw)
            assert payload["reason"] == REASON
            assert payload["actor"] == ACTOR
            # `at` must round-trip as a timestamp, not as an opaque blob.
            assert isinstance(datetime.fromisoformat(str(payload["at"])), datetime)
            assert set(await client.keys("killswitch:*")) == {expected_key.encode()}

    asyncio.run(scenario())


# --- the allowed path ---------------------------------------------------------


def test_no_switch_means_the_call_proceeds(redis_url: str) -> None:
    """The default must be allow, or every tool call would need a switch set."""

    async def scenario() -> None:
        async with _client(redis_url) as client:
            await KillSwitch(client).check(TENANT_A, TOOL)  # must not raise

    asyncio.run(scenario())


def test_status_is_none_when_no_switch_is_set(redis_url: str) -> None:
    """An admin UI must be able to tell "not tripped" from "tripped, unknown why"."""

    async def scenario() -> None:
        async with _client(redis_url) as client:
            assert await KillSwitch(client).status(TENANT_A, TOOL) is None

    asyncio.run(scenario())


def test_a_switch_on_one_tool_does_not_trip_another(redis_url: str) -> None:
    """The switch is per tool; a blanket stop would take down unrelated work."""

    async def scenario() -> None:
        async with _client(redis_url) as client:
            switch = KillSwitch(client)
            await switch.trip(TENANT_A, TOOL, REASON, ACTOR)
            await switch.check(TENANT_A, OTHER_TOOL)  # must not raise

    asyncio.run(scenario())


# --- tenant scope -------------------------------------------------------------


def test_a_tenant_switch_trips_that_tenant_with_reason_and_actor(redis_url: str) -> None:
    """The error carries the admin's own words: the user is told why the tool is
    off, and the audit trail records who turned it off (the design)."""

    async def scenario() -> None:
        async with _client(redis_url) as client:
            await client.set(f"killswitch:{TENANT_A}:{TOOL}", _state_json())
            with pytest.raises(ToolDisabledError) as excinfo:
                await KillSwitch(client).check(TENANT_A, TOOL)
            assert excinfo.value.code == ErrorCode.TOOL_DISABLED
            message = str(excinfo.value)
            assert REASON in message
            assert ACTOR in message

    asyncio.run(scenario())


def test_a_tenant_switch_never_trips_another_tenant(redis_url: str) -> None:
    """a project rule in effect: one tenant's emergency stop must not leak into
    another tenant's traffic."""

    async def scenario() -> None:
        async with _client(redis_url) as client:
            switch = KillSwitch(client)
            await switch.trip(TENANT_A, TOOL, REASON, ACTOR)
            with pytest.raises(ToolDisabledError):
                await switch.check(TENANT_A, TOOL)
            await switch.check(TENANT_B, TOOL)  # must not raise

    asyncio.run(scenario())


# --- global scope and precedence ----------------------------------------------


def test_a_global_switch_trips_every_tenant(redis_url: str) -> None:
    """Two unrelated tenants, one platform-wide stop."""

    async def scenario() -> None:
        async with _client(redis_url) as client:
            switch = KillSwitch(client)
            await switch.trip(GLOBAL, TOOL, REASON, ACTOR)
            for tenant in (TENANT_A, TENANT_B):
                with pytest.raises(ToolDisabledError) as excinfo:
                    await switch.check(tenant, TOOL)
                assert REASON in str(excinfo.value)

    asyncio.run(scenario())


def test_the_global_switch_wins_over_a_tenant_switch(redis_url: str) -> None:
    """Decision D. The two reasons differ, so the message proves which key was
    read: a tenant switch must not be able to mask the platform stop."""

    async def scenario() -> None:
        async with _client(redis_url) as client:
            await client.set(f"killswitch:global:{TOOL}", _state_json(reason=REASON))
            await client.set(f"killswitch:{TENANT_A}:{TOOL}", _state_json(reason=OTHER_REASON))
            with pytest.raises(ToolDisabledError) as excinfo:
                await KillSwitch(client).check(TENANT_A, TOOL)
            message = str(excinfo.value)
            assert REASON in message
            assert OTHER_REASON not in message

    asyncio.run(scenario())


def test_status_reports_the_global_switch_over_the_tenant_one(redis_url: str) -> None:
    """`status` must answer the same question `check` answers, or an admin would
    clear a tenant switch and wonder why the tool is still off."""

    async def scenario() -> None:
        async with _client(redis_url) as client:
            await client.set(f"killswitch:global:{TOOL}", _state_json(reason=REASON))
            await client.set(f"killswitch:{TENANT_A}:{TOOL}", _state_json(reason=OTHER_REASON))
            state = await KillSwitch(client).status(TENANT_A, TOOL)
            assert state is not None
            assert state.reason == REASON

    asyncio.run(scenario())


def test_clearing_the_tenant_switch_leaves_the_global_one_tripped(redis_url: str) -> None:
    """Boundary between the two scopes: clearing one must not clear the other."""

    async def scenario() -> None:
        async with _client(redis_url) as client:
            switch = KillSwitch(client)
            await switch.trip(GLOBAL, TOOL, REASON, ACTOR)
            await switch.trip(TENANT_A, TOOL, OTHER_REASON, ACTOR)
            await switch.clear(TENANT_A, TOOL)
            with pytest.raises(ToolDisabledError) as excinfo:
                await switch.check(TENANT_A, TOOL)
            assert REASON in str(excinfo.value)

    asyncio.run(scenario())


# --- round trip ---------------------------------------------------------------


@pytest.mark.parametrize(
    "scope",
    [pytest.param(GLOBAL, id="global"), pytest.param(TENANT_A, id="tenant")],
)
def test_trip_status_clear_round_trip(redis_url: str, scope: Any) -> None:
    """An earlier change acceptance: after `trip` the state reads back with reason and
    actor; after `clear` the tool is allowed again."""

    async def scenario() -> None:
        async with _client(redis_url) as client:
            switch = KillSwitch(client)
            before = datetime.now(UTC)
            tripped = await switch.trip(scope, TOOL, REASON, ACTOR)
            assert isinstance(tripped, SwitchState)
            assert (tripped.reason, tripped.actor) == (REASON, ACTOR)

            state = await switch.status(TENANT_A, TOOL)
            assert state is not None
            assert (state.reason, state.actor) == (REASON, ACTOR)
            assert state.at >= before, "`at` records when the switch was tripped"

            with pytest.raises(ToolDisabledError):
                await switch.check(TENANT_A, TOOL)

            await switch.clear(scope, TOOL)
            assert await switch.status(TENANT_A, TOOL) is None
            await switch.check(TENANT_A, TOOL)  # must not raise

    asyncio.run(scenario())


def test_clearing_a_switch_that_was_never_tripped_is_harmless(redis_url: str) -> None:
    """Unhappy path an admin will hit: a double click on "re-enable" must not error."""

    async def scenario() -> None:
        async with _client(redis_url) as client:
            switch = KillSwitch(client)
            await switch.clear(TENANT_A, TOOL)  # must not raise
            await switch.check(TENANT_A, TOOL)  # must not raise

    asyncio.run(scenario())


def test_switch_state_is_frozen() -> None:
    """The provenance of a stop is evidence; nothing downstream may rewrite it."""
    state = SwitchState(reason=REASON, actor=ACTOR, at=datetime.now(UTC))
    with pytest.raises(ValidationError):
        state.reason = "rewritten"  # frozen model: assignment must be refused


# --- fail closed --------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "case"),
    [
        pytest.param("not json at all", "not-json", id="not-json"),
        pytest.param("[]", "wrong-type", id="json-but-not-an-object"),
        pytest.param(
            json.dumps({"actor": ACTOR, "at": "2026-09-04T10:00:00+00:00"}),
            "missing-reason",
            id="missing-reason",
        ),
        pytest.param(
            json.dumps({"reason": REASON, "at": "2026-09-04T10:00:00+00:00"}),
            "missing-actor",
            id="missing-actor",
        ),
        pytest.param(
            json.dumps({"reason": REASON, "actor": ACTOR}),
            "missing-at",
            id="missing-at",
        ),
        pytest.param(
            json.dumps(
                {
                    "reason": REASON,
                    "actor": ACTOR,
                    "at": "2026-09-04T10:00:00+00:00",
                    "scheduled_until": "2026-09-05T10:00:00+00:00",
                }
            ),
            "extra-field",
            id="extra-field",
        ),
    ],
)
@pytest.mark.parametrize(
    "key_template",
    [
        pytest.param("killswitch:global:{tool}", id="global-key"),
        pytest.param("killswitch:{tenant}:{tool}", id="tenant-key"),
    ],
)
def test_an_unreadable_switch_value_denies(
    redis_url: str, raw: str, case: str, key_template: str
) -> None:
    """Decision C, the boundary row: a value present but unparseable is NOT
    absent. Treating it as absent would let a corrupt write silently re-enable
    a tool an operator believes is stopped."""

    async def scenario() -> None:
        async with _client(redis_url) as client:
            await client.set(key_template.format(tenant=TENANT_A, tool=TOOL), raw)
            with pytest.raises(ToolDisabledError) as excinfo:
                await KillSwitch(client).check(TENANT_A, TOOL)
            assert excinfo.value.code == ErrorCode.TOOL_DISABLED
            assert "unreadable" in str(excinfo.value).lower(), (
                "the operator must be told the switch could not be read, "
                f"not given a parse error ({case})"
            )

    asyncio.run(scenario())


def test_an_unreadable_global_value_denies_even_with_a_valid_tenant_value(
    redis_url: str,
) -> None:
    """Precedence still holds when the winning key is corrupt: the readable
    tenant switch must not rescue the unreadable global one."""

    async def scenario() -> None:
        async with _client(redis_url) as client:
            await client.set(f"killswitch:global:{TOOL}", "not json at all")
            await client.set(f"killswitch:{TENANT_A}:{TOOL}", _state_json(reason=OTHER_REASON))
            with pytest.raises(ToolDisabledError) as excinfo:
                await KillSwitch(client).check(TENANT_A, TOOL)
            assert "unreadable" in str(excinfo.value).lower()

    asyncio.run(scenario())


def test_an_unreachable_redis_denies_with_internal() -> None:
    """Decision C: a switch that cannot be read is not a switch. Failing open
    here would mean an outage quietly re-enables every stopped tool.

    No `redis_url` fixture: this client points at a port nothing listens on.
    """
    port = _unused_port()

    async def scenario() -> None:
        async with _client(f"redis://127.0.0.1:{port}/0") as client:
            with pytest.raises(ToolError) as excinfo:
                await KillSwitch(client).check(TENANT_A, TOOL)
            assert excinfo.value.code == ErrorCode.INTERNAL

    asyncio.run(scenario())


def test_an_unreachable_redis_denies_status_paths_too() -> None:
    """`status` shares the read path, so it must not report "not tripped" while
    Redis is down; an admin would read that as safe."""
    port = _unused_port()

    async def scenario() -> None:
        async with _client(f"redis://127.0.0.1:{port}/0") as client:
            with pytest.raises(ToolError) as excinfo:
                await KillSwitch(client).status(TENANT_A, TOOL)
            assert excinfo.value.code == ErrorCode.INTERNAL

    asyncio.run(scenario())


def test_an_unreachable_redis_fails_trip_rather_than_reporting_success() -> None:
    """The write path fails closed the other way round: an admin who is told
    the tool is stopped, while nothing was written, is the worst outcome here."""
    port = _unused_port()

    async def scenario() -> None:
        async with _client(f"redis://127.0.0.1:{port}/0") as client:
            with pytest.raises(ToolError) as excinfo:
                await KillSwitch(client).trip(GLOBAL, TOOL, REASON, ACTOR)
            assert excinfo.value.code == ErrorCode.INTERNAL

    asyncio.run(scenario())


def test_an_unreachable_redis_fails_clear_rather_than_reporting_success() -> None:
    """Same for re-enabling: silently doing nothing would leave the tool off
    while the admin surface showed it back on."""
    port = _unused_port()

    async def scenario() -> None:
        async with _client(f"redis://127.0.0.1:{port}/0") as client:
            with pytest.raises(ToolError) as excinfo:
                await KillSwitch(client).clear(GLOBAL, TOOL)
            assert excinfo.value.code == ErrorCode.INTERNAL

    asyncio.run(scenario())


# --- validation of everything that shapes a key or a stored value -------------
# Reviewer round 1: the module claimed in prose that a string could not become
# the global scope and that `tool` was not user input. Neither was enforced.


@pytest.mark.parametrize(
    "scope",
    [
        pytest.param("global", id="the-literal-string-global"),
        pytest.param(str(TENANT_A), id="a-tenant-id-as-a-string"),
        pytest.param(None, id="none"),
        pytest.param(0, id="an-int"),
    ],
)
def test_a_scope_that_is_not_a_uuid_or_the_sentinel_is_refused(redis_url: str, scope: Any) -> None:
    """A scope arriving through an `Any`-typed seam (a request body, a tool
    argument map) must not be able to trip the platform-wide switch: mypy does
    not run at the MCP boundary, so the check has to exist at runtime."""

    async def scenario() -> None:
        async with _client(redis_url) as client:
            with pytest.raises(InvalidSwitchTargetError) as excinfo:
                await KillSwitch(client).trip(scope, TOOL, REASON, ACTOR)
            assert excinfo.value.code == ErrorCode.VALIDATION
            assert await client.keys("killswitch:*") == [], "nothing may be written"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "tool",
    [
        pytest.param("revoke-access", id="hyphen-instead-of-underscore"),
        pytest.param("revoke_access ", id="trailing-space"),
        pytest.param("Revoke_Access", id="uppercase"),
        pytest.param("killswitch:global:x", id="colons-straddling-the-separators"),
        pytest.param("", id="empty"),
        pytest.param("*", id="a-redis-glob"),
        pytest.param("9lives", id="leading-digit"),
        # `$` in Python matches before a trailing newline, so an anchored
        # `.match` would let this through and write a key nothing reads.
        pytest.param("revoke_access\n", id="trailing-newline"),
        pytest.param("revoke_access\r\n", id="trailing-crlf"),
    ],
)
def test_a_malformed_tool_name_is_refused(redis_url: str, tool: str) -> None:
    """A name `check` will never be called with writes a key nothing reads: the
    admin surface shows the tool stopped while every call still goes through."""

    async def scenario() -> None:
        async with _client(redis_url) as client:
            with pytest.raises(InvalidSwitchTargetError) as excinfo:
                await KillSwitch(client).trip(GLOBAL, tool, REASON, ACTOR)
            assert excinfo.value.code == ErrorCode.VALIDATION
            assert await client.keys("killswitch:*") == [], "nothing may be written"

    asyncio.run(scenario())


def test_a_tool_name_of_exactly_the_maximum_length_is_accepted(redis_url: str) -> None:
    """The inclusive boundary, so widening or narrowing the cap fails here."""
    tool = "t" * TOOL_NAME_MAX_LENGTH

    async def scenario() -> None:
        async with _client(redis_url) as client:
            await KillSwitch(client).trip(GLOBAL, tool, REASON, ACTOR)
            assert await client.get(f"killswitch:global:{tool}") is not None

    asyncio.run(scenario())


def test_a_tool_name_one_character_over_the_maximum_is_refused(redis_url: str) -> None:
    """The other side of the same boundary."""
    tool = "t" * (TOOL_NAME_MAX_LENGTH + 1)

    async def scenario() -> None:
        async with _client(redis_url) as client:
            with pytest.raises(InvalidSwitchTargetError):
                await KillSwitch(client).trip(GLOBAL, tool, REASON, ACTOR)

    asyncio.run(scenario())


def test_the_tool_name_cap_is_exactly_64() -> None:
    """Pinned as a value, not a band: a docstring is not a record."""
    assert TOOL_NAME_MAX_LENGTH == 64


def test_the_reason_and_actor_caps_are_exactly_500_and_320() -> None:
    """320 is the maximum length of an email address; the reason is read on
    every tool call, so it stays small."""
    assert REASON_MAX_LENGTH == 500
    assert ACTOR_MAX_LENGTH == 320


@pytest.mark.parametrize(
    ("reason", "actor", "case"),
    [
        pytest.param("r" * (REASON_MAX_LENGTH + 1), ACTOR, "reason", id="reason-too-long"),
        pytest.param(REASON, "a" * (ACTOR_MAX_LENGTH + 1), "actor", id="actor-too-long"),
    ],
)
def test_an_oversized_reason_or_actor_is_refused_before_the_write(
    redis_url: str, reason: str, actor: str, case: str
) -> None:
    """`check` reads this value before every tool call and copies it into an
    exception message, so an unbounded reason is an amplifier on the hottest
    path in the system."""

    async def scenario() -> None:
        async with _client(redis_url) as client:
            with pytest.raises(InvalidSwitchTargetError) as excinfo:
                await KillSwitch(client).trip(GLOBAL, TOOL, reason, actor)
            assert excinfo.value.code == ErrorCode.VALIDATION
            assert await client.keys("killswitch:*") == [], f"nothing may be written ({case})"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("reason", "actor"),
    [
        pytest.param("r" * REASON_MAX_LENGTH, ACTOR, id="reason-at-the-cap"),
        pytest.param(REASON, "a" * ACTOR_MAX_LENGTH, id="actor-at-the-cap"),
    ],
)
def test_a_reason_or_actor_of_exactly_the_maximum_length_is_accepted(
    redis_url: str, reason: str, actor: str
) -> None:
    """Inclusive boundaries on both caps."""

    async def scenario() -> None:
        async with _client(redis_url) as client:
            switch = KillSwitch(client)
            await switch.trip(GLOBAL, TOOL, reason, actor)
            state = await switch.status(TENANT_A, TOOL)
            assert state is not None
            assert (state.reason, state.actor) == (reason, actor)

    asyncio.run(scenario())


def test_an_oversized_stored_reason_is_unreadable_rather_than_read(redis_url: str) -> None:
    """The cap holds on the way out too: a value written past this module (by
    hand, or by an older version) must deny rather than be honoured at any size."""

    async def scenario() -> None:
        async with _client(redis_url) as client:
            oversized = json.dumps(
                {
                    "reason": "r" * (REASON_MAX_LENGTH + 1),
                    "actor": ACTOR,
                    "at": "2026-09-04T10:00:00+00:00",
                }
            )
            await client.set(f"killswitch:global:{TOOL}", oversized)
            with pytest.raises(ToolDisabledError) as excinfo:
                await KillSwitch(client).check(TENANT_A, TOOL)
            assert "unreadable" in str(excinfo.value).lower()

    asyncio.run(scenario())


def test_the_unreadable_message_does_not_leak_the_raw_key(redis_url: str) -> None:
    """Row 10 surfaces this exception type's message to the caller (it is how
    the admin's reason reaches the user), so the corrupt-value variant must not
    hand back the tenant id and the internal key shape."""

    async def scenario() -> None:
        async with _client(redis_url) as client:
            await client.set(f"killswitch:{TENANT_A}:{TOOL}", "not json at all")
            with pytest.raises(ToolDisabledError) as excinfo:
                await KillSwitch(client).check(TENANT_A, TOOL)
            message = str(excinfo.value)
            assert str(TENANT_A) not in message
            assert "killswitch:" not in message

    asyncio.run(scenario())


def test_every_rbac_tool_name_is_a_valid_switch_target(redis_url: str) -> None:
    """`switch.py` does not import the matrix, so the two could drift: a tool
    the matrix names in a shape this pattern refuses would be the one tool with
    no emergency stop, and it would be the newest, least-proven one."""

    async def scenario() -> None:
        async with _client(redis_url) as client:
            switch = KillSwitch(client)
            for tool in Tool:
                await switch.trip(GLOBAL, tool.value, REASON, ACTOR)
                await switch.clear(GLOBAL, tool.value)

    asyncio.run(scenario())


def test_an_unreachable_redis_message_does_not_leak_the_key_or_the_endpoint() -> None:
    """Same reason as the corrupt-value message: row 10 maps INTERNAL and may
    surface the text, and this one would otherwise carry the tenant id, the key
    shape and the Redis host and port."""
    port = _unused_port()

    async def scenario() -> None:
        async with _client(f"redis://127.0.0.1:{port}/0") as client:
            switch = KillSwitch(client)
            for call in (
                switch.check(TENANT_A, TOOL),
                switch.status(TENANT_A, TOOL),
                switch.trip(GLOBAL, TOOL, REASON, ACTOR),
                switch.clear(GLOBAL, TOOL),
            ):
                with pytest.raises(ToolError) as excinfo:
                    await call
                message = str(excinfo.value)
                assert str(TENANT_A) not in message
                assert "killswitch:" not in message
                assert str(port) not in message

    asyncio.run(scenario())
