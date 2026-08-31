"""DataPilot graph with real Planner and SQL Agent nodes.

Query tasks execute sequentially through SQL Agent. Reviewer and Analyst remain
explicitly unimplemented, so the graph never invents a final answer.
"""

from __future__ import annotations

from dataclasses import dataclass

from datapilot.agent.planner import Planner, PlannerResult
from datapilot.agent.sql_agent import SQLAgent, is_task_ready
from datapilot.agent.state import AgentState, SQLResult
from datapilot.tracing.trace import TraceCollector

GRAPH_NODES = ("start", "planner", "sql_agent", "reviewer", "analyst", "end")
GRAPH_EDGES = tuple(zip(GRAPH_NODES[:-1], GRAPH_NODES[1:], strict=True))


def execute_ready_query_tasks(
    state: AgentState,
    sql_agent: SQLAgent,
    *,
    trace: TraceCollector,
) -> tuple[SQLResult, ...]:
    """Execute ready query tasks sequentially and stop on first failure."""

    results: list[SQLResult] = []
    while True:
        task = next(
            (
                item
                for item in state["pending_tasks"]
                if is_task_ready(state, item)
            ),
            None,
        )
        if task is None:
            break
        result = sql_agent.execute_task(state, task, trace=trace)
        results.append(result)
        if not result.success:
            break
    return tuple(results)


@dataclass(frozen=True, slots=True)
class GraphSkeleton:
    """Execute planning and ready queries, then stop before review."""

    planner: Planner
    sql_agent: SQLAgent
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

    def run_sql_tasks(
        self,
        state: AgentState,
        *,
        trace: TraceCollector,
    ) -> tuple[SQLResult, ...]:
        """Execute ready query tasks sequentially and stop on first failure."""

        return execute_ready_query_tasks(state, self.sql_agent, trace=trace)

    def run(self, state: AgentState, *, trace: TraceCollector) -> AgentState:
        """Run implemented nodes and stop at the future review boundary."""

        self.run_planner(state, trace=trace)
        results = self.run_sql_tasks(state, trace=trace)
        if any(not result.success for result in results):
            return state
        raise NotImplementedError(
            "DataPilot Reviewer and Analyst are not implemented yet"
        )


def build_graph(planner: Planner, sql_agent: SQLAgent) -> GraphSkeleton:
    """Inject the implemented Phase 4 nodes into the explicit graph."""

    return GraphSkeleton(planner=planner, sql_agent=sql_agent)
