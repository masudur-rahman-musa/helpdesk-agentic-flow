"""The prompt loader and the two shipped prompt files.

Expected implementation surface (the implementer must provide exactly this):

``agent_api.prompts``
    ``PROMPTS_DIR: Path`` — ``services/agent-api/prompts/``.
    ``@dataclass(frozen=True) class LoadedPrompt`` — ``name``, ``text``,
        ``sha256``.
    ``def load_prompt(name: str) -> LoadedPrompt`` — reads
        ``PROMPTS_DIR / f"{name}.md"`` and returns the sha256 of the file's
        BYTES as a 64-character lowercase hex digest. NOT cached: a rewritten
        file yields a new digest on the next call, because the digest is what
        every trace records as ``prompt_version``.
        An unknown name raises ``PromptNotFoundError`` (a ``ToolError`` with
        ``code = ErrorCode.INTERNAL``).
    Files: ``prompts/system.md`` and ``prompts/intent_extraction.md``.

A third file, ``prompts/tool_planning.md``, is the prompt
``plan_tools`` sends under the ``agent-strong`` alias. Its digest rides on
``TurnState.planning_prompt_version``, never on ``prompt_version``, because one
string cannot carry two versions.

A fourth file, ``prompts/reply_composition.md``, is the prompt
``compose_reply`` sends under the ``agent-default`` alias. It must carry the
two obligations in its own words, because the composition is the one
call whose output a person reads.

Nothing here needs Postgres, Redis, Qdrant or a socket: the loader reads the
repository's own files, and the mutation case writes into `tmp_path` with
``PROMPTS_DIR`` monkeypatched, so the shipped prompts are never edited by a
test run.
"""

import hashlib
from pathlib import Path

import pytest

import helpdesk.agent_api.agent as agent_package
from helpdesk.agent_api.agent import prompts as prompts_module
from helpdesk.agent_api.agent.gates import INTENT_TOOLS
from helpdesk.agent_api.agent.prompts import (
    PROMPTS_DIR,
    LoadedPrompt,
    PromptNotFoundError,
    load_prompt,
)
from helpdesk.agent_api.agent.state import Intent
from helpdesk.shared.core.errors import ErrorCode, ToolError

SYSTEM_PROMPT = "system"
INTENT_PROMPT = "intent_extraction"
PLANNING_PROMPT = "tool_planning"
COMPOSITION_PROMPT = "reply_composition"

SHIPPED_PROMPTS = (SYSTEM_PROMPT, INTENT_PROMPT, PLANNING_PROMPT, COMPOSITION_PROMPT)

SYSTEM_PROMPT_REQUIRED_PHRASES = (
    "does not decide access",
    "policy engine",
    "never claim",
    "tool result",
)
"""The two obligations, in the words a reader can check: the agent
does not decide access (the policy engine does), and it must never claim a
grant happened unless a tool result says so."""

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


def test_the_system_prompt_states_the_agent_does_not_decide_access() -> None:
    """Catches a system prompt missing the two obligations — the one
    guardrail this repo's whole design rests on. Without them a model is free
    to answer "you now have access" from its own judgement, with no tool result
    behind it and no policy engine consulted."""
    text = load_prompt(SYSTEM_PROMPT).text.lower()

    missing = [phrase for phrase in SYSTEM_PROMPT_REQUIRED_PHRASES if phrase not in text]
    assert not missing, f"system.md does not state: {missing}"


def test_the_intent_prompt_names_every_intent_the_enum_carries() -> None:
    """Catches the prompt and the closed label set
    drifting apart. A 14th intent added to the enum but never described to the
    model is a label the model can never produce; the schema would still
    validate, so nothing else in the suite goes red."""
    text = load_prompt(INTENT_PROMPT).text.lower()

    missing = [intent.value for intent in Intent if intent.value not in text]
    assert not missing, f"intent_extraction.md does not name: {missing}"


def test_the_planning_prompt_names_every_tool_a_row_can_offer() -> None:
    """The same failure `test_the_intent_prompt_...`
    guards for the label set. Catches a tool added to an `INTENT_TOOLS` row
    and never described to the model: it would be offered in the allowed set
    with no explanation of when to reach for it, so the model either never
    calls it or calls it for the wrong turn — and nothing else in the suite
    goes red, because the schema still validates either way.

    Read off `INTENT_TOOLS` rather than off `TOOL_INPUTS`: `search_policy` is
    in no row and the planner must never be told it may call it."""
    text = load_prompt(PLANNING_PROMPT).text.lower()
    offered = sorted(frozenset().union(*INTENT_TOOLS.values()))

    missing = [name for name in offered if name not in text]
    assert not missing, f"tool_planning.md does not name: {missing}"


def test_the_composition_prompt_states_the_agent_does_not_decide_access() -> None:
    """Catches a composition prompt that describes the format of a
    reply and nothing else: the composing call is handed the tool results and
    the policy passages together, so a model told only to "write a friendly
    answer" is free to read a `POLICY_BLOCKED` as a hurdle and tell the user
    the access is on its way.

    The same two obligations `system.md` carries, asserted separately here
    because the composition is the one call whose words reach a person: the
    agent does not decide access, and it may not claim a grant happened unless
    a tool result says so. The decision-word post-check is the guard that
    catches a reply which says it anyway; this is the instruction it rests on.
    """
    text = load_prompt(COMPOSITION_PROMPT).text.lower()

    missing = [phrase for phrase in SYSTEM_PROMPT_REQUIRED_PHRASES if phrase not in text]
    assert not missing, f"reply_composition.md does not state: {missing}"


def test_the_composition_prompt_asks_for_the_question_mark() -> None:
    """Catches a prompt left as it was: the writer would never be told to answer
    in the shape or what the mark means, and every reply would read as no question.
    The prompt must name both fields and say the mark is true only when the answer
    asks the person something."""
    text = load_prompt(COMPOSITION_PROMPT).text.lower()

    assert "asks_question" in text
    assert "`text`" in text
    assert "true only when" in text
    assert "asks the person" in text


def test_the_intent_prompt_puts_own_tickets_under_check_my_access() -> None:
    """Catches an intent prompt left as it was: a "what happened to my ticket"
    message would be labelled `open_ticket` and file a new ticket instead of
    reading the existing ones."""
    text = load_prompt(INTENT_PROMPT).text.lower()
    line = next(row for row in text.split("\n- ") if row.startswith("`check_my_access`"))

    assert "ticket" in line


def test_the_composition_prompt_covers_ticket_status() -> None:
    """Catches a composition prompt that never says how to report tickets: the
    writer could skip the ticket number or the open or solved status, stay
    silent for a person with none, or describe someone else's ticket."""
    text = load_prompt(COMPOSITION_PROMPT).text.lower()

    assert "open or solved" in text
    assert "no tickets" in text
    assert "only their own" in text


PLAN_54_PROMPT_FILES = (INTENT_PROMPT, PLANNING_PROMPT, COMPOSITION_PROMPT)
"""The three prompt files; `system.md` sits in the same folder."""


def test_prompts_dir_is_the_folder_holding_the_prompt_files() -> None:
    """Catches a `PROMPTS_DIR` resolved one level too high, at
    `agent_api/prompts`, which does not exist: every `load_prompt` call then
    raises `INTERNAL` and every turn dies before the first model call.

    The expected path is built from the `agent` package's own location, not
    from `PROMPTS_DIR`, so a wrong constant cannot agree with itself."""
    expected = (Path(agent_package.__file__).resolve().parent / "prompts").resolve()

    assert expected.parts[-4:] == ("helpdesk", "agent_api", "agent", "prompts")
    assert PROMPTS_DIR.resolve() == expected
    for name in PLAN_54_PROMPT_FILES:
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
