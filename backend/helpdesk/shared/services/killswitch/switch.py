"""Per-tool kill switches in Redis, checked before every tool call.

`killswitch:global:{tool}` stops a tool for everyone and
`killswitch:{tenant}:{tool}` stops it for one tenant. Global is read first,
so a tenant switch can never hide the platform one.

It fails closed. An unreadable value denies with `TOOL_DISABLED` and an
unreachable Redis denies with `INTERNAL`, because an operator must be able to
trust a switch they set. The cost is that Redis is needed for every call.

Scope and tool name are checked at runtime too. A tool name that differs from
the one `check` uses would write a key nothing reads, so the admin page would
show the tool stopped while calls still went through.
"""

import enum
import re
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from helpdesk.shared.core.errors import ErrorCode, ToolDisabledError, ToolError

TOOL_NAME_MAX_LENGTH = 64
"""Longest accepted tool name. Redis sets no limit of its own."""

TOOL_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]*")
"""The shape every RBAC tool name has. Used with `fullmatch`, since `$` with
`match` would let a trailing newline through."""

REASON_MAX_LENGTH = 500
"""Longest accepted reason. Read on every tool call, so it stays small."""

ACTOR_MAX_LENGTH = 320

KEY_PREFIX = "killswitch"

GLOBAL_SEGMENT = "global"


class _GlobalScope(enum.Enum):
    """A one-member enum so `GLOBAL` has its own type."""

    GLOBAL = GLOBAL_SEGMENT


GLOBAL = _GlobalScope.GLOBAL
"""The platform-wide scope. Not the string "global": a scope is a tenant id
or this sentinel, so no string from a request can be passed off as global."""

Scope = uuid.UUID | _GlobalScope


class InvalidSwitchTargetError(ToolError):
    """The scope, tool name, reason or actor cannot be stored. Raised before
    any write, so a bad admin action never half lands."""

    code = ErrorCode.VALIDATION


class SwitchUnavailableError(ToolError):
    """Redis could not be reached, so the state is unknown. The call is still
    denied, with `INTERNAL` since the tool is not known to be off."""

    code = ErrorCode.INTERNAL


class SwitchState(BaseModel):
    """Why a tool is off, who turned it off, and when. Extra fields mean
    something else wrote the value, so it is refused rather than guessed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str = Field(max_length=REASON_MAX_LENGTH)
    actor: str = Field(max_length=ACTOR_MAX_LENGTH)
    at: datetime


class ToolSwitchStates(BaseModel):
    """Both layers of one tool's switch, for the admin board.

    Both are kept so an admin clearing a tenant switch sees that the global
    one still holds. A value that will not parse marks only its own row, so
    one bad key cannot blank the board the admin needs to fix it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    global_state: SwitchState | None
    tenant_state: SwitchState | None
    unreadable: bool


def key_for(scope: Scope, tool: str) -> str:
    """The Redis key for a scope and tool. Every path into Redis checks its
    arguments here."""
    if isinstance(scope, _GlobalScope):
        segment = GLOBAL_SEGMENT
    elif isinstance(scope, uuid.UUID):
        segment = str(scope)
    else:
        # A plain "global" string from a request body must not pass as the
        # platform-wide scope.
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
    """One stored value as (state, readable), without raising, so the board
    can show a bad key instead of failing."""
    if raw is None:
        return None, True
    try:
        return SwitchState.model_validate_json(raw), True
    except ValidationError:
        return None, False


class KillSwitch:
    """Read and write the switches over one Redis client the caller owns."""

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
        """The state `check` would act on, or None when the tool is allowed."""
        state, _ = await self._read_effective(tenant_id, tool)
        return state

    async def list_states(
        self, tenant_id: uuid.UUID, tools: Sequence[str]
    ) -> list[ToolSwitchStates]:
        """Both layers of every named tool, in order, in one `MGET`.

        One row per asked tool, so the board can say "running". The tenant
        key comes from the argument, never a glob, so other tenants stay
        hidden. A Redis outage still raises: it is not a board of running
        tools.
        """
        names = list(tools)
        if not names:
            # `MGET` with no keys is an error, and an empty board needs no read.
            return []
        keys = [key for tool in names for key in (key_for(GLOBAL, tool), key_for(tenant_id, tool))]
        try:
            values: list[bytes | str | None] = await self._client.mget(keys)
        except RedisError as exc:
            # The message reaches the caller, so it names no key or endpoint.
            raise SwitchUnavailableError("could not read the kill switch board") from exc
        rows: list[ToolSwitchStates] = []
        for index, tool in enumerate(names):
            global_state, global_ok = _parse(values[2 * index])
            tenant_state, tenant_ok = _parse(values[2 * index + 1])
            unreadable = not (global_ok and tenant_ok)
            rows.append(
                ToolSwitchStates(
                    tool=tool,
                    # An unreadable row shows no state, not half a story.
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
            # The message may reach the caller, so it names only the tool.
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
            raw: bytes | str | None = await self._client.get(key)
        except RedisError as exc:
            raise SwitchUnavailableError(f"could not read the switch for {tool}") from exc
        if raw is None:
            return None
        try:
            return SwitchState.model_validate_json(raw)
        except ValidationError as exc:
            # Present but unparseable is not absent. Reading it as "allowed"
            # would quietly turn back on a tool an operator stopped.
            where = "platform-wide" if isinstance(scope, _GlobalScope) else "for this tenant"
            raise ToolDisabledError(
                f"{tool} is disabled: the switch set {where} is unreadable, "
                f"so the call is denied ({exc.error_count()} validation errors)"
            ) from exc
