"""Non-executable DataPilot graph skeleton.

The node names and edges reserve a stable shape for later phases. No node
pretends to plan, generate SQL, review results, or analyze an answer yet.
"""

from __future__ import annotations

from dataclasses import dataclass

from datapilot.agent.state import AgentState

GRAPH_NODES = ("start", "planner", "sql_agent", "reviewer", "analyst", "end")
GRAPH_EDGES = tuple(zip(GRAPH_NODES[:-1], GRAPH_NODES[1:], strict=True))


@dataclass(frozen=True, slots=True)
class GraphSkeleton:
    """Declarative placeholder for the future LangGraph application."""

    nodes: tuple[str, ...] = GRAPH_NODES
    edges: tuple[tuple[str, str], ...] = GRAPH_EDGES

    def run(self, state: AgentState) -> AgentState:
        """Reject execution until real graph nodes are implemented."""

        raise NotImplementedError("DataPilot agent workflow is not implemented yet")


def build_graph() -> GraphSkeleton:
    """Return the explicit, non-executable Phase 2 graph structure."""

    return GraphSkeleton()
