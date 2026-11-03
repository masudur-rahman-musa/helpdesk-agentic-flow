"""The shared typed-error module.

Expected implementation surface (`helpdesk/errors.py`, pure: no I/O, no
imports from models or services):

``class ErrorCode(StrEnum)``
    Exactly the eight error codes, value == name: ``FORBIDDEN``,
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
    request fingerprint.

``helpdesk.auth.rbac.RbacDenied`` is rebased onto ``ToolError`` and keeps
``code == "FORBIDDEN"``; the 188-case RBAC suite stays untouched and green.
"""

import importlib
import pkgutil

import pytest

from helpdesk.mcp_server.services.idempotency import (
    InvalidIdempotencyKeyError,
    UnstorableResponseError,
)
from helpdesk.shared.access.review_types import ReviewAlreadyOpenError
from helpdesk.shared.auth.rbac import RbacDenied, UnknownToolError
from helpdesk.shared.core.errors import (
    ErrorCode,
    IdempotencyConflictError,
    NotFoundError,
    ToolDisabledError,
    ToolError,
)
from helpdesk.shared.services.killswitch import InvalidSwitchTargetError, SwitchUnavailableError

# The code list, written out literally so the test cannot drift with the enum.
SPEC_CODES: tuple[str, ...] = (
    "FORBIDDEN",
    "NOT_FOUND",
    "SOD_CONFLICT",
    "POLICY_BLOCKED",
    "IDEMPOTENCY_CONFLICT",
    "TOOL_DISABLED",
    "VALIDATION",
    "INTERNAL",
    "REQUEST_NOT_PENDING",
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
    # An admin tripped the tool's kill switch. Distinct from
    # FORBIDDEN, which is about who is asking, not whether the tool is on.
    (ToolDisabledError, "TOOL_DISABLED"),
    # Redis was unreachable, so the switch state is unknown. The
    # call is still denied, but the tool is not known to be off, so this is
    # not TOOL_DISABLED — an outage must not read as an admin decision.
    (SwitchUnavailableError, "INTERNAL"),
    # A scope, tool name, reason or actor this module cannot store.
    (InvalidSwitchTargetError, "VALIDATION"),
    # The row is not in the caller's tenant. One answer for "another
    # tenant owns it" and "nobody owns it", so not-found is not an oracle.
    (NotFoundError, "NOT_FOUND"),
)


# --- the code set -------------------------------------------------------------


def test_error_codes_are_exactly_the_spec_set() -> None:
    """A missing code has no user-facing mapping; an extra one is not in the contract."""
    assert {member.value for member in ErrorCode} == set(SPEC_CODES)


def test_there_are_nine_codes() -> None:
    assert len(list(ErrorCode)) == len(SPEC_CODES) == 9


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
    assert instance.code in set(ErrorCode), (
        f"{error_type.__name__} carries a code outside ErrorCode"
    )


# --- the RBAC rebase (no behaviour change) ------------------------------------


def test_rbac_denied_is_a_tool_error_with_forbidden() -> None:
    """Rbac's errors rebase onto ToolError, unchanged."""
    error = RbacDenied("approve_request")
    assert isinstance(error, ToolError)
    assert error.code == ErrorCode.FORBIDDEN
    assert error.code == "FORBIDDEN", "the existing RBAC suite compares against the raw string"
    assert str(error) == "approve_request"


def test_rbac_denied_is_still_catchable_as_a_plain_exception() -> None:
    """Callers written against the original RBAC API keep working."""
    with pytest.raises(Exception, match="approve_request"):
        raise RbacDenied("approve_request")


# --- the fixed reason every typed error carries -----------------------------------------------


def _every_tool_error() -> set[type[ToolError]]:
    """Every typed error class in the app, found by importing each module that
    could define one, so a class added later is swept too."""
    for package in ("helpdesk.shared", "helpdesk.agent_api", "helpdesk.mcp_server"):
        root = importlib.import_module(package)
        for module in pkgutil.walk_packages(root.__path__, f"{package}."):
            try:
                importlib.import_module(module.name)
            except ImportError:  # pragma: no cover - a module needing an absent extra
                continue
    found: set[type[ToolError]] = set()
    stack: list[type[ToolError]] = [ToolError]
    while stack:
        for sub in stack.pop().__subclasses__():
            if sub not in found:
                found.add(sub)
                stack.append(sub)
    return found


def test_every_typed_error_class_carries_one_plain_sentence_as_its_reason() -> None:
    """Catches a typed error class with no reason (the model is told nothing),
    an empty or padded one, one that is not a string, or one holding a line
    break that would break the one-line `CODE: reason` wire text.

    Every subclass of the base, found by walking the app, has a non-blank
    one-line string reason, and the sweep finds a real number of classes so it
    cannot pass over none.
    """
    classes = _every_tool_error()

    assert len(classes) > 30
    for cls in sorted(classes, key=lambda c: c.__qualname__):
        reason = cls.reason
        assert isinstance(reason, str), cls
        assert reason.strip() == reason != "", cls
        assert "\n" not in reason and "\r" not in reason, cls


def test_the_base_reason_is_a_sentence_and_not_the_message() -> None:
    """Catches a reason built from the exception's own message (row data, log
    text), which would put it on the wire and in front of the model.

    The same sentence comes back whatever message the error was made with, and
    it never contains that message.
    """
    secret = "row 7 of tenant fake-secret-tenant"
    plain = NotFoundError(secret)
    other = NotFoundError("another message")

    assert plain.reason == other.reason == NotFoundError.reason
    assert secret not in plain.reason
    assert ToolError.reason != ""


def test_a_review_already_open_error_says_so() -> None:
    """Catches a review-already-open error that falls back to the generic
    sentence, so the model still has to guess why the refusal happened.

    The reason names the review, its scope and its period and says it is
    already open, and the error's message never appears in it.
    """
    reason = ReviewAlreadyOpenError("review 9 scope fake").reason.lower()

    assert "review" in reason
    assert "scope" in reason
    assert "period" in reason
    assert "already open" in reason
    assert "review 9" not in reason
    assert ReviewAlreadyOpenError.reason != ToolError.reason
