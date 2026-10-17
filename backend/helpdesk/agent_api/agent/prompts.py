"""Prompt files, loaded from disk with their sha256.

The digest of the file's bytes is recorded on every trace as
`prompt_version`, so even a whitespace edit gets a new version. Not cached:
an edited prompt would keep its old digest until restart. A missing file is
`INTERNAL`, a deployment fault no caller input can fix.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from helpdesk.shared.core.errors import ErrorCode, ToolError

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
"""The prompts folder beside this module, so loading works from any working
directory."""


class PromptNotFoundError(ToolError):
    """A prompt file this service ships is not on disk."""

    code = ErrorCode.INTERNAL


@dataclass(frozen=True)
class LoadedPrompt:
    """One prompt file and its digest. Frozen, so text and digest always
    describe the same file."""

    name: str
    text: str
    sha256: str


def load_prompt(name: str) -> LoadedPrompt:
    """Read the prompt file and digest its raw bytes."""
    path = PROMPTS_DIR / f"{name}.md"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PromptNotFoundError(f"prompt {name!r} is not on disk") from exc
    return LoadedPrompt(name=name, text=raw.decode(), sha256=hashlib.sha256(raw).hexdigest())
