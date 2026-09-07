"""An earlier change §6: the prompt loader and the two shipped prompt files.

Expected implementation surface (the implementer must provide exactly this):

``agent_api.prompts``
    ``PROMPTS_DIR: Path`` — ``services/agent-api/prompts/``.
    ``@dataclass(frozen=True) class LoadedPrompt`` — ``name``, ``text``,
        ``sha256``.
    ``def load_prompt(name: str) -> LoadedPrompt`` — reads
        ``PROMPTS_DIR / f"{name}.md"`` and returns the sha256 of the file's
        BYTES as a 64-character lowercase hex digest. NOT cached: a rewritten
        file yields a new digest on the next call, because the digest is what
        every trace records as ``prompt_version`` (the design).
        An unknown name raises ``PromptNotFoundError`` (a ``ToolError`` with
        ``code = ErrorCode.INTERNAL``).
    Files: ``prompts/system.md`` and ``prompts/intent_extraction.md``.

Nothing here needs Postgres, Redis, Qdrant or a socket: the loader reads the
repository's own files, and the mutation case writes into `tmp_path` with
``PROMPTS_DIR`` monkeypatched, so the shipped prompts are never edited by a
test run.
"""

import hashlib
from pathlib import Path

import pytest
from agent_api import prompts as prompts_module
from agent_api.graph.state import Intent
from agent_api.prompts import PROMPTS_DIR, LoadedPrompt, PromptNotFoundError, load_prompt
from helpdesk.errors import ErrorCode, ToolError

SYSTEM_PROMPT = "system"
INTENT_PROMPT = "intent_extraction"

SHIPPED_PROMPTS = (SYSTEM_PROMPT, INTENT_PROMPT)

SYSTEM_PROMPT_REQUIRED_PHRASES = (
    "does not decide access",
    "policy engine",
    "never claim",
    "tool result",
)
"""the design's two obligations, in the words a reader can check: the agent
does not decide access (the policy engine does), and it must never claim a
grant happened unless a tool result says so."""

HEX_DIGEST_LENGTH = 64


def test_the_prompts_directory_ships_both_files() -> None:
    """Guard against a vacuous file: catches a loader shipped with no prompts
    at all, which would make every content case below pass by raising nothing
    it asserts on."""
    assert PROMPTS_DIR.is_dir()
    for name in SHIPPED_PROMPTS:
        assert (PROMPTS_DIR / f"{name}.md").is_file(), name


@pytest.mark.parametrize("name", SHIPPED_PROMPTS)
def test_load_prompt_returns_the_files_real_sha256(name: str) -> None:
    """Catches a `prompt_version` that is anything other than the file's own
    digest — a hash of the stripped text, of a normalised copy, or a hand-kept
    version string — any of which makes an edited prompt keep its old version
    in every trace."""
    path = PROMPTS_DIR / f"{name}.md"
    expected = hashlib.sha256(path.read_bytes()).hexdigest()

    loaded = load_prompt(name)

    assert isinstance(loaded, LoadedPrompt)
    assert loaded.name == name
    assert loaded.text == path.read_text()
    assert loaded.sha256 == expected
    assert len(loaded.sha256) == HEX_DIGEST_LENGTH
    assert loaded.sha256 == loaded.sha256.lower()


def test_the_digest_changes_when_the_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a cached loader (`@lru_cache`, or a dict filled at import): the
    second read would return the first digest, so a prompt edited between two
    turns would be traced under the version it replaced. Requires the loader
    to read `PROMPTS_DIR` at CALL time rather than binding it at definition."""
    monkeypatch.setattr(prompts_module, "PROMPTS_DIR", tmp_path)
    path = tmp_path / "temporary.md"

    path.write_text("first body\n")
    first = load_prompt("temporary")

    path.write_text("second body, one word longer\n")
    second = load_prompt("temporary")

    assert first.sha256 == hashlib.sha256(b"first body\n").hexdigest()
    assert second.sha256 == hashlib.sha256(b"second body, one word longer\n").hexdigest()
    assert first.sha256 != second.sha256


def test_an_unknown_prompt_name_raises_the_typed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a loader that lets `FileNotFoundError` escape: a missing prompt
    would then cross the tool boundary as free text instead of the design's
    `INTERNAL`, and the turn would fail with an OS error in the reply path."""
    monkeypatch.setattr(prompts_module, "PROMPTS_DIR", tmp_path)

    with pytest.raises(PromptNotFoundError) as excinfo:
        load_prompt("no_such_prompt")

    assert excinfo.value.code is ErrorCode.INTERNAL
    assert issubclass(PromptNotFoundError, ToolError)


def test_the_system_prompt_states_the_agent_does_not_decide_access() -> None:
    """Catches a system prompt missing the design's two obligations — the one
    guardrail this repo's whole design rests on. Without them a model is free
    to answer "you now have access" from its own judgement, with no tool result
    behind it and no policy engine consulted."""
    text = load_prompt(SYSTEM_PROMPT).text.lower()

    missing = [phrase for phrase in SYSTEM_PROMPT_REQUIRED_PHRASES if phrase not in text]
    assert not missing, f"system.md does not state: {missing}"


def test_the_intent_prompt_names_every_intent_the_enum_carries() -> None:
    """Catches plan §5's first risk: the prompt and the closed label set
    drifting apart. A 14th intent added to the enum but never described to the
    model is a label the model can never produce; the schema would still
    validate, so nothing else in the suite goes red."""
    text = load_prompt(INTENT_PROMPT).text.lower()

    missing = [intent.value for intent in Intent if intent.value not in text]
    assert not missing, f"intent_extraction.md does not name: {missing}"
