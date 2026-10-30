"""Every step the e2e reset runs points at code that exists.

The reset shells out, so a step naming a script that was moved or deleted
only fails when someone runs `make e2e` or `make eval-chat`. These cases
check each step's target without running it: no database, no gateway.
"""

import importlib.util

import pytest

from helpdesk.mcp_server.cli import _parse
from scripts import e2e_reset

TENANTS = ("nordlicht", "cuan")
"""The reset's default tenants."""


@pytest.mark.parametrize("command", e2e_reset.ingest_commands(TENANTS), ids=TENANTS)
def test_each_ingest_step_runs_the_ingest_command_on_a_real_corpus(command: list[str]) -> None:
    """Catches an ingest step naming a script or module that does not exist,
    a flag the ingest command does not take, or a corpus folder that is not
    there."""
    module = command[command.index("-m") + 1]
    assert importlib.util.find_spec(module) is not None, f"no module {module}"

    args = _parse(command[command.index("-m") + 2 :])

    assert args.corpus_root is not None
    assert (args.corpus_root / args.tenant / args.version).is_dir()
    assert any((args.corpus_root / args.tenant / args.version).glob("*.md"))


def test_the_seed_step_and_the_stub_gateway_exist() -> None:
    assert (e2e_reset._BACKEND_SCRIPTS_DIR / "seed.py").is_file()
    assert e2e_reset.STUB_GATEWAY.is_file()
