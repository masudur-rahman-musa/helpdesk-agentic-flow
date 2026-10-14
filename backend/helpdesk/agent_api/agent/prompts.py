"""Prompt files, loaded from disk with their sha256 (the design).

Prompts are files in `services/agent-api/prompts/`, versioned in git, and the
digest of the FILE'S BYTES is what every trace records as `prompt_version`.
Bytes, not stripped text: a prompt edited only in its whitespace is still a
different prompt to the model, and a version that did not move would put two
different behaviours under one label.

Nothing here is cached. A cache is the obvious optimisation — the files are
tiny and read once per turn — and it is the wrong one: an edited prompt would
keep its old digest until the process restarted, so a turn would be traced
under the version it replaced. Pinned by
`test_agent_prompts.py::test_the_digest_changes_when_the_file_changes`, which
rewrites the file between two calls; `PROMPTS_DIR` is read at CALL time for
the same reason.

A missing file is `INTERNAL`, never a bare `FileNotFoundError`: a prompt is
this service's own asset, so its absence is a deployment fault and not
something a caller's input can fix.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from helpdesk.shared.errors import ErrorCode, ToolError

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
"""`services/agent-api/prompts/`, resolved from this module's own location so
the loader works from any working directory."""


class PromptNotFoundError(ToolError):
    """A prompt file this service ships is not on disk.

    `INTERNAL`, pinned by
    `test_agent_prompts.py::test_an_unknown_prompt_name_raises_the_typed_error`:
    nothing the caller sent is wrong, and no argument change makes the file
    appear.
    """

    code = ErrorCode.INTERNAL


@dataclass(frozen=True)
class LoadedPrompt:
    """One prompt file and the version every trace records it under.

    Frozen: `sha256` is the claim "this text is what ran", and a caller that
    could edit either half would break the tie between a trace and a file.
    """

    name: str
    text: str
    sha256: str


def load_prompt(name: str) -> LoadedPrompt:
    """Read `PROMPTS_DIR / f"{name}.md"` and digest its bytes.

    The digest is taken over the raw bytes and the text is returned exactly as
    read, so the two halves describe one file. Pinned by
    `test_agent_prompts.py::test_load_prompt_returns_the_files_real_sha256`,
    which recomputes the digest from the same file on disk.
    """
    path = PROMPTS_DIR / f"{name}.md"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PromptNotFoundError(f"prompt {name!r} is not on disk") from exc
    return LoadedPrompt(name=name, text=raw.decode(), sha256=hashlib.sha256(raw).hexdigest())
