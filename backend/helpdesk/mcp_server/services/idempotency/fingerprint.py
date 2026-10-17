"""What makes two requests the same request.

Uses the same JSON encoder as the audit chain, so the two hashes never
disagree about what a request was. Key order does not count; value types do
(`1` and `"1"` differ).
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
