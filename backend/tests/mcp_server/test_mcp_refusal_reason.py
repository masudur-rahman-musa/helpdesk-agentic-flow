"""How a refusal's reason rides beside its code on the wire.

Cost and isolation. Pure: the encoder is called directly and nothing is
opened. No service, no `db` marker, no socket, no waiting.
"""

from typing import Any

import pytest
from mcp import types as mcp_types

from helpdesk.mcp_server.tools.dispatch import error_result
from helpdesk.shared.core.errors import ErrorCode

SENTENCE = "A review of this scope and period is already open."


def _text(result: mcp_types.CallToolResult) -> str:
    assert result.is_error is True
    assert len(result.content) == 1
    block: Any = result.content[0]
    assert isinstance(block, mcp_types.TextContent)
    return str(block.text)


@pytest.mark.parametrize("code", sorted(ErrorCode))
def test_a_refusal_with_no_reason_is_the_bare_code(code: ErrorCode) -> None:
    """Catches an encoder that always appends a separator or a blank reason
    (`NOT_FOUND: `), so every caller that reads the bare code breaks.

    With no reason the whole text is exactly the code, for every code.
    """
    assert _text(error_result(code)) == code.value
    assert _text(error_result(code, reason="")) == code.value


@pytest.mark.parametrize("code", sorted(ErrorCode))
def test_a_refusal_with_a_reason_is_code_colon_space_reason(code: ErrorCode) -> None:
    """Catches an encoder that drops the reason, joins it without the
    separator, or puts the reason before the code.

    The whole text is exactly the code, a colon, a space and the sentence.
    """
    assert _text(error_result(code, reason=SENTENCE)) == f"{code.value}: {SENTENCE}"
