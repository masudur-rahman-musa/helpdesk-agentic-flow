"""The reader that turns one load run's spans into the speed results file.

Expected implementation surface, `backend/scripts/agent_speed.py` (imported as
`scripts.agent_speed`):

``SpanRecord``
    frozen dataclass ``(name: str, started_at: datetime, duration_ms: float)``,
    the only thing the reader knows about a span. ``started_at`` is timezone
    aware.
``percentile(values: Sequence[float], pct: int) -> float``
    nearest rank: sort, take the value at rank ``ceil(pct / 100 * n)`` (1-based).
    No averaging between two values.
``summarise(spans: Sequence[SpanRecord], since: datetime) -> dict[str, Any]``
    counts only spans with ``started_at >= since``. Returns ``turn`` (the
    ``chat_turn`` spans), ``model_step`` (the ``model_step`` spans) and
    ``tools`` (one row per ``tool:<name>`` span name, keyed by ``<name>``), each
    row ``{"count", "p50_ms", "p95_ms"}``.
``read_until_stable(source, sleep, max_reads=6, wait=10) -> list[SpanRecord]``
    ``source.read()`` returns the spans filed so far. Read, and while the number
    of ``chat_turn`` spans grew since the last read (a first read of zero turns
    counts as not yet stable), call ``sleep(wait)`` and read again, at most
    ``max_reads`` reads. No wait after the last read. Returns the last read.
``write_entry(path, label, entry, *, measured_at, commit) -> None``
    ``label`` must be exactly ``before`` or ``after`` (else ``ValueError``,
    nothing written). The file is a JSON object keyed by label; the other
    label's entry is kept as it was. The stored entry is ``entry`` plus
    ``measured_at``, ``commit``, ``users`` 20 and ``run_time`` "5m".
``main(argv, *, source=None, sleep=time.sleep, now=<utc now>, commit=<git head>,
env=os.environ) -> int``
    ``--label``, ``--since`` (ISO 8601 with offset) and ``--out``. Needs
    ``LANGFUSE_PUBLIC_KEY`` and ``LANGFUSE_SECRET_KEY`` in ``env``; when one is
    empty or missing it prints a message naming it to stderr and returns
    non-zero before the source is read. ``source=None`` builds the Langfuse
    reader; tests pass a fake. Returns 0 after writing; non-zero (an int, or
    argparse's ``SystemExit``) and nothing written for a bad label or when no
    ``chat_turn`` span is found after all reads. ``measured_at`` is
    ``now().isoformat()``.

No network, no Langfuse, no real waiting: a fake source and a recording sleep.
"""

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.agent_speed import (
    SpanRecord,
    main,
    percentile,
    read_until_stable,
    summarise,
    write_entry,
)

SINCE = datetime(2026, 10, 31, 9, 0, 0, tzinfo=UTC)
KEYS: Mapping[str, str] = {
    "LANGFUSE_PUBLIC_KEY": "pk-fake-not-a-key",
    "LANGFUSE_SECRET_KEY": "sk-fake-not-a-key",
}


def rec(name: str, ms: float = 10, *, at: datetime = SINCE) -> SpanRecord:
    return SpanRecord(name=name, started_at=at, duration_ms=ms)


class FakeSource:
    """Plays one scripted list of spans per read; the last list repeats."""

    def __init__(self, *reads: Sequence[SpanRecord]) -> None:
        self._reads = list(reads)
        self.reads = 0

    def read(self) -> Sequence[SpanRecord]:
        spans = self._reads[min(self.reads, len(self._reads) - 1)]
        self.reads += 1
        return spans


def turns(n: int) -> list[SpanRecord]:
    return [rec("chat_turn") for _ in range(n)]


def growing_source() -> FakeSource:
    return FakeSource(*[turns(n) for n in (1, 2, 3, 4, 5, 6, 7, 8)])


# --- percentiles ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "p50", "p95"),
    [
        pytest.param(list(range(1, 101)), 50, 95, id="one-to-100"),
        pytest.param([7], 7, 7, id="single-value"),
        pytest.param([4, 1, 3, 2], 2, 4, id="unsorted-no-averaging"),
        pytest.param([30, 10, 20], 20, 30, id="three-values"),
    ],
)
def test_percentiles_are_nearest_rank(values: list[float], p50: float, p95: float) -> None:
    """Catches a percentile that averages the two middle values (2.5 for
    1 to 4), interpolates, rounds the rank down instead of up, or forgets to
    sort first."""
    assert percentile(values, 50) == p50
    assert percentile(values, 95) == p95


# --- grouping and the start time ----------------------------------------------------


def test_spans_are_grouped_into_turn_model_step_and_each_tool() -> None:
    """Catches a reader that counts every span in one bucket, keeps the `tool:`
    prefix in the tool names, merges all tools into one row, or counts a span
    of some other name (here `other`)."""
    spans = (
        [rec("chat_turn", 100)] * 3
        + [rec("model_step", 40)] * 5
        + [rec("tool:search_policy", 5)] * 2
        + [rec("tool:list_my_grants", 9)]
        + [rec("other", 1)]
    )

    result = summarise(spans, SINCE)

    assert result["turn"] == {"count": 3, "p50_ms": 100, "p95_ms": 100}
    assert result["model_step"] == {"count": 5, "p50_ms": 40, "p95_ms": 40}
    assert result["tools"] == {
        "search_policy": {"count": 2, "p50_ms": 5, "p95_ms": 5},
        "list_my_grants": {"count": 1, "p50_ms": 9, "p95_ms": 9},
    }


def test_spans_before_the_start_time_are_left_out() -> None:
    """Catches a reader that counts spans of an earlier run (start strictly
    after instead of at-or-after drops the exact-start span; no filter keeps
    the early one). Same rule for every kind of span."""
    one_second_early = SINCE - timedelta(seconds=1)
    spans = [
        rec("chat_turn", 1, at=one_second_early),
        rec("chat_turn", 2, at=SINCE),
        rec("model_step", 1, at=one_second_early),
        rec("tool:search_policy", 1, at=one_second_early),
        rec("tool:search_policy", 3, at=SINCE),
    ]

    result = summarise(spans, SINCE)

    assert result["turn"]["count"] == 1
    assert result["turn"]["p50_ms"] == 2
    assert result["model_step"]["count"] == 0
    assert result["tools"] == {"search_policy": {"count": 1, "p50_ms": 3, "p95_ms": 3}}


# --- reading until the count settles -----------------------------------------------


def test_reads_until_the_turn_count_stops_growing() -> None:
    """Catches a reader that reads once (misses late spans), stops on the first
    equal count too late or too early, waits a different time than 10 seconds,
    or waits after the read it keeps."""
    source = FakeSource(turns(3), turns(5), turns(5))
    waits: list[float] = []

    spans = read_until_stable(source, waits.append)

    assert source.reads == 3
    assert waits == [10, 10]
    assert len(spans) == 5


def test_a_count_that_keeps_growing_is_read_six_times_and_then_used() -> None:
    """Catches a reader that loops forever on a count that never settles, or
    gives up (raises, or returns nothing) at the sixth read instead of using it."""
    source = growing_source()
    waits: list[float] = []

    spans = read_until_stable(source, waits.append)

    assert source.reads == 6
    assert waits == [10] * 5
    assert len(spans) == 6


# --- the command ---------------------------------------------------------------------


def run_main(
    tmp_path: Path,
    label: str,
    source: FakeSource,
    *,
    env: Mapping[str, str] = KEYS,
    waits: list[float] | None = None,
) -> int:
    out = tmp_path / "agent-speed.json"
    commit: Callable[[], str] = lambda: "abc1234"  # noqa: E731
    try:
        code = main(
            ["--label", label, "--since", SINCE.isoformat(), "--out", str(out)],
            source=source,
            sleep=(waits if waits is not None else []).append,
            now=lambda: datetime(2026, 10, 31, 9, 6, 0, tzinfo=UTC),
            commit=commit,
            env=env,
        )
    except SystemExit as stop:
        return int(stop.code) if isinstance(stop.code, int) else 1
    return int(code)


@pytest.mark.parametrize("label", ["Before", "during", "", "before ", "after\n"])
def test_the_label_must_be_before_or_after(tmp_path: Path, label: str) -> None:
    """Catches a reader that takes any label, matches case-insensitively, trims
    spaces or a trailing newline, or lets an empty label write an entry under
    an empty key. Each refused label must also leave the file unmade."""
    source = FakeSource(turns(2))

    code = run_main(tmp_path, label, source)

    assert code != 0
    assert not (tmp_path / "agent-speed.json").exists()


@pytest.mark.parametrize("label", ["before", "after"])
def test_the_two_allowed_labels_are_taken(tmp_path: Path, label: str) -> None:
    """Catches a label check that refuses everything, or one that only knows
    `before`. Also shows the entry lands under the label that was given."""
    code = run_main(tmp_path, label, FakeSource(turns(2)))

    assert code == 0
    stored = json.loads((tmp_path / "agent-speed.json").read_text(encoding="utf-8"))
    assert list(stored) == [label]


def test_writing_one_label_keeps_the_other(tmp_path: Path) -> None:
    """Catches a writer that overwrites the whole file with the new label (the
    saved `after` is lost), changes the kept entry, or leaves out the fixed
    fields of the new one. The span from before the start time must not be
    counted in what is written."""
    out = tmp_path / "agent-speed.json"
    kept = {"measured_at": "2026-01-01T00:00:00+00:00", "commit": "old", "users": 20}
    out.write_text(json.dumps({"after": kept}), encoding="utf-8")
    spans = [
        rec("chat_turn", 10),
        rec("chat_turn", 20),
        rec("chat_turn", 999, at=SINCE - timedelta(seconds=1)),
        rec("model_step", 5),
    ]

    code = run_main(tmp_path, "before", FakeSource(spans))

    stored = json.loads(out.read_text(encoding="utf-8"))
    assert code == 0
    assert stored["after"] == kept
    entry = stored["before"]
    assert entry["measured_at"] == "2026-10-31T09:06:00+00:00"
    assert entry["commit"] == "abc1234"
    assert entry["users"] == 20
    assert entry["run_time"] == "5m"
    assert entry["turn"] == {"count": 2, "p50_ms": 10, "p95_ms": 20}
    assert entry["model_step"] == {"count": 1, "p50_ms": 5, "p95_ms": 5}
    assert entry["tools"] == {}


def test_write_entry_refuses_a_bad_label_and_keeps_the_file(tmp_path: Path) -> None:
    """Catches a writer that trusts its caller: a wrong label reaching it must
    stop there, not add a third key to the results file."""
    out = tmp_path / "agent-speed.json"
    out.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError):
        write_entry(out, "during", {}, measured_at="x", commit="y")

    assert out.read_text(encoding="utf-8") == "{}"


def test_no_turns_found_writes_nothing(tmp_path: Path) -> None:
    """Catches a reader that writes an all-zero entry when Langfuse returned no
    turns (it would pass for a real baseline), or that gives up before the
    sixth read. The model steps in the data are real, so only the missing
    `chat_turn` spans can be what stops it."""
    out = tmp_path / "agent-speed.json"
    original = json.dumps({"after": {"commit": "old"}})
    out.write_text(original, encoding="utf-8")
    source = FakeSource([rec("model_step", 5)])
    waits: list[float] = []

    code = run_main(tmp_path, "before", source, waits=waits)

    assert code != 0
    assert source.reads == 6
    assert out.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    "missing",
    [
        pytest.param("LANGFUSE_PUBLIC_KEY", id="public-key"),
        pytest.param("LANGFUSE_SECRET_KEY", id="secret-key"),
    ],
)
@pytest.mark.parametrize("how", ["empty", "absent"])
def test_missing_langfuse_keys_stop_before_reading(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], missing: str, how: str
) -> None:
    """Catches a reader that asks Langfuse with half a login, checks only one
    of the two keys, accepts an empty key, or fails without saying which key is
    missing. The turns in the source are fine, so only the keys can stop it."""
    env = {key: value for key, value in KEYS.items() if key != missing}
    if how == "empty":
        env[missing] = ""
    source = FakeSource(turns(3))

    code = run_main(tmp_path, "before", source, env=env)

    assert code != 0
    assert source.reads == 0
    assert missing in capsys.readouterr().err
    assert not (tmp_path / "agent-speed.json").exists()


# --- the agent's own span log ---------------------------------------------------------


def test_the_span_log_is_read_instead_of_langfuse_and_needs_no_keys(tmp_path: Path) -> None:
    """Catches a reader that still asks for Langfuse keys when it is given the
    agent's own span log, ignores the file, or drops a line of it."""
    log = tmp_path / "spans.jsonl"
    log.write_text(
        "\n".join(
            json.dumps({"name": n, "started_at": SINCE.isoformat(), "duration_ms": ms})
            for n, ms in (("chat_turn", 30), ("model_step", 10), ("tool:search_policy", 4))
        )
        + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "agent-speed.json"

    code = main(
        [
            "--label",
            "before",
            "--since",
            SINCE.isoformat(),
            "--out",
            str(out),
            "--spans-file",
            str(log),
        ],
        sleep=lambda _seconds: None,
        now=lambda: SINCE,
        commit=lambda: "abc1234",
        env={},
    )

    stored = json.loads(out.read_text(encoding="utf-8"))["before"]
    assert code == 0
    assert stored["turn"]["count"] == 1
    assert stored["model_step"] == {"count": 1, "p50_ms": 10, "p95_ms": 10}
    assert stored["tools"] == {"search_policy": {"count": 1, "p50_ms": 4, "p95_ms": 4}}
