"""An earlier change section 6: the telemetry context a log line belongs to.

Written before the implementation exists. Surface under test:

``helpdesk.obs.context``
    ``class TelemetryContext(BaseModel)`` — frozen, fields ``tenant_id``,
    ``session_id``, ``trace_id``, ``principal_kind``, all ``str | None`` and
    all defaulting to ``None``
    ``def current_context() -> TelemetryContext``
    ``@contextmanager def bind_context(**fields) -> Iterator[TelemetryContext]``
    — binds a new context and restores the previous one on exit

the design in one line: `tenant_id`, `session_id`, `trace_id` and
`principal_kind` may travel in telemetry; a user id, an email or a name may
not. The last test here is that commitment, asserted on the emitted line.

No backing service: a ContextVar, two asyncio tasks and a formatter.
"""

import asyncio
import io
import json
import logging
import uuid
from typing import Any

from helpdesk.shared.core.observability.context import (
    TelemetryContext,
    bind_context,
    current_context,
)
from helpdesk.shared.core.observability.logging import FORBIDDEN_KEYS, REDACTED, JsonFormatter

TENANT_A = "tenant-a-0000-0000-0000-000000000001"
TENANT_B = "tenant-b-0000-0000-0000-000000000002"

# Obviously fake identity values, planted to prove they cannot reach a line.
PROBE_USER_ID = "11111111-2222-3333-4444-555555555555"
PROBE_EMAIL = "context.probe@fake.invalid"
PROBE_DISPLAY_NAME = "Context Probe"
PROBE_PASSWORD = "not-a-real-password-tests-only"
PROBE_ACTOR_REF = "user:context-probe"


def _emit(message: str, extra: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    """Format one record through `JsonFormatter`; return the raw text and its parse.

    A throwaway non-propagating logger per call, so concurrent tasks cannot
    write into each other's capture buffer.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger(f"test.obs.context.{uuid.uuid4().hex}")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        logger.info(message, extra=extra or {})
    finally:
        logger.removeHandler(handler)
        handler.close()
    raw = stream.getvalue()
    parsed: dict[str, Any] = json.loads(raw)
    return raw, parsed


def test_a_bound_context_reaches_the_emitted_line() -> None:
    """Catches a formatter that reads the contextvar once, at import time (or
    at handler construction), so every line carries whatever was bound then —
    which for a long-lived process means the first request's tenant on every
    later request's line."""
    with bind_context(
        tenant_id=TENANT_A,
        session_id="sid-a",
        trace_id="trace-a",
        principal_kind="employee",
    ):
        _, line = _emit("inside the bound context")

    assert line["tenant_id"] == TENANT_A
    assert line["session_id"] == "sid-a"
    assert line["trace_id"] == "trace-a"
    assert line["principal_kind"] == "employee"


def test_the_context_is_restored_on_exit() -> None:
    """Catches a bind that sets the ContextVar without keeping the token to
    reset it: the inner value then outlives its block, so every line logged
    after a nested bind reports the wrong tenant with no error anywhere."""
    assert current_context() == TelemetryContext()

    with bind_context(tenant_id=TENANT_A, session_id="sid-a") as outer:
        assert current_context().tenant_id == TENANT_A

        with bind_context(tenant_id=TENANT_B, session_id="sid-b"):
            assert current_context().tenant_id == TENANT_B

        assert current_context() == outer
        assert current_context().session_id == "sid-a"

    # An unbound context is every field None, not the last thing bound.
    assert current_context() == TelemetryContext()
    assert current_context().tenant_id is None


def test_two_concurrent_tasks_do_not_see_each_other_s_context() -> None:
    """Catches a module global instead of a ContextVar: with two chat turns in
    flight, the second bind overwrites the first, and one tenant's session id
    is published on another tenant's log line — the exact disclosure SPEC
    §13.5 exists to prevent.

    Each task logs only after BOTH have bound, so a global would have been
    overwritten by the time the first task's line is rendered.
    """

    async def scenario() -> dict[str, dict[str, Any]]:
        both_bound = asyncio.Barrier(2)
        both_logged = asyncio.Barrier(2)
        lines: dict[str, dict[str, Any]] = {}

        async def turn(name: str, tenant: str) -> None:
            with bind_context(tenant_id=tenant, session_id=f"sid-{name}"):
                await both_bound.wait()
                _, lines[name] = _emit(f"turn-{name}")
                await both_logged.wait()

        await asyncio.gather(turn("a", TENANT_A), turn("b", TENANT_B))
        return lines

    lines = asyncio.run(scenario())

    assert lines["a"]["tenant_id"] == TENANT_A
    assert lines["a"]["session_id"] == "sid-a"
    assert lines["b"]["tenant_id"] == TENANT_B
    assert lines["b"]["session_id"] == "sid-b"


def test_no_forbidden_field_survives_a_line_logged_with_user_id_in_extra() -> None:
    """Catches a wiring that trusts callers not to pass identity: redaction is
    a property of the formatter every line goes through, so a caller handing
    `user_id` and `email` to `extra` gets them redacted rather than honoured,
    while the tenant and session the line is allowed to carry stay."""
    identity: dict[str, Any] = {
        "user_id": PROBE_USER_ID,
        "email": PROBE_EMAIL,
        "display_name": PROBE_DISPLAY_NAME,
        "password": PROBE_PASSWORD,
        "actor_ref": PROBE_ACTOR_REF,
    }
    assert set(identity) == set(FORBIDDEN_KEYS)

    with bind_context(tenant_id=TENANT_A, session_id="sid-a", principal_kind="employee"):
        raw, line = _emit("a caller passing identity", dict(identity))

    for key in FORBIDDEN_KEYS:
        assert line[key] == REDACTED, key
    for value in identity.values():
        assert value not in raw

    assert line["tenant_id"] == TENANT_A
    assert line["session_id"] == "sid-a"
    assert line["principal_kind"] == "employee"
