"""The old intent pipeline is deleted and the loop is scored ahead of it.

Pure file checks: no database, no network, no model. The paths are built from
this file's own place, so a moved folder cannot make a check agree with itself.
"""

import ast
import json
import re
import tomllib
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from helpdesk.shared.core.config import Settings

BACKEND = Path(__file__).resolve().parents[2]
HELPDESK = BACKEND / "helpdesk"
AGENT = HELPDESK / "agent_api" / "agent"
PROMPTS = AGENT / "prompts"
SCORES = BACKEND / "evals" / "engine-scores.json"

DELETED_MODULES = (
    "resume",
    "replay",
    "understand",
    "clarify_cap",
    "pushback",
    "follow_up",
    "required_fields",
    "request_draft",
    "requestable",
    "next_steps",
    "errors",
)
DELETED_PROMPTS = ("intent_extraction", "reply_composition", "system", "tool_planning")
KEPT_MODULES = (
    "loop",
    "graph",
    "checkpointer",
    "loop_tools",
    "confirmation",
    "postcheck",
    "gates",
    "role_words",
    "state",
    "tool_args",
)
CONVERSATION_CASES = 33


@pytest.mark.parametrize("name", DELETED_MODULES)
def test_pipeline_modules_are_gone(name: str) -> None:
    """Catches a switch-over that leaves a pipeline module on disk (dead code a
    later change could wire back in), whether as a module file or a package.

    One case per module so the failure names the one left behind.
    """
    assert not (AGENT / f"{name}.py").exists()
    assert not (AGENT / name).exists()


def test_pipeline_node_package_is_gone() -> None:
    """Catches a delete that removes the modules but leaves the `nodes/` package
    (the intent gate and the writers live there)."""
    assert not (AGENT / "nodes").exists()


@pytest.mark.parametrize("name", DELETED_PROMPTS)
def test_pipeline_prompts_are_gone(name: str) -> None:
    """Catches a delete that leaves a pipeline prompt file shipped, so its digest
    would still look like a version of something that no longer runs."""
    assert not (PROMPTS / f"{name}.md").exists()


def test_loop_files_are_still_there() -> None:
    """Catches a delete that goes too far (the loop, its prompt, or a kept
    module removed), and keeps the checks above from passing on an empty tree.

    Also pins that the loop prompt is the only prompt left.
    """
    for name in KEPT_MODULES:
        assert (AGENT / f"{name}.py").is_file(), name
    assert sorted(path.name for path in PROMPTS.glob("*.md")) == ["agent_loop.md"]


def _imported_modules(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            found.append(node.module)
            found.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def test_only_the_graph_and_checkpointer_import_langgraph() -> None:
    """Catches a module other than the graph and the checkpointer that imports
    `langgraph` (the frame would then spread into the loop's own steps and
    tools), and a graph that stops being the place that uses it.

    Every import in every `.py` file is read from the syntax tree, so a mention
    in a comment or docstring does not count and a late import inside a
    function does. The set of files that import it must be exactly those two.
    """
    files = sorted(HELPDESK.rglob("*.py"))
    assert len(files) > 50
    importers = {
        path.relative_to(AGENT).as_posix()
        for path in files
        for module in _imported_modules(path)
        if module == "langgraph" or module.startswith("langgraph.")
        if AGENT in path.parents
    }
    outside = [
        f"{path.relative_to(BACKEND)}: {module}"
        for path in files
        if AGENT not in path.parents
        for module in _imported_modules(path)
        if module == "langgraph" or module.startswith("langgraph.")
    ]
    assert outside == []
    assert importers == {"graph.py", "checkpointer.py"}


def test_no_kept_module_imports_a_deleted_one() -> None:
    """Catches a kept module that still imports a deleted pipeline module (it
    would fail on import in production although this suite never loads it), by
    reading the imports of every file under `helpdesk/`."""
    deleted = {f"helpdesk.agent_api.agent.{name}" for name in DELETED_MODULES}
    deleted.add("helpdesk.agent_api.agent.nodes")
    offenders = [
        f"{path.relative_to(BACKEND)}: {module}"
        for path in sorted(HELPDESK.rglob("*.py"))
        for module in _imported_modules(path)
        if module in deleted or module.startswith("helpdesk.agent_api.agent.nodes.")
    ]
    assert offenders == []


def test_langgraph_packages_are_in_the_agent_api_group() -> None:
    """Catches a graph that imports `langgraph` while the packages are missing
    from `pyproject.toml` (a fresh install would fail on import), one that puts
    them anywhere but the `agent-api` group, a missing `psycopg`, or a lock file
    that does not hold them.
    """
    project: dict[str, Any] = tomllib.loads((BACKEND / "pyproject.toml").read_text())
    group: list[str] = [
        item for item in project["dependency-groups"]["agent-api"] if isinstance(item, str)
    ]
    wanted = ("langgraph", "langgraph-checkpoint-postgres", "psycopg")
    for name in wanted:
        assert any(re.match(rf"{re.escape(name)}(\[|[<>=!~ ]|$)", item) for item in group), name

    elsewhere: list[str] = list(project["project"].get("dependencies", []))
    for extra in project["project"].get("optional-dependencies", {}).values():
        elsewhere.extend(extra)
    for other, items in project["dependency-groups"].items():
        if other != "agent-api":
            elsewhere.extend(item for item in items if isinstance(item, str))
    assert [name for name in elsewhere if name.lower().startswith("langgraph")] == []

    lock: dict[str, Any] = tomllib.loads((BACKEND / "uv.lock").read_text())
    locked = {package["name"] for package in lock["package"]}
    assert {"langgraph", "langgraph-checkpoint-postgres", "psycopg"} <= locked


def test_engine_setting_is_gone() -> None:
    """Catches a switch-over that leaves `CHAT_ENGINE` and the settings only the
    pipeline read (the intent confidence floor) in the settings class or in the
    committed env example, where they would look like knobs that do something."""
    assert "chat_engine" not in Settings.model_fields
    assert "intent_confidence_min" not in Settings.model_fields
    example = (BACKEND / ".env.example").read_text()
    assert "CHAT_ENGINE" not in example
    assert "INTENT_CONFIDENCE_MIN" not in example


def _scores() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(SCORES.read_text())
    return loaded


def test_engine_scores_have_all_three_engines_in_one_shape() -> None:
    """Catches a scores file with a missing engine (the graph's score must be
    recorded beside the pipeline's and the loop's), a missing field, a case
    count other than the 33 conversation cases, a count of passed cases that
    cannot be (negative or above 33), a score that is not a number, or a commit
    and date that are not a commit hash and a calendar day.

    A hash is matched whole, so a trailing newline, CRLF or space is refused.
    """
    scores = _scores()

    assert sorted(scores) == ["graph", "loop", "pipeline"]
    for engine, record in scores.items():
        assert sorted(record) == ["cases", "cases_passed", "commit", "date", "score"], engine
        assert record["cases"] == CONVERSATION_CASES, engine
        assert isinstance(record["cases_passed"], int), engine
        assert not isinstance(record["cases_passed"], bool), engine
        assert 0 <= record["cases_passed"] <= CONVERSATION_CASES, engine
        assert isinstance(record["score"], int | float), engine
        assert not isinstance(record["score"], bool), engine
        assert isinstance(record["commit"], str), engine
        assert re.fullmatch(r"[0-9a-f]{7,40}", record["commit"]), engine
        assert isinstance(record["date"], str), engine
        assert date.fromisoformat(record["date"]).isoformat() == record["date"], engine


def test_engine_scores_show_the_loop_ahead() -> None:
    """Catches a switch-over that lands although the loop did not score higher
    than the old pipeline, and one that lands on a tie (a tie is not ahead).

    The scores are the console's, recorded by the session; this reads the
    recorded numbers and checks only the order.
    """
    scores = _scores()

    assert scores["loop"]["score"] > scores["pipeline"]["score"]
