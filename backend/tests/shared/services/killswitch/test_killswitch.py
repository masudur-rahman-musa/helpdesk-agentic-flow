"""Per-tool kill switches, written before they exist.

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
      first, then ``killswitch:{tenant_id}:{tool}``; first hit wins (a global switch is the
      platform's emergency stop and must not
      be overridable per tenant).
    * ``await trip(scope, tool, reason, actor) -> SwitchState``
    * ``await clear(scope, tool) -> None``
    * ``await status(tenant_id, tool) -> SwitchState | None``

Fail closed: an unreadable value denies with ``TOOL_DISABLED`` and
a message saying the switch is unreadable; an unreachable Redis denies with
``INTERNAL``. A switch that cannot be read is never treated as absent.

The BULK read:

``await list_states(tenant_id, tools) -> Sequence[row]``
    One row per requested tool, in the order the tools were requested, read in
    ONE Redis round trip (an ``MGET`` over the two keys per tool).
    Each row carries ``tool``, ``global_state``, ``tenant_state`` (each a
    ``SwitchState`` or ``None``) and ``unreadable``.

    A value present but unparseable answers that row's ``unreadable`` marker
    with a null state instead of raising, so one bad key cannot
    blank the whole board; a ``RedisError`` still raises
    ``SwitchUnavailableError``. The sequence and the row attribute names are
    fixed here because the fields are named and the
    container is not; a mapping would fail the ordering case.

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
from pydantic import ValidationError
from redis.asyncio import Redis

from helpdesk.shared.auth.rbac import Tool
from helpdesk.shared.core.errors import ErrorCode, ToolDisabledError, ToolError
from helpdesk.shared.services.killswitch import (
    ACTOR_MAX_LENGTH,
    GLOBAL,
    REASON_MAX_LENGTH,
    TOOL_NAME_MAX_LENGTH,
    InvalidSwitchTargetError,
    KillSwitch,
    SwitchState,
)

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
# The design names these exact keys. Drift would silently disable nothing: the
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
    inspecting Redis, or the admin toggle, will expect."""

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
    off, and the audit trail records who turned it off."""

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
    """One tenant's emergency stop must not leak into
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
    """The global switch is checked first. The two reasons differ, so the message proves
    which key was read: a tenant switch must not be able to mask the platform stop."""

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
    """After `trip` the state reads back with reason and
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
    """A value present but unparseable is NOT
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
    """A switch that cannot be read is not a switch. Failing open
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
    """The caller sees this exception type's message to the caller (it is how
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
    """Same reason as the corrupt-value message: the caller maps INTERNAL and may
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


# --- the bulk read --------------------------------------
#
# `check` and `status` answer for ONE tool at a time. The admin board at
# `/admin/killswitch` needs every tool at once, and the rows below pin what it
# is allowed to be told: the two layers separately, another tenant's switch
# nowhere, a bad key confined to its own row, and one round trip for the lot.

# Five valid switch targets, so the middle one is genuinely in the middle: the
# `MGET` matches values to tools by POSITION, and an off-by-one attributes one
# tool's stop to its neighbour.
LIST_TOOLS: tuple[str, ...] = (
    "get_me",
    "list_my_access",
    "revoke_access",
    "search_audit",
    "create_ticket",
)
MIDDLE_TOOL = LIST_TOOLS[2]

# Every command name issued through `_CountingRedis`, in order. Module level
# rather than an instance attribute so the counter is readable off a client
# typed as the library's own `Redis`.
_COMMANDS: list[str] = []


class _CountingRedis(Redis):
    """Records the NAME of every command issued through this client.

    Counting on the client is the only honest way to assert "one round trip":
    reading the source for an `mget` call would pass just as
    well for an implementation that also issued a `GET` per key first.
    """

    async def execute_command(self, *args: Any, **options: Any) -> Any:
        _COMMANDS.append(str(args[0]))
        # redis-py leaves this method unannotated, so calling it from a typed
        # override is `no-untyped-call` under `mypy --strict`.
        return await super().execute_command(*args, **options)  # type: ignore[no-untyped-call]


def _row_by_tool(rows: Any, tool: str) -> Any:
    """The one row for a tool, asserting there is exactly one."""
    matches = [row for row in rows if row.tool == tool]
    assert len(matches) == 1, f"expected exactly one row for {tool}, got {len(matches)}"
    return matches[0]


def test_list_states_answers_one_null_row_per_tool_over_a_clean_namespace(
    redis_url: str,
) -> None:
    """Catches a bulk read that answers only the tools with a key set.

    Redis returns nothing at all for a key that does not exist, so an
    implementation building its rows from what came BACK (a `SCAN`, or a
    filter over the non-null `MGET` values) answers a shorter list here, in
    which every tool is stopped. The admin board would then have no way to say
    "this tool is running" — the sentence the page exists for.
    The order is asserted too: the rows must follow the tools as requested.
    """

    async def scenario() -> None:
        async with _client(redis_url) as client:
            rows = await KillSwitch(client).list_states(TENANT_A, LIST_TOOLS)

            assert [row.tool for row in rows] == list(LIST_TOOLS)
            for row in rows:
                assert row.global_state is None, row.tool
                assert row.tenant_state is None, row.tool
                assert row.unreadable is False, row.tool

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("set_global", "set_tenant"),
    [
        pytest.param(True, False, id="global-only"),
        pytest.param(False, True, id="tenant-only"),
        pytest.param(True, True, id="both-layers"),
    ],
)
def test_list_states_reports_each_layer_on_its_own(
    redis_url: str, set_global: bool, set_tenant: bool
) -> None:
    """Catches a bulk read that collapses the two layers into one effective
    state, the way `status` does.

    The `both-layers` row is the one that separates them: with one effective
    state the tenant switch is invisible behind the global one, so an admin
    clearing the tenant switch is never told the global one still holds the
    tool down (see `KillSwitch.status`' own docstring). The two
    reasons differ, so a row reading the wrong KEY into the wrong layer fails
    as well as one reading a single key.
    """

    async def scenario() -> None:
        async with _client(redis_url) as client:
            if set_global:
                await client.set(f"killswitch:global:{MIDDLE_TOOL}", _state_json(reason=REASON))
            if set_tenant:
                await client.set(
                    f"killswitch:{TENANT_A}:{MIDDLE_TOOL}", _state_json(reason=OTHER_REASON)
                )

            rows = await KillSwitch(client).list_states(TENANT_A, LIST_TOOLS)
            row = _row_by_tool(rows, MIDDLE_TOOL)

            if set_global:
                assert row.global_state is not None
                assert row.global_state.reason == REASON
                assert row.global_state.actor == ACTOR
            else:
                assert row.global_state is None
            if set_tenant:
                assert row.tenant_state is not None
                assert row.tenant_state.reason == OTHER_REASON
            else:
                assert row.tenant_state is None
            assert row.unreadable is False

    asyncio.run(scenario())


def test_list_states_never_reports_another_tenants_switch(redis_url: str) -> None:
    """Catches a bulk read that keys the tenant layer on anything but the
    tenant it was asked about.

    Only tenant B has a switch here, so every row of tenant A's board must be
    clear. An implementation reading `killswitch:*:{tool}` with a glob, or
    keying on the tenant that happens to be stored in the value, would show
    tenant A a stop nobody in tenant A set and the admin could not clear it.
    """

    async def scenario() -> None:
        async with _client(redis_url) as client:
            await client.set(f"killswitch:{TENANT_B}:{MIDDLE_TOOL}", _state_json())

            rows = await KillSwitch(client).list_states(TENANT_A, LIST_TOOLS)

            assert [row.tool for row in rows] == list(LIST_TOOLS)
            for row in rows:
                assert row.global_state is None, row.tool
                assert row.tenant_state is None, row.tool
                assert row.unreadable is False, row.tool

    asyncio.run(scenario())


def test_a_switch_in_the_middle_leaves_every_other_row_clear(redis_url: str) -> None:
    """Catches the `MGET` off-by-one.

    A bulk read matches values to tools by position, and the two keys per tool
    interleave, so a row built from the wrong index shows the stop on the tool
    NEXT to the one an operator actually stopped — a board that is wrong in the
    most confusing possible way, with every single-tool test still green. The
    switch is planted in the middle of five so a shift in either direction, by
    one place or by one LAYER, lands on a row asserted clear.
    """

    async def scenario() -> None:
        async with _client(redis_url) as client:
            await client.set(f"killswitch:{TENANT_A}:{MIDDLE_TOOL}", _state_json())

            rows = await KillSwitch(client).list_states(TENANT_A, LIST_TOOLS)

            stopped = _row_by_tool(rows, MIDDLE_TOOL)
            assert stopped.tenant_state is not None
            assert stopped.tenant_state.reason == REASON
            assert stopped.global_state is None
            for row in rows:
                if row.tool == MIDDLE_TOOL:
                    continue
                assert row.global_state is None, row.tool
                assert row.tenant_state is None, row.tool
                assert row.unreadable is False, row.tool

    asyncio.run(scenario())


def test_an_unparseable_value_marks_its_own_row_and_answers_the_others(
    redis_url: str,
) -> None:
    """Catches an unparseable value raising as `check` does.

    `_read` raises `ToolDisabledError` on a value it cannot parse, and reusing
    that path here blanks the whole board behind one bad key — the board being
    the only surface from which that key can be seen and cleared. It equally
    catches the opposite error, a parse failure swallowed as "no switch": the
    marker and a null state are asserted TOGETHER, so a row that reads clear
    where `check` is denying every call fails.

    A second tool carries a good value in the same read, so the case pins that
    the rest of the list still answers rather than just that nothing raised.
    """

    async def scenario() -> None:
        async with _client(redis_url) as client:
            await client.set(f"killswitch:global:{MIDDLE_TOOL}", "not json at all")
            await client.set(f"killswitch:{TENANT_A}:{LIST_TOOLS[0]}", _state_json())

            rows = await KillSwitch(client).list_states(TENANT_A, LIST_TOOLS)

            bad = _row_by_tool(rows, MIDDLE_TOOL)
            assert bad.unreadable is True
            assert bad.global_state is None
            assert bad.tenant_state is None

            good = _row_by_tool(rows, LIST_TOOLS[0])
            assert good.unreadable is False
            assert good.tenant_state is not None
            assert good.tenant_state.reason == REASON
            assert good.global_state is None

    asyncio.run(scenario())


def test_list_states_on_an_unreachable_redis_raises_internal(redis_url: str) -> None:
    """Catches an outage answered as a board of running tools.

    An unparseable VALUE is a row-level marker; a `RedisError`
    is not that, and an implementation that widened the marker to cover every
    failure would paint the whole board green during an outage while `check`
    denied every call. `INTERNAL` and not `TOOL_DISABLED`: the platform is not
    able to say what is stopped.

    The message is asserted to name neither the host and port nor the key, for
    the reason the other read paths are: the tenant id and the Redis endpoint
    would otherwise travel to whoever called the tool.

    No `redis_url` use beyond the fixture's namespace guarantee: this client
    points at a port nothing listens on.
    """
    port = _unused_port()

    async def scenario() -> None:
        async with _client(f"redis://127.0.0.1:{port}/0") as client:
            with pytest.raises(ToolError) as excinfo:
                await KillSwitch(client).list_states(TENANT_A, LIST_TOOLS)
            assert excinfo.value.code == ErrorCode.INTERNAL
            message = str(excinfo.value)
            assert str(TENANT_A) not in message
            assert "killswitch:" not in message
            assert str(port) not in message

    asyncio.run(scenario())


def test_the_whole_board_costs_exactly_one_redis_command(redis_url: str) -> None:
    """Catches a bulk read that is a loop of `GET`s.

    One `MGET` over the two keys per tool, or fifty-two round trips to paint
    one page, on every render of the admin board. Counted on the CLIENT rather
    than read off the source: an implementation calling `_read` per tool
    produces exactly the same rows, and only the command count tells them
    apart. The command NAME is asserted as well, so a single `KEYS` or `SCAN`
    over the keyspace — one command, and unbounded — fails here too.

    The seeding writes go through the same client and are excluded by clearing
    the recorder immediately before the read, with the connection already
    open, so no connection handshake is counted.
    """

    async def scenario() -> None:
        client = _CountingRedis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
        try:
            await client.set(f"killswitch:{TENANT_A}:{MIDDLE_TOOL}", _state_json())
            _COMMANDS.clear()

            rows = await KillSwitch(client).list_states(TENANT_A, LIST_TOOLS)

            assert [row.tool for row in rows] == list(LIST_TOOLS)
            assert [name.upper() for name in _COMMANDS] == ["MGET"], _COMMANDS
        finally:
            _COMMANDS.clear()
            await client.aclose()

    asyncio.run(scenario())
