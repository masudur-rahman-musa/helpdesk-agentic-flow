"""What makes two requests the same request.

The fingerprint is the whole basis for telling a replay from a conflict, so
it covers the tool name and every argument, and it uses the same canonical
encoder as the audit chain: one set of bytes, no chance of the two hashes
disagreeing about what a request was.

Key order is not part of a request (a client may serialise a dict any way),
but value types are: `1` and `"1"` are different requests, and the encoder
renders them differently rather than collapsing them.

Pure: no I/O, no clock.
"""

import hashlib
from collections.abc import Mapping

from helpdesk.shared.core.canonical import canonical_json


def fingerprint(tool_name: str, args: Mapping[str, object]) -> str:
    """sha256 of the canonical request as 64 lowercase hex chars.

    The tool name and the arguments sit in separate fields rather than being
    concatenated, so no crafted tool name can imitate another request.
    """
    return hashlib.sha256(canonical_json({"tool": tool_name, "args": args})).hexdigest()
