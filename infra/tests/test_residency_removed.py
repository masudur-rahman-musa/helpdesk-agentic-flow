"""Residency, the gateway region header and any local chat model are gone.

What is pinned: no tracked file outside the excluded paths names
residency, the region header or field, or a local chat model, and no code or
doc file there holds the status 451; and both gateway calls still send
`X-Data-Class` (the gateway uses it to decide redaction).

The scan is a function that takes a root and a list of files, so the
planted-line case can feed it a fake tree. Pure filesystem and `git ls-files`:
no service, no network, no import of application code.
"""

import re
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest


def _repo_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / ".git").exists():
            return candidate
    raise RuntimeError("could not locate the repo root (.git) above this test file")


REPO_ROOT = _repo_root()

# Tests may name the old things (they check they are gone); the policy
# documents are content the agent searches; migrations and the lock file are
# frozen records.
EXCLUDED_PREFIXES = (
    "backend/tests/",
    "infra/tests/",
    "backend/data/policies/",
    "backend/migrations/versions/",
    "backend/uv.lock",
)

NAME_PATTERN = re.compile(r"residen|X-Gateway-Region|gateway_region|ollama|llama", re.IGNORECASE)
STATUS_PATTERN = re.compile(r"\b451\b")
STATUS_SUFFIXES = (".py", ".ts", ".tsx", ".md")


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, check=True
    ).stdout
    return [name for name in out.decode("utf-8").split("\0") if name]


def scan_for_residency(root: Path, files: Sequence[str]) -> list[tuple[str, int]]:
    """Return (file, line number) for every live line under `root` that names one."""
    found: list[tuple[str, int]] = []
    for name in files:
        if name.startswith(EXCLUDED_PREFIXES):
            continue
        path = root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        check_status = name.endswith(STATUS_SUFFIXES)
        for number, line in enumerate(text.splitlines(), start=1):
            if NAME_PATTERN.search(line) or (check_status and STATUS_PATTERN.search(line)):
                found.append((name, number))
    return found


def test_no_live_file_names_residency_a_region_header_or_a_local_chat_model() -> None:
    """Catches a doc, comment, config, template or source line that still names
    residency, the region header or field, a local chat model, or the 451 the
    gateway no longer returns.

    The scan reads every tracked file outside the excluded paths, so a stale
    mention anywhere live is reported with its file and line.
    """
    assert scan_for_residency(REPO_ROOT, _tracked_files()) == []


def test_the_name_scan_reports_a_planted_line(tmp_path: Path) -> None:
    """Catches a scan that reports nothing (so the live-file check above could
    never fail), one that skips the 451 rule, and one that matches `4510`.

    A fake tree holds a file with `residency="eu"` on line 2, a file with
    `status 451`, and a file with `4510`; an excluded file holding the same
    line must not be reported.
    """
    (tmp_path / "a.py").write_text('x = 1\nresidency="eu"\n', encoding="utf-8")
    (tmp_path / "b.md").write_text("ok\nok\nok\nstatus 451\n", encoding="utf-8")
    (tmp_path / "c.md").write_text("code 4510\n", encoding="utf-8")
    (tmp_path / "backend" / "tests").mkdir(parents=True)
    (tmp_path / "backend" / "tests" / "t.py").write_text('residency="eu"\n', encoding="utf-8")

    found = scan_for_residency(tmp_path, ["a.py", "b.md", "c.md", "backend/tests/t.py"])

    assert found == [("a.py", 2), ("b.md", 4)]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        pytest.param(
            "backend/helpdesk/agent_api/services/model_gateway/client.py",
            "confidential",
            id="chat",
        ),
        pytest.param(
            "backend/helpdesk/mcp_server/services/rag/redaction_client.py",
            "internal",
            id="redaction",
        ),
    ],
)
def test_both_gateway_calls_still_send_a_data_class(path: str, value: str) -> None:
    """Catches a cleanup that removes `X-Data-Class` along with the region
    header (the gateway would then redact by its default), and one that swaps
    the two classes.

    Each file must set the header from its own `DATA_CLASS` constant, and the
    constant must hold the exact value.
    """
    source = (REPO_ROOT / path).read_text(encoding="utf-8")
    assert re.search(rf'^DATA_CLASS\s*(:\s*\w+\s*)?=\s*"{value}"\s*$', source, re.MULTILINE)
    assert re.search(r'"X-Data-Class"\s*:\s*DATA_CLASS', source)
