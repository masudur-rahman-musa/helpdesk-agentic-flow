"""The per-tenant golden sets and the `make eval` recipe.

Written before the implementation exists. Surface under test:

``dev/scripts/eval_sets.py``, importable as ``dev.scripts.eval_sets`` (the
repo root is on ``sys.path`` through ``pyproject.toml``'s pytest
``pythonpath``):
    ``write_tenant_sets(golden: Path, out_dir: Path) -> dict[str, Path]``
    reads the golden file and writes one file per tenant into ``out_dir``
    (made when missing), each holding that tenant's cases, one per line, byte
    for byte as they were read; returns each tenant's slug mapped to its file.

``Makefile``: the ``eval`` recipe restores the database with
``dev/scripts/e2e_reset.py`` and the seed, writes the per-tenant sets into
``backend/evals/reports/``, runs ``ops-eval run`` from ``$(CONSOLE_DIR)``
(default ``../ai-operations-console``) against ``$(AGENT_API_URL)``, and gates
each report with ``ops-eval gate`` against the console's own baseline. This
repo holds no baseline, and ``eval`` stays out of ``make verify``.

No backing service and no network: the split reads the delivered file, and the
Makefile case reads text.
"""

import json
import re
from collections import Counter
from pathlib import Path

from dev.scripts.eval_sets import write_tenant_sets


def _repo_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / ".git").exists():
            return candidate
    raise RuntimeError("could not locate the repo root (.git) above this test file")


ROOT = _repo_root()
GOLDEN = ROOT / "backend" / "evals" / "golden" / "helpdesk-v1.jsonl"
MAKEFILE = ROOT / "Makefile"
TOTAL_CASES = 159
TENANTS = ("nordlicht", "cuan")


def test_the_set_splits_into_one_file_per_tenant(tmp_path: Path) -> None:
    """Catches a split that drops a case, writes one twice, files it under the
    wrong tenant, or rewrites the line (re-serialising would reorder or
    reformat keys, and the console loads the line as written).

    Every case lands in its own tenant's file exactly once and the counts add up
    to 159, the size of the golden set. Each line is compared to the source
    line byte for byte, so `args_subset` and every message survive untouched.
    The tenants are the two seeded ones, no more and no fewer.
    """
    source_lines = GOLDEN.read_text(encoding="utf-8").splitlines()
    assert len(source_lines) == TOTAL_CASES, "premise: the golden set holds 159 cases"
    out_dir = tmp_path / "reports"

    files = write_tenant_sets(GOLDEN, out_dir)

    assert set(files) == set(TENANTS)
    written: Counter[str] = Counter()
    per_tenant: dict[str, int] = {}
    for tenant, path in files.items():
        assert path.parent == out_dir
        lines = path.read_text(encoding="utf-8").splitlines()
        per_tenant[tenant] = len(lines)
        for line in lines:
            assert json.loads(line)["tenant"] == tenant
            written[line] += 1
    assert sum(per_tenant.values()) == TOTAL_CASES
    assert all(count > 0 for count in per_tenant.values())
    assert written == Counter(source_lines), "every line once, unchanged"


def test_a_tenant_with_no_cases_gets_no_file(tmp_path: Path) -> None:
    """Catches a split that is hard-wired to two tenants: a golden file with one
    tenant writes one file, named by the tenant it holds, never an empty file
    for a tenant with nothing (the console would gate an empty set).

    A made-up two-line set in `tmp_path`, so it does not depend on the delivered
    file.
    """
    golden = tmp_path / "small.jsonl"
    lines = [json.dumps({"id": f"aut-00{n}", "tenant": "cuan"}) for n in (1, 2)]
    golden.write_text("\n".join(lines) + "\n", encoding="utf-8")

    files = write_tenant_sets(golden, tmp_path / "out")

    assert set(files) == {"cuan"}
    assert files["cuan"].read_text(encoding="utf-8").splitlines() == lines


# --- the make recipe ----------------------------------------------------------


def _eval_recipe() -> str:
    """The `eval` recipe, backslash continuations joined, as one text."""
    lines = MAKEFILE.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if re.match(r"^eval:", line))
    recipe: list[str] = []
    for line in lines[start + 1 :]:
        if line and not line.startswith(("\t", " ", "#")):
            break
        recipe.append(line)
    return re.sub(r"\\\n\s*", " ", "\n".join(recipe))


def test_make_eval_runs_the_console_command_and_gate() -> None:
    """Catches a recipe that is still the placeholder `echo`, one that runs the
    gate against a baseline held in this repo (the console's is the only
    one), one that runs the eval on a database it did not restore, and one
    that is wired into `make verify` (it needs the real gateway and costs money).

    The recipe must name `ops-eval run` and `ops-eval gate`, `e2e_reset.py`
    and the seed, in that order; run from `$(CONSOLE_DIR)` on the same line
    as `ops-eval run`; point at `$(AGENT_API_URL)`; cover both tenants; write
    to `backend/evals/reports`; and name `baseline.json` only as
    `$(CONSOLE_DIR)/backend/evals/baseline.json`.
    """
    makefile = MAKEFILE.read_text(encoding="utf-8")
    recipe = _eval_recipe()

    for needle in ("ops-eval run", "ops-eval gate", "e2e_reset.py", "seed.py"):
        assert needle in recipe, f"the eval recipe never names {needle!r}"
    assert (
        recipe.index("e2e_reset.py")
        < recipe.index("seed.py")
        < recipe.index("ops-eval run")
        < recipe.index("ops-eval gate")
    ), "restore, seed, run, then gate"
    assert any(
        "cd $(CONSOLE_DIR)" in line and "ops-eval run" in line for line in recipe.splitlines()
    ), "ops-eval run starts from the console's folder"
    assert "--target $(AGENT_API_URL)" in recipe
    for tenant in TENANTS:
        assert tenant in recipe, f"the eval recipe never names the {tenant} set"
    assert "backend/evals/reports" in recipe
    assert re.search(r"^CONSOLE_DIR\s*\?=\s*\.\./ai-operations-console\s*$", makefile, re.MULTILINE)

    assert "$(CONSOLE_DIR)/backend/evals/baseline.json" in recipe
    for match in re.finditer(r"baseline\.json", recipe):
        assert recipe[: match.start()].endswith("$(CONSOLE_DIR)/backend/evals/"), (
            "baseline.json is named only through the console's folder"
        )

    verify = next(line for line in makefile.splitlines() if re.match(r"^verify:", line)).split(
        "##"
    )[0]
    assert "eval" not in verify.split(":", 1)[1].split()


def test_this_repo_holds_no_baseline_file() -> None:
    """Catches a baseline left behind: `backend/evals/baseline.json` is deleted
    in this plan, because the console's baseline is the only baseline. A copy
    here would be gated against by nobody and drift."""
    assert not (ROOT / "backend" / "evals" / "baseline.json").exists()
