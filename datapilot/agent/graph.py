"""DataPilot graph through reviewed SQL execution.

Query tasks execute and receive semantic review one at a time. Reviewer retry
feedback may produce one corrected SQL version. Analyst remains unimplemented.
"""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Any

from datapilot.agent.planner import Planner, PlannerResult
from datapilot.agent.reviewer import Reviewer, ReviewerRetryLimitError
from datapilot.agent.sql_agent import SQLAgent, is_task_ready
from datapilot.agent.state import AgentState, ReviewerResult, SQLResult, TaskItem
from datapilot.tracing.trace import EventType, TraceCollector

GRAPH_NODES = ("start", "planner", "sql_agent", "reviewer", "analyst", "end")
GRAPH_EDGES = tuple(zip(GRAPH_NODES[:-1], GRAPH_NODES[1:], strict=True))


@dataclass(frozen=True, slots=True)
class ReviewedQueryRun:
    """All SQL versions and semantic decisions produced for one query task."""

    task_id: str
    sql_results: tuple[SQLResult, ...]
    review_results: tuple[ReviewerResult, ...]

    @property
    def approved(self) -> bool:
        return bool(
            self.review_results
            and self.review_results[-1].decision == "approve"
        )


def _add_workflow_event(
    trace: TraceCollector,
    event_type: EventType,
    *,
    task_id: str,
    review_retry_count: int,
    duration: float,
    error_type: str | None = None,
    tool: str | None = None,
) -> None:
    metadata: dict[str, Any] = {
        "task_id": task_id,
        "review_retry_count": review_retry_count,
        "duration": duration,
    }
    if error_type is not None:
        metadata["error_type"] = error_type
    if tool is not None:
        metadata["tool"] = tool
    trace.add_event(
        event_type,
        component="review_workflow",
        action=event_type.value.lower(),
        summary=f"Review workflow event: {event_type.value}.",
        metadata=metadata,
    )


def _mark_task_failed(state: AgentState, task: TaskItem) -> None:
    task.status = "failed"
    state["pending_tasks"] = [
        item for item in state["pending_tasks"] if item.task_id != task.task_id
    ]
    state["current_task"] = task


def _store_verified_sql(
    sql_agent: SQLAgent,
    task: TaskItem,
    result: SQLResult,
    *,
    trace: TraceCollector,
    review_retry_count: int,
) -> None:
    """Store only Reviewer-approved SQL; optional memory failure is nonfatal."""

    started_at = perf_counter()
    _add_workflow_event(
        trace,
        EventType.SQL_MEMORY_STORE_STARTED,
        task_id=task.task_id,
        review_retry_count=review_retry_count,
        duration=0.0,
        tool="wren_store_query",
    )
    try:
        sql_agent.wren_tools.store_query(
            task.description,
            result.sql,
            tags=["datapilot-reviewed"],
        )
    except Exception as exc:
        _add_workflow_event(
            trace,
            EventType.SQL_MEMORY_STORE_FAILED,
            task_id=task.task_id,
            review_retry_count=review_retry_count,
            duration=perf_counter() - started_at,
            error_type=type(exc).__name__,
            tool="wren_store_query",
        )
        return
    _add_workflow_event(
        trace,
        EventType.SQL_MEMORY_STORE_SUCCEEDED,
        task_id=task.task_id,
        review_retry_count=review_retry_count,
        duration=perf_counter() - started_at,
        tool="wren_store_query",
    )


def execute_ready_query_tasks(
    state: AgentState,
    sql_agent: SQLAgent,
    reviewer: Reviewer,
    *,
    trace: TraceCollector,
    max_semantic_corrections: int = 1,
) -> tuple[ReviewedQueryRun, ...]:
    """Execute, review, and optionally correct ready query tasks in order."""

    if max_semantic_corrections not in {0, 1}:
        raise ValueError("max_semantic_corrections must be 0 or 1")
    runs: list[ReviewedQueryRun] = []
    stop_workflow = False
    while not stop_workflow:
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

        sql_results = [sql_agent.execute_task(state, task, trace=trace)]
        reviews: list[ReviewerResult] = []
        current_result = sql_results[-1]
        previous_feedback: ReviewerResult | None = None
        review_retry_count = 0
        if not current_result.success:
            stop_workflow = True

        while current_result.success and not stop_workflow:
            review = reviewer.review(
                state,
                task,
                current_result,
                trace=trace,
                review_retry_count=review_retry_count,
                previous_feedback=previous_feedback,
            )
            reviews.append(review)
            if review.decision == "approve":
                _store_verified_sql(
                    sql_agent,
                    task,
                    current_result,
                    trace=trace,
                    review_retry_count=review_retry_count,
                )
                break
            if review.decision == "fail":
                stop_workflow = True
                break
            if review_retry_count >= max_semantic_corrections:
                error = ReviewerRetryLimitError(
                    stage="semantic_retry_limit",
                    task_id=task.task_id,
                    summary="semantic correction limit exhausted",
                )
                _mark_task_failed(state, task)
                _add_workflow_event(
                    trace,
                    EventType.REVIEW_FAILED,
                    task_id=task.task_id,
                    review_retry_count=review_retry_count,
                    duration=0.0,
                    error_type=type(error).__name__,
                )
                stop_workflow = True
                break

            retry_started = perf_counter()
            review_retry_count += 1
            _add_workflow_event(
                trace,
                EventType.SEMANTIC_RETRY_STARTED,
                task_id=task.task_id,
                review_retry_count=review_retry_count,
                duration=0.0,
            )
            current_result = sql_agent.execute_correction(
                state,
                task,
                current_result,
                review,
                trace=trace,
                semantic_retry_count=review_retry_count,
            )
            sql_results.append(current_result)
            _add_workflow_event(
                trace,
                EventType.SEMANTIC_RETRY_COMPLETED,
                task_id=task.task_id,
                review_retry_count=review_retry_count,
                duration=perf_counter() - retry_started,
                error_type=(
                    None if current_result.success else "SQLCorrectionFailed"
                ),
            )
            if not current_result.success:
                _mark_task_failed(state, task)
                stop_workflow = True
                break
            previous_feedback = review

        runs.append(
            ReviewedQueryRun(
                task_id=task.task_id,
                sql_results=tuple(sql_results),
                review_results=tuple(reviews),
            )
        )
    return tuple(runs)


@dataclass(frozen=True, slots=True)
class GraphSkeleton:
    """Execute planning and reviewed queries, then stop before analysis."""

    planner: Planner
    sql_agent: SQLAgent
    reviewer: Reviewer
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

    def run_query_reviews(
        self,
        state: AgentState,
        *,
        trace: TraceCollector,
    ) -> tuple[ReviewedQueryRun, ...]:
        """Execute all ready query tasks through the review workflow."""

        return execute_ready_query_tasks(
            state,
            self.sql_agent,
            self.reviewer,
            trace=trace,
        )

    def run(self, state: AgentState, *, trace: TraceCollector) -> AgentState:
        """Run implemented nodes and stop at the future Analyst boundary."""

        self.run_planner(state, trace=trace)
        runs = self.run_query_reviews(state, trace=trace)
        if any(not run.approved for run in runs):
            return state
        raise NotImplementedError("DataPilot Analyst is not implemented yet")


def build_graph(
    planner: Planner,
    sql_agent: SQLAgent,
    reviewer: Reviewer,
) -> GraphSkeleton:
    """Inject Phase 5 nodes into the explicit graph structure."""

    return GraphSkeleton(
        planner=planner,
        sql_agent=sql_agent,
        reviewer=reviewer,
    )
