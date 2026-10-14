"""An earlier change section 6: the shared typed-error module, written before it exists.

Expected implementation surface (`helpdesk/errors.py`, pure: no I/O, no
imports from models or services):

``class ErrorCode(StrEnum)``
    Exactly the eight the design codes, value == name: ``FORBIDDEN``,
    ``NOT_FOUND``, ``SOD_CONFLICT``, ``POLICY_BLOCKED``,
    ``IDEMPOTENCY_CONFLICT``, ``TOOL_DISABLED``, ``VALIDATION``,
    ``INTERNAL``. No ninth member: the MCP boundary maps codes to user
    language, and an unmapped code would leak as free text.

``class ToolError(Exception)``
    Base for every error that crosses a tool boundary. Carries ``code:
    ErrorCode`` as a class attribute a subclass sets, and takes a single
    message argument like the existing RBAC errors do
    (``RbacDenied(tool_key.value)``), so rebasing them changes no behaviour.

``class IdempotencyConflictError(ToolError)``
    ``code == ErrorCode.IDEMPOTENCY_CONFLICT`` — same key, different
    request fingerprint (the design).

``helpdesk.auth.rbac.RbacDenied`` is rebased onto ``ToolError`` and keeps
``code == "FORBIDDEN"``; the 188-case RBAC suite stays untouched and green.
"""

import pytest

from helpdesk.mcp_server.services.idempotency import (
    InvalidIdempotencyKeyError,
    UnstorableResponseError,
)
from helpdesk.shared.auth.rbac import RbacDenied, UnknownToolError
from helpdesk.shared.errors import (
    ErrorCode,
    IdempotencyConflictError,
    NotFoundError,
    ToolDisabledError,
    ToolError,
)
from helpdesk.shared.killswitch import InvalidSwitchTargetError, SwitchUnavailableError

# the design, written out literally so the test cannot drift with the enum.
SPEC_CODES: tuple[str, ...] = (
    "FORBIDDEN",
    "NOT_FOUND",
    "SOD_CONFLICT",
    "POLICY_BLOCKED",
    "IDEMPOTENCY_CONFLICT",
    "TOOL_DISABLED",
    "VALIDATION",
    "INTERNAL",
)

# Every typed error the package exposes today, with the code it must carry.
TYPED_ERRORS: tuple[tuple[type[ToolError], str], ...] = (
    (IdempotencyConflictError, "IDEMPOTENCY_CONFLICT"),
    (RbacDenied, "FORBIDDEN"),
    # A tool name outside the matrix is a wiring bug, not a permission
    # decision: FORBIDDEN here would make the two indistinguishable to the
    # user and in the audit trail.
    (UnknownToolError, "INTERNAL"),
    (InvalidIdempotencyKeyError, "VALIDATION"),
    (UnstorableResponseError, "INTERNAL"),
    # An earlier change: an admin tripped the tool's kill switch. Distinct from
    # FORBIDDEN, which is about who is asking, not whether the tool is on.
    (ToolDisabledError, "TOOL_DISABLED"),
    # An earlier change: Redis was unreachable, so the switch state is unknown. The
    # call is still denied, but the tool is not known to be off, so this is
    # not TOOL_DISABLED — an outage must not read as an admin decision.
    (SwitchUnavailableError, "INTERNAL"),
    # An earlier change: a scope, tool name, reason or actor this module cannot store.
    (InvalidSwitchTargetError, "VALIDATION"),
    # An earlier change: the row is not in the caller's tenant. One answer for "another
    # tenant owns it" and "nobody owns it", so not-found is not an oracle.
    (NotFoundError, "NOT_FOUND"),
)


# --- the code set -------------------------------------------------------------


def test_error_codes_are_exactly_the_spec_set() -> None:
    """A missing code has no user-facing mapping; an extra one is not in the contract."""
    assert {member.value for member in ErrorCode} == set(SPEC_CODES)


def test_there_are_eight_codes() -> None:
    assert len(list(ErrorCode)) == len(SPEC_CODES) == 8


@pytest.mark.parametrize("code", SPEC_CODES)
def test_each_spec_code_exists_with_value_equal_to_its_name(code: str) -> None:
    member = ErrorCode[code]
    assert member.value == code
    assert member == code, "ErrorCode must compare equal to its wire string (StrEnum)"


def test_codes_are_strings_on_the_wire() -> None:
    """Serialised into a tool response and into audit payloads as plain strings."""
    assert all(isinstance(member, str) for member in ErrorCode)


# --- ToolError ----------------------------------------------------------------


def test_tool_error_is_an_exception() -> None:
    assert issubclass(ToolError, Exception)


def test_a_subclass_carries_its_code_and_keeps_its_message() -> None:
    """`ToolError` carries the code; construction stays single-argument."""

    class _NotFound(ToolError):
        code = ErrorCode.NOT_FOUND

    error = _NotFound("fake-subject-id")
    assert isinstance(error, ToolError)
    assert error.code == ErrorCode.NOT_FOUND
    assert str(error) == "fake-subject-id"


def test_tool_error_can_be_caught_as_the_base() -> None:
    """One `except ToolError` at the tool boundary must catch every typed error."""
    with pytest.raises(ToolError):
        raise IdempotencyConflictError("fake-key")


@pytest.mark.parametrize(("error_type", "expected_code"), TYPED_ERRORS)
def test_typed_errors_derive_from_tool_error(
    error_type: type[ToolError], expected_code: str
) -> None:
    assert issubclass(error_type, ToolError)


@pytest.mark.parametrize(("error_type", "expected_code"), TYPED_ERRORS)
def test_typed_errors_carry_their_spec_code(
    error_type: type[ToolError], expected_code: str
) -> None:
    instance = error_type("fake detail")
    assert instance.code == expected_code
    assert instance.code in set(ErrorCode), f"{error_type.__name__} carries a non-SPEC code"


# --- the RBAC rebase (no behaviour change) ------------------------------------


def test_rbac_denied_is_a_tool_error_with_forbidden() -> None:
    """An earlier change acceptance: rbac's errors rebase onto ToolError, unchanged."""
    error = RbacDenied("approve_request")
    assert isinstance(error, ToolError)
    assert error.code == ErrorCode.FORBIDDEN
    assert error.code == "FORBIDDEN", "the existing RBAC suite compares against the raw string"
    assert str(error) == "approve_request"


def test_rbac_denied_is_still_catchable_as_a_plain_exception() -> None:
    """Callers written against the closed an earlier change API keep working."""
    with pytest.raises(Exception, match="approve_request"):
        raise RbacDenied("approve_request")
