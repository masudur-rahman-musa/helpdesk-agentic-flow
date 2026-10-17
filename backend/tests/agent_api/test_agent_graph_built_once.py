"""An earlier change: graph cached by checkpointer, and agent imports nothing from the API layer.

Three tests, no database, no graph execution, no I/O beyond AST parsing.
"""

import ast
from pathlib import Path

import helpdesk.agent_api.agent as agent_pkg
from helpdesk.agent_api.agent.graph import build_graph
from langgraph.checkpoint.memory import InMemorySaver


def test_one_checkpointer_compiles_the_graph_once() -> None:
    """Catches an implementation that compiles a fresh graph on every
    `build_graph` call instead of returning the already-compiled one for a
    checkpointer it has already seen.
    """
    saver = InMemorySaver()
    first = build_graph(saver)
    second = build_graph(saver)
    assert first is second


def test_a_different_checkpointer_gets_its_own_graph() -> None:
    """Catches a cache that shares ONE compiled graph across all checkpointers
    instead of keeping a separate compiled graph per saver; also catches a cache
    that binds the wrong saver to a graph slot.
    """
    saver1 = InMemorySaver()
    saver2 = InMemorySaver()
    graph1 = build_graph(saver1)
    graph2 = build_graph(saver2)
    assert graph1 is not graph2
    assert graph1.checkpointer is saver1
    assert graph2.checkpointer is saver2


def test_the_agent_package_imports_nothing_from_the_api_layer() -> None:
    """Catches a module under `agent/` that imports from `helpdesk.agent_api.api`,
    which would mean the agent layer depends on the HTTP edge rather than the
    other way round.
    """
    agent_dir = Path(str(agent_pkg.__file__)).parent
    bad: list[str] = []
    for path in sorted(agent_dir.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                if node.module.startswith("helpdesk.agent_api.api"):
                    bad.append(f"{path.name}: {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("helpdesk.agent_api.api"):
                        bad.append(f"{path.name}: {alias.name}")
    assert bad == [], "agent modules import from the api layer:\n" + "\n".join(bad)
