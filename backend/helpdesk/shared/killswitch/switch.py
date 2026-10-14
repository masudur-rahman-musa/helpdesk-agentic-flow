"""Per-tool kill switches, checked before every tool call (the design).

Two scopes, two key shapes, both named by the design:
`killswitch:global:{tool}` is the platform's emergency stop and
`killswitch:{tenant}:{tool}` stops one tenant. `check` reads global first
and the first hit wins, so a tenant switch can never mask the platform one.

The whole module fails closed (decision C). A switch that cannot be read is
not a switch: an unparseable value denies with `TOOL_DISABLED` saying the
switch is unreadable, and an unreachable Redis denies with `INTERNAL`.
Neither is treated as "no switch set", because an operator who trips a
switch must be able to rely on it — the price is that Redis is a hard
dependency of every tool call, recorded as a known issue in STATUS.

Every input that shapes a key or a stored value is checked at runtime, not
only by the type checker. The scope must be a `uuid.UUID` or the `GLOBAL`
sentinel — the string "global" is refused, so a scope deserialised from a
request body at an `Any`-typed seam cannot be passed off as the
platform-wide stop. The tool name must match
`TOOL_NAME_PATTERN` within `TOOL_NAME_MAX_LENGTH`, because a name that
differs from the one `check` is called with (a hyphen, a trailing space)
writes a key nothing reads: the admin surface would show the tool stopped
while every call went through. `reason` and `actor` are length-capped by
`SwitchState` itself, so the cap holds on the way in and on the way out —
`check` runs before every tool call, and an unbounded reason would be read
and copied into an exception message on that path.

`tool` is typed `str` rather than `helpdesk.auth.rbac.Tool` so this package
stays importable without the matrix; the pattern is what enforces the shape.

I/O only. Nothing here decides policy; row 10 calls `check` before each
tool, row 24 builds the admin toggle over `trip`/`clear`/`status`.
"""

import enum
import re
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from helpdesk.shared.errors import ErrorCode, ToolDisabledError, ToolError

TOOL_NAME_MAX_LENGTH = 64
"""Longest accepted tool name. Redis imposes no limit, so this is the only
backstop against a name that no `check` call will ever match."""

TOOL_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]*")
"""The shape every tool in the RBAC matrix has, pinned by
`test_every_rbac_tool_name_is_a_valid_switch_target`. A colon would also let a
name straddle the key's own separators.

Matched with `fullmatch`, never `match`: `$` in Python also matches before a
trailing newline, so an anchored `match` would accept `"revoke_access\n"` —
the exact key-shape drift this pattern exists to stop."""

REASON_MAX_LENGTH = 500
"""Longest accepted reason. Read on every tool call, so it stays small."""

ACTOR_MAX_LENGTH = 320
"""Longest accepted actor. 320 is the maximum length of an email address."""

KEY_PREFIX = "killswitch"
"""The literal prefix from the design. Shared by both key shapes."""

GLOBAL_SEGMENT = "global"
"""What stands where a tenant id would, for the platform-wide switch."""


class _GlobalScope(enum.Enum):
    """A one-member enum so `GLOBAL` has a type a signature can name."""

    GLOBAL = GLOBAL_SEGMENT


GLOBAL = _GlobalScope.GLOBAL
"""The platform-wide scope. Not the string "global": a scope is a tenant id
or this sentinel, so no string from a request can be passed off as global."""

Scope = uuid.UUID | _GlobalScope


class InvalidSwitchTargetError(ToolError):
    """The scope, tool name, reason or actor was not something this can store.

    Raised before any write, so a malformed admin action never half-lands:
    the alternative is a key written that `check` will never read, which
    presents as a stopped tool that is quietly still running.
    """

    code = ErrorCode.VALIDATION


class SwitchUnavailableError(ToolError):
    """Redis could not be reached, so the switch state is unknown.

    `INTERNAL` rather than `TOOL_DISABLED`: the tool is not known to be off,
    the platform is not able to say. The call is still denied (decision C).
    """

    code = ErrorCode.INTERNAL


class SwitchState(BaseModel):
    """Why a tool is off, who turned it off, and when.

    Frozen and `extra="forbid"`: the provenance of a stop is audit evidence,
    and a value carrying unexpected fields is a value written by something
    other than this module, which is a reason to deny rather than to guess.
    Both pinned by `test_switch_state_is_frozen` and the `extra-field` row of
    `test_an_unreadable_switch_value_denies`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str = Field(max_length=REASON_MAX_LENGTH)
    actor: str = Field(max_length=ACTOR_MAX_LENGTH)
    at: datetime


class ToolSwitchStates(BaseModel):
    """Both layers of one tool's switch, as stored, for the admin board.

    Both are carried rather than one effective state:
    an admin clearing a tenant switch has to be told the global one is still
    holding the tool down, which is the same reason `status`' docstring
    gives. Pinned by
    `test_killswitch.py::test_list_states_reports_each_layer_on_its_own`,
    whose `both-layers` case fails against a single effective state.

    `unreadable` is a fact about what is STORED, not a decision about what to
    do with it: a value that will not parse marks its own row and leaves both
    states null, so one bad key cannot blank the board an admin needs in
    order to clear it (decision D). Pinned by
    `::test_an_unparseable_value_marks_its_own_row_and_answers_the_others`.
    `check` keeps its own behaviour: an unreadable value still denies there.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    global_state: SwitchState | None
    tenant_state: SwitchState | None
    unreadable: bool


def key_for(scope: Scope, tool: str) -> str:
    """The exact Redis key for a scope and tool (the design).

    Both arguments are checked here rather than at each call site, so every
    path into Redis goes through the same gate.
    """
    if isinstance(scope, _GlobalScope):
        segment = GLOBAL_SEGMENT
    elif isinstance(scope, uuid.UUID):
        segment = str(scope)
    else:
        # Not a type error at runtime: an `Any`-typed seam (a request body, a
        # tool argument map) is exactly where the string "global" would arrive.
        raise InvalidSwitchTargetError(
            f"scope must be a tenant UUID or the GLOBAL sentinel, got {type(scope).__name__}"
        )
    if len(tool) > TOOL_NAME_MAX_LENGTH or not TOOL_NAME_PATTERN.fullmatch(tool):
        raise InvalidSwitchTargetError(
            f"tool name must match {TOOL_NAME_PATTERN.pattern} "
            f"and be at most {TOOL_NAME_MAX_LENGTH} characters"
        )
    return f"{KEY_PREFIX}:{segment}:{tool}"


def _parse(raw: bytes | str | None) -> tuple[SwitchState | None, bool]:
    """One stored value as (state, readable).

    Absent is `(None, True)` and unparseable is `(None, False)`, which is the
    distinction `list_states` needs and `_read` deliberately does not make:
    `check` denies on an unreadable value, while the BOARD reports it, because
    the board is the only surface from which a bad key can be seen and cleared. No exception either way, so one bad key cannot
    hide the rest of the board
    (`test_killswitch.py::test_an_unparseable_value_marks_its_own_row_and_answers_the_others`).
    """
    if raw is None:
        return None, True
    try:
        return SwitchState.model_validate_json(raw), True
    except ValidationError:
        return None, False


class KillSwitch:
    """The read and write side of the switches, over one Redis client.

    The client is injected rather than built here so a caller owns its
    lifecycle (and tests can point one at a dedicated database index).
    """

    def __init__(self, client: Redis) -> None:
        self._client = client

    async def check(self, tenant_id: uuid.UUID, tool: str) -> None:
        """Return if the tool may run; raise otherwise. Called before every tool."""
        state, scope = await self._read_effective(tenant_id, tool)
        if state is None:
            return
        where = "platform-wide" if isinstance(scope, _GlobalScope) else "for this tenant"
        raise ToolDisabledError(
            f"{tool} is disabled {where}: {state.reason} (tripped by {state.actor})"
        )

    async def status(self, tenant_id: uuid.UUID, tool: str) -> SwitchState | None:
        """The state `check` would act on, or None when the tool is allowed.

        Same read path as `check` on purpose: an admin clearing a tenant
        switch must be told the global one is still holding the tool down.
        """
        state, _ = await self._read_effective(tenant_id, tool)
        return state

    async def list_states(
        self, tenant_id: uuid.UUID, tools: Sequence[str]
    ) -> list[ToolSwitchStates]:
        """Both layers of every named tool, in the order asked, in ONE round trip.

        The board behind row 24's `/admin/killswitch` page. One row per
        REQUESTED tool, never one per key that happens to exist: a board built
        from what Redis returned could never say "this tool is running", which
        is the sentence the page exists for
        (`test_killswitch.py::test_list_states_answers_one_null_row_per_tool_over_a_clean_namespace`).

        One `MGET` over the two keys per tool, not a `GET` per key (an earlier change
        decision C): fifty-two round trips on every render of the board is a
        latency budget spent on nothing. Counted on the client by
        `::test_the_whole_board_costs_exactly_one_redis_command`, because an
        implementation looping `_read` answers exactly the same rows.

        Keys are built by `key_for`, so this path passes the same gate as
        every other, and the tenant layer is keyed on the ARGUMENT alone —
        never a glob — so another tenant's switch is invisible here
        (`::test_list_states_never_reports_another_tenants_switch`). Values
        are matched to tools by position, two per tool, global first; the
        off-by-one that would attribute a stop to the neighbouring tool is
        caught by `::test_a_switch_in_the_middle_leaves_every_other_row_clear`.

        A value present but unparseable marks its row `unreadable` instead of
        raising (decision D); a `RedisError` still raises
        `SwitchUnavailableError`, because an outage is not a board of running
        tools (`::test_list_states_on_an_unreachable_redis_raises_internal`).
        Neither message names the key, the tenant or the endpoint.
        """
        names = list(tools)
        if not names:
            # `MGET` with no keys is an error, and an empty board needs no read.
            return []
        keys = [key for tool in names for key in (key_for(GLOBAL, tool), key_for(tenant_id, tool))]
        try:
            values: list[bytes | str | None] = await self._client.mget(keys)
        except RedisError as exc:
            # Neither the keys (which hold the tenant id) nor the endpoint:
            # row 24 surfaces this message to whoever called the tool.
            raise SwitchUnavailableError("could not read the kill switch board") from exc
        rows: list[ToolSwitchStates] = []
        for index, tool in enumerate(names):
            global_state, global_ok = _parse(values[2 * index])
            tenant_state, tenant_ok = _parse(values[2 * index + 1])
            unreadable = not (global_ok and tenant_ok)
            rows.append(
                ToolSwitchStates(
                    tool=tool,
                    # An unreadable row carries no state at all: what is there
                    # could not be read, and reporting the other layer beside
                    # the marker would invite the page to render half a story.
                    global_state=None if unreadable else global_state,
                    tenant_state=None if unreadable else tenant_state,
                    unreadable=unreadable,
                )
            )
        return rows

    async def trip(self, scope: Scope, tool: str, reason: str, actor: str) -> SwitchState:
        """Turn a tool off for a scope, recording the admin's reason and identity."""
        key = key_for(scope, tool)
        try:
            state = SwitchState(reason=reason, actor=actor, at=datetime.now(UTC))
        except ValidationError as exc:
            raise InvalidSwitchTargetError(
                f"reason (max {REASON_MAX_LENGTH}) or actor (max {ACTOR_MAX_LENGTH}) "
                f"is too long to store: {exc.error_count()} validation errors"
            ) from exc
        try:
            await self._client.set(key, state.model_dump_json())
        except RedisError as exc:
            # The key holds the tenant id and the exception text holds the
            # Redis endpoint; row 10 may surface this message, so it names the
            # tool only. The `from exc` chain keeps the detail for logs.
            raise SwitchUnavailableError(f"could not trip the switch for {tool}") from exc
        return state

    async def clear(self, scope: Scope, tool: str) -> None:
        """Turn a tool back on for a scope. A switch that was never set is a no-op."""
        try:
            await self._client.delete(key_for(scope, tool))
        except RedisError as exc:
            raise SwitchUnavailableError(f"could not clear the switch for {tool}") from exc

    async def _read_effective(
        self, tenant_id: uuid.UUID, tool: str
    ) -> tuple[SwitchState | None, Scope | None]:
        """Global first, then the tenant; the first switch found wins."""
        for scope in (GLOBAL, tenant_id):
            state = await self._read(scope, tool)
            if state is not None:
                return state, scope
        return None, None

    async def _read(self, scope: Scope, tool: str) -> SwitchState | None:
        key = key_for(scope, tool)
        try:
            # `decode_responses` is the caller's choice, so the value comes
            # back as bytes or str; `model_validate_json` takes either.
            raw: bytes | str | None = await self._client.get(key)
        except RedisError as exc:
            raise SwitchUnavailableError(f"could not read the switch for {tool}") from exc
        if raw is None:
            return None
        try:
            return SwitchState.model_validate_json(raw)
        except ValidationError as exc:
            # Present but unparseable is not absent: something wrote a value
            # here, and guessing it meant "allowed" would silently re-enable
            # a tool an operator believes is stopped.
            # The key holds the tenant id, and row 10 surfaces this exception
            # type's message to the caller, so the message names the scope in
            # words; the key itself belongs in a log line, not in a response.
            where = "platform-wide" if isinstance(scope, _GlobalScope) else "for this tenant"
            raise ToolDisabledError(
                f"{tool} is disabled: the switch set {where} is unreadable, "
                f"so the call is denied ({exc.error_count()} validation errors)"
            ) from exc
