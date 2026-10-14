"""Print the test files for one CI slice, balanced by measured time.

CI runs the suite as several parallel jobs, each on its own runner with its own
Postgres, Redis and Qdrant. This script decides which files each job gets:
largest first, each to the slice with the least time so far, using the per-file
seconds in `scripts/test_durations.json`. A file missing from that table (a new
test file) counts as the median, so it still lands somewhere. Every file under
`backend/pyproject.toml`'s `testpaths` is in exactly one slice, which
`backend/tests/shared/test_test_shard.py` pins.

    uv run python scripts/test_shard.py --shard 0 --of 3

Refresh the table when the slices drift apart:

    uv run pytest --no-cov -q --durations=0 --durations-min=0 > durations.txt
"""

from __future__ import annotations

import argparse
import json
import statistics
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
# The Python project and its test tree live under `backend/`, so testpaths and
# the globbed files are resolved there. The duration table stays with the
# scripts at the repo root.
BACKEND_ROOT = REPO_ROOT / "backend"
DURATIONS = REPO_ROOT / "scripts" / "test_durations.json"


def test_files() -> list[str]:
    """Every `test_*.py` under the configured testpaths, repo-relative and sorted."""
    config = tomllib.loads((BACKEND_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    paths = config["tool"]["pytest"]["ini_options"]["testpaths"]
    return sorted(
        str(path.relative_to(BACKEND_ROOT))
        for root in paths
        for path in (BACKEND_ROOT / root).rglob("test_*.py")
    )


def slices(files: list[str], durations: dict[str, float], count: int) -> list[list[str]]:
    """Split `files` into `count` lists whose summed durations are close."""
    fallback = statistics.median(durations.values()) if durations else 1.0
    weight = {name: durations.get(name, fallback) for name in files}
    totals = [0.0] * count
    result: list[list[str]] = [[] for _ in range(count)]
    for name in sorted(files, key=lambda item: (-weight[item], item)):
        lightest = totals.index(min(totals))
        result[lightest].append(name)
        totals[lightest] += weight[name]
    return [sorted(chunk) for chunk in result]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--shard", type=int, required=True)
    parser.add_argument("--of", type=int, required=True, dest="count")
    arguments = parser.parse_args()
    if not 0 <= arguments.shard < arguments.count:
        parser.error(f"--shard must be in 0..{arguments.count - 1}")
    durations = json.loads(DURATIONS.read_text(encoding="utf-8"))
    print(" ".join(slices(test_files(), durations, arguments.count)[arguments.shard]))


if __name__ == "__main__":
    main()
