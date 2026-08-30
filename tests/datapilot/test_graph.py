from __future__ import annotations

import pytest

from datapilot.agent.graph import build_graph
from datapilot.agent.state import create_initial_state


def test_graph_skeleton_is_explicit_and_not_executable() -> None:
    graph = build_graph()

    assert graph.nodes == (
        "start",
        "planner",
        "sql_agent",
        "reviewer",
        "analyst",
        "end",
    )
    assert graph.edges == (
        ("start", "planner"),
        ("planner", "sql_agent"),
        ("sql_agent", "reviewer"),
        ("reviewer", "analyst"),
        ("analyst", "end"),
    )

    with pytest.raises(NotImplementedError, match="not implemented yet"):
        graph.run(create_initial_state("show sales"))
