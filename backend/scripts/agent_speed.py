"""Turn one load run's traces into the speed results file.

Run by `make agent-speed` right after the load smoke test. It reads the spans
the run filed in Langfuse (the whole turn, each model step, each tool call),
works out p50 and p95 for each, and stores them under a label, `before` or
`after`, in `evals/agent-speed.json`. The other label is left as it was.

With `--spans-file` it reads the agent's own local span log (`SPAN_LOG_FILE`) instead,
which needs no Langfuse. Langfuse files spans a few seconds late, so the reader reads
again until the number of turns stops growing. Percentiles are nearest rank. The run is meant
for a quiet stack: other turns inside the window would be counted too.
"""

import argparse
import json
import math
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

TURN_SPAN = "chat_turn"
MODEL_STEP_SPAN = "model_step"
TOOL_PREFIX = "tool:"
LABELS = ("before", "after")
USERS = 20
RUN_TIME = "5m"
MAX_READS = 6
WAIT_SECONDS = 10
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "evals" / "agent-speed.json"
PAGE_SIZE = 100


@dataclass(frozen=True)
class SpanRecord:
    """The only things the reader needs to know about a span."""

    name: str
    started_at: datetime
    duration_ms: float


class SpanSource(Protocol):
    def read(self) -> Sequence[SpanRecord]: ...


def percentile(values: Sequence[float], pct: int) -> float:
    """Nearest rank: the value at rank ceil(pct / 100 * n) of the sorted values."""
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[rank - 1]


def _row(durations: Sequence[float]) -> dict[str, float]:
    if not durations:
        return {"count": 0, "p50_ms": 0, "p95_ms": 0}
    return {
        "count": len(durations),
        "p50_ms": percentile(durations, 50),
        "p95_ms": percentile(durations, 95),
    }


def summarise(spans: Sequence[SpanRecord], since: datetime) -> dict[str, Any]:
    """Counts and percentiles of the spans that started at or after `since`."""
    turns: list[float] = []
    steps: list[float] = []
    tools: dict[str, list[float]] = {}
    for item in spans:
        if item.started_at < since:
            continue
        if item.name == TURN_SPAN:
            turns.append(item.duration_ms)
        elif item.name == MODEL_STEP_SPAN:
            steps.append(item.duration_ms)
        elif item.name.startswith(TOOL_PREFIX):
            tools.setdefault(item.name.removeprefix(TOOL_PREFIX), []).append(item.duration_ms)
    return {
        "turn": _row(turns),
        "model_step": _row(steps),
        "tools": {name: _row(durations) for name, durations in tools.items()},
    }


def _turn_count(spans: Sequence[SpanRecord]) -> int:
    return sum(1 for item in spans if item.name == TURN_SPAN)


def read_until_stable(
    source: SpanSource,
    sleep: Callable[[float], None],
    max_reads: int = MAX_READS,
    wait: float = WAIT_SECONDS,
) -> Sequence[SpanRecord]:
    """Read, and read again while the turn count grows, at most `max_reads` times.

    A first read of no turns is not yet stable. Returns the last read.
    """
    spans = source.read()
    reads = 1
    seen = _turn_count(spans)
    while reads < max_reads:
        sleep(wait)
        spans = source.read()
        reads += 1
        count = _turn_count(spans)
        grown = count > seen
        seen = count
        if not grown and count > 0:
            break
    return spans


def write_entry(
    path: Path, label: str, entry: Mapping[str, Any], *, measured_at: str, commit: str
) -> None:
    """Store `entry` under `label`, keeping the other label as it was."""
    if label not in LABELS:
        raise ValueError(f"label must be one of {LABELS}, not {label!r}")
    stored: dict[str, Any] = {}
    if path.exists():
        stored = json.loads(path.read_text(encoding="utf-8"))
    stored[label] = {
        "measured_at": measured_at,
        "commit": commit,
        "users": USERS,
        "run_time": RUN_TIME,
        **entry,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stored, indent=2) + "\n", encoding="utf-8")


class FileSource:
    """Reads the spans the agent appended to its local span log."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self) -> Sequence[SpanRecord]:
        records: list[SpanRecord] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            started = datetime.fromisoformat(item["started_at"])
            records.append(SpanRecord(item["name"], started, float(item["duration_ms"])))
        return records


class LangfuseSource:  # pragma: no cover - reads the real Langfuse
    """Reads the run's spans from Langfuse through the SDK."""

    def __init__(self, *, public_key: str, secret_key: str, host: str, since: datetime) -> None:
        from langfuse import Langfuse

        self._client = Langfuse(public_key=public_key, secret_key=secret_key, host=host)
        self._since = since

    def read(self) -> Sequence[SpanRecord]:
        records: list[SpanRecord] = []
        for name_filter in (TURN_SPAN, MODEL_STEP_SPAN, None):
            page = 1
            while True:
                views = self._client.api.legacy.observations_v1.get_many(
                    page=page, limit=PAGE_SIZE, name=name_filter, from_start_time=self._since
                )
                for item in views.data:
                    name = item.name or ""
                    if name_filter is None and not name.startswith(TOOL_PREFIX):
                        continue
                    if item.end_time is None:
                        continue
                    started = item.start_time
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=UTC)
                    ended = item.end_time
                    if ended.tzinfo is None:
                        ended = ended.replace(tzinfo=UTC)
                    millis = (ended - started).total_seconds() * 1000
                    records.append(SpanRecord(name, started, millis))
                if page >= views.meta.total_pages:
                    break
                page += 1
        return records


def _git_head() -> str:  # pragma: no cover - needs a git checkout
    out = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def main(
    argv: Sequence[str],
    *,
    source: SpanSource | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    commit: Callable[[], str] = _git_head,
    env: Mapping[str, str] = os.environ,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True, choices=LABELS)
    parser.add_argument("--since", required=True, type=datetime.fromisoformat)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--spans-file", type=Path, help="read the agent's local span log instead of Langfuse"
    )
    args = parser.parse_args(argv)
    if source is None and args.spans_file is not None:
        source = FileSource(args.spans_file)
    if source is None or args.spans_file is None:
        for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
            if not env.get(key):
                print(f"{key} is missing; put it in backend/.env", file=sys.stderr)
                return 2
    if source is None:  # pragma: no cover - real Langfuse
        source = LangfuseSource(
            public_key=env["LANGFUSE_PUBLIC_KEY"],
            secret_key=env["LANGFUSE_SECRET_KEY"],
            host=env.get("LANGFUSE_HOST") or "https://cloud.langfuse.com",
            since=args.since,
        )
    spans = read_until_stable(source, sleep)
    entry = summarise(spans, args.since)
    if entry["turn"]["count"] == 0:
        print("no chat turns found after the start time; nothing written", file=sys.stderr)
        return 1
    write_entry(args.out, args.label, entry, measured_at=now().isoformat(), commit=commit())
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
