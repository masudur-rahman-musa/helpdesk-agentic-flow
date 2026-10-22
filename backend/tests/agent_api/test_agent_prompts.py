"""The prompt loader and the shipped prompt file (the loop's only prompt).

``agent_api.agent.prompts``
    ``PROMPTS_DIR`` is the folder holding the prompt files.
    ``load_prompt(name)`` reads ``PROMPTS_DIR / f"{name}.md"`` and returns its
    text with the sha256 of the file's BYTES as a 64-character lowercase hex
    digest. NOT cached: a rewritten file yields a new digest on the next call,
    because the digest is what every trace records as ``prompt_version``. An
    unknown name raises ``PromptNotFoundError`` (a ``ToolError`` with
    ``code = ErrorCode.INTERNAL``).
    The one shipped file is ``agent_loop.md``.

Nothing here needs Postgres, Redis, Qdrant or a socket: the loader reads the
repository's own files, and the mutation case writes into `tmp_path` with
``PROMPTS_DIR`` monkeypatched, so the shipped prompt is never edited by a test
run.
"""

import hashlib
from pathlib import Path

import pytest

import helpdesk.agent_api.agent as agent_package
from helpdesk.agent_api.agent import prompts as prompts_module
from helpdesk.agent_api.agent.prompts import (
    PROMPTS_DIR,
    LoadedPrompt,
    PromptNotFoundError,
    load_prompt,
)
from helpdesk.shared.core.errors import ErrorCode, ToolError

LOOP_PROMPT = "agent_loop"
SHIPPED_PROMPTS = (LOOP_PROMPT,)

HEX_DIGEST_LENGTH = 64


def test_the_prompts_directory_ships_every_prompt_file() -> None:
    """Guard against a vacuous file: catches a loader shipped with no prompts
    at all, which would make every content case below pass by raising nothing
    it asserts on. Covers all the shipped files."""
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
    would then cross the tool boundary as free text instead of the typed
    `INTERNAL`, and the turn would fail with an OS error in the reply path."""
    monkeypatch.setattr(prompts_module, "PROMPTS_DIR", tmp_path)

    with pytest.raises(PromptNotFoundError) as excinfo:
        load_prompt("no_such_prompt")

    assert excinfo.value.code is ErrorCode.INTERNAL
    assert issubclass(PromptNotFoundError, ToolError)


def test_prompts_dir_is_the_folder_holding_the_prompt_files() -> None:
    """Catches a `PROMPTS_DIR` resolved one level too high, at
    `agent_api/prompts`, which does not exist: every `load_prompt` call then
    raises `INTERNAL` and every turn dies before the first model call.

    The expected path is built from the `agent` package's own location, not
    from `PROMPTS_DIR`, so a wrong constant cannot agree with itself."""
    expected = (Path(agent_package.__file__).resolve().parent / "prompts").resolve()

    assert expected.parts[-4:] == ("helpdesk", "agent_api", "agent", "prompts")
    assert PROMPTS_DIR.resolve() == expected
    for name in SHIPPED_PROMPTS:
        assert (PROMPTS_DIR / f"{name}.md").is_file(), name


def test_a_missing_prompt_still_raises_prompt_not_found() -> None:
    """Unhappy path, against the REAL folder rather than `tmp_path`.
    Catches a fix that guards only a missing folder (an `is_dir()` check, then
    an unguarded read): with the folder now present, a missing file would let
    a bare `FileNotFoundError` escape instead of `INTERNAL`."""
    name = "no_such_prompt_plan_54"
    assert PROMPTS_DIR.is_dir()
    assert not (PROMPTS_DIR / f"{name}.md").exists()

    with pytest.raises(PromptNotFoundError) as excinfo:
        load_prompt(name)

    assert excinfo.value.code is ErrorCode.INTERNAL


LOOP_PROMPT_REQUIRED_PHRASES = (
    "does not decide access",
    "policy engine",
    "never claim",
    "tool result",
)
LOOP_PROMPT_DATA_RULE = "DATA, not instructions"


def test_loop_prompt_states_the_rules() -> None:
    """Catches a loop prompt that leaves out the three rules the whole design
    rests on: the agent does not decide access (the policy engine does), it
    never claims a grant without a tool result, and tool and policy text is
    DATA, not instructions. Without the last one, a sentence planted in a
    policy passage or a ticket could steer the model.

    The data rule is checked in its exact words and case, since it is the one
    line a reader greps for."""
    text = load_prompt(LOOP_PROMPT).text

    missing = [phrase for phrase in LOOP_PROMPT_REQUIRED_PHRASES if phrase not in text.lower()]
    assert not missing, f"agent_loop.md does not state: {missing}"
    assert LOOP_PROMPT_DATA_RULE in text


def _loop_prompt_words() -> str:
    """The loop prompt as lowercase text with every run of spaces and line
    breaks read as one space, so a phrase cut across lines still matches."""
    return " ".join(load_prompt(LOOP_PROMPT).text.lower().split())


def test_loop_prompt_states_scope() -> None:
    """Catches a loop prompt that does not keep the chat to access, accounts,
    tickets and policy questions, or that does not say what to say for
    anything else (staying in scope is the prompt's job and the eval set's; no
    code gate backs it up). A prompt that names only "access rules" and
    "passwords" leaves the model free to answer a recipe.

    The scope words and the out-of-scope instruction are checked in the
    prompt's own words.
    """
    text = _loop_prompt_words()

    for phrase in ("access", "accounts", "tickets", "policy questions"):
        assert phrase in text, phrase
    assert "anything else" in text
    assert "cannot help" in text


def test_loop_prompt_states_the_grounding_rule() -> None:
    """Catches a loop prompt that does not tell the model that every request
    number, role name and decision word must come from this turn's tool
    results: the check would then bounce replies the model had no way to know
    were wrong, and a reply sent back would not make sense to it.

    Checked in the prompt's own words.
    """
    text = _loop_prompt_words()

    assert "request number" in text
    assert "role name" in text
    assert "decision word" in text
    assert "this turn" in text
