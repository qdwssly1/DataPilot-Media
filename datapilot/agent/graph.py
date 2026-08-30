"""DataPilot graph skeleton with a real Planner node.

The Planner node is executable. SQL Agent, Reviewer, and Analyst remain clear
stubs and the graph stops before any SQL generation or execution.
"""

from __future__ import annotations

from dataclasses import dataclass

from datapilot.agent.planner import Planner, PlannerResult
from datapilot.agent.state import AgentState
from datapilot.tracing.trace import TraceCollector

GRAPH_NODES = ("start", "planner", "sql_agent", "reviewer", "analyst", "end")
GRAPH_EDGES = tuple(zip(GRAPH_NODES[:-1], GRAPH_NODES[1:], strict=True))


@dataclass(frozen=True, slots=True)
class GraphSkeleton:
    """Execute planning, then stop at the unimplemented SQL Agent boundary."""

    planner: Planner
    nodes: tuple[str, ...] = GRAPH_NODES
    edges: tuple[tuple[str, str], ...] = GRAPH_EDGES

    def run_planner(
        self,
        state: AgentState,
        *,
        trace: TraceCollector,
    ) -> PlannerResult:
        """Run the real Planner node and return its structured result."""

        return self.planner.plan(state, trace=trace)

    def run(self, state: AgentState, *, trace: TraceCollector) -> AgentState:
        """Run Planner and reject transition into the future SQL Agent."""

        self.run_planner(state, trace=trace)
        raise NotImplementedError("DataPilot SQL Agent is not implemented yet")


def build_graph(planner: Planner) -> GraphSkeleton:
    """Inject the Planner into the explicit Phase 3 graph structure."""

    return GraphSkeleton(planner=planner)
