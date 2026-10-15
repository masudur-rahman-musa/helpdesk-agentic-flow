"""`dev/scripts/test_shard.py`: the CI slices cover the suite exactly once, balanced.

A file in no slice is a test CI silently stops running, and a file in two is
run twice and double-counted in the combined coverage; both are invisible on a
green run, so they are pinned here.
"""

from __future__ import annotations

import json

from dev.scripts.test_shard import DURATIONS, slices, test_files

SLICE_COUNT = 3


def test_every_test_file_is_in_exactly_one_slice() -> None:
    files = test_files()
    chunks = slices(files, json.loads(DURATIONS.read_text(encoding="utf-8")), SLICE_COUNT)
    flat = [name for chunk in chunks for name in chunk]
    assert sorted(flat) == files
    assert len(flat) == len(set(flat))


def test_a_file_missing_from_the_table_still_lands_in_a_slice() -> None:
    chunks = slices(["new_a.py", "known.py"], {"known.py": 5.0}, 2)
    assert sorted(name for chunk in chunks for name in chunk) == [
        "known.py",
        "new_a.py",
    ]


def test_the_slices_are_balanced_by_measured_time() -> None:
    durations = {"a.py": 9.0, "b.py": 5.0, "c.py": 4.0, "d.py": 3.0, "e.py": 3.0}
    chunks = slices(sorted(durations), durations, 2)
    totals = sorted(sum(durations[name] for name in chunk) for chunk in chunks)
    assert totals == [12.0, 12.0]


def test_the_shipped_table_names_only_files_that_exist() -> None:
    """A stale entry for a deleted or renamed file is harmless to the split but
    means the table no longer describes the suite; refresh it when this reddens."""
    files = set(test_files())
    stale = sorted(set(json.loads(DURATIONS.read_text(encoding="utf-8"))) - files)
    assert stale == []
