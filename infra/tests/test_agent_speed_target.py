"""The `agent-speed` Makefile target runs the load smoke, then the reader.

Pure text and filesystem: the Makefile is read as lines, nothing is run.
"""

from pathlib import Path


def _repo_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / ".git").exists():
            return candidate
    raise RuntimeError("could not locate the repo root (.git) above this test file")


REPO_ROOT = _repo_root()


def _target(lines: list[str]) -> tuple[str, list[str]]:
    """The target line and the tab-indented recipe lines under it."""
    found = [i for i, line in enumerate(lines) if line.startswith("agent-speed:")]
    assert found, "the Makefile has no agent-speed target"
    start = found[0]
    recipe: list[str] = []
    for line in lines[start + 1 :]:
        if not line.startswith("\t"):
            break
        recipe.append(line)
    return lines[start], recipe


def test_the_agent_speed_target_runs_the_load_smoke_then_the_reader() -> None:
    """Catches a target that is missing from `.PHONY` (a file of that name
    would block it), has no help line, runs the reader before the load (it
    would read the last run's spans) or never runs the load at all, passes the
    reader no label or start time, or points at a script that does not exist."""
    lines = (REPO_ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
    phony = next(line for line in lines if line.startswith(".PHONY:"))
    head, recipe = _target(lines)
    prerequisites = head.split("##")[0].removeprefix("agent-speed:")
    reader_at = next(i for i, line in enumerate(recipe) if "scripts/agent_speed.py" in line)
    load_in_recipe = [i for i, line in enumerate(recipe) if "load-smoke" in line]

    assert "agent-speed" in phony.removeprefix(".PHONY:").split()
    assert "##" in head
    assert head.split("##")[1].strip() != ""
    assert "load-smoke" in prerequisites.split() or any(i < reader_at for i in load_in_recipe)
    assert "--label" in recipe[reader_at]
    assert "--since" in recipe[reader_at]
    assert (REPO_ROOT / "backend" / "scripts" / "agent_speed.py").is_file()
