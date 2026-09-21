"""Sequential DataPilot workflow dispatcher.

Ready tasks execute in Planner order: query tasks pass through SQL and semantic
review, analysis tasks use the grounded Analyst, and response tasks produce the
final answer.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from datapilot.agent.analyst import Analyst
from datapilot.agent.follow_up import (
    FollowUpResolution,
    FollowUpResolver,
    SessionContextExtractor,
    merge_session_context,
)
from datapilot.agent.planner import Planner, PlannerIntent, PlannerResult
from datapilot.agent.reviewer import Reviewer, ReviewerRetryLimitError
from datapilot.agent.sql_agent import SQLAgent, is_task_ready
from datapilot.agent.state import (
    AgentState,
    AnalysisResult,
    FinalAnswerResult,
    ReviewerResult,
    SQLResult,
    SessionContext,
    TaskItem,
    get_effective_query,
)
from datapilot.memory.session_memory import SessionMemoryStore
from datapilot.tools.integration import (
    mark_evidence_review_status,
    merge_correction_evidence,
    serialized_tool_result,
    tool_result_to_sql_result,
)
from datapilot.tools.router import ToolRouter
from datapilot.tools.wren_tools import try_fetch_planning_context
from datapilot.tracing.trace import EventType, TraceCollector

GRAPH_NODES = (
    "start",
    "planner",
    "session_context",
    "follow_up_resolver",
    "replan",
    "dispatcher",
    "sql_agent",
    "reviewer",
    "analyst",
    "final_answer",
    "end",
)
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


@dataclass(frozen=True, slots=True)
class WorkflowRun:
    """Observable outputs from one sequential task-plan execution."""

    reviewed_queries: tuple[ReviewedQueryRun, ...]
    analysis_results: tuple[AnalysisResult, ...]
    final_answer_result: FinalAnswerResult | None


@dataclass(frozen=True, slots=True)
class TurnPlanningRun:
    """Initial planning plus optional one-shot follow-up resolution."""

    initial_result: PlannerResult
    effective_result: PlannerResult | None
    resolution: FollowUpResolution | None = None
    error: str | None = None

    @property
    def can_execute(self) -> bool:
        return self.effective_result is not None and self.error is None


@dataclass(frozen=True, slots=True)
class SessionTurnRun:
    """Observable outcome of one complete session-aware turn."""

    planning: TurnPlanningRun
    workflow: WorkflowRun | None
    memory_updated: bool = False

    @property
    def success(self) -> bool:
        return bool(
            self.workflow
            and self.workflow.final_answer_result
            and self.workflow.final_answer_result.success
            and self.memory_updated
        )


def _session_metadata(state: AgentState) -> dict[str, Any]:
    context = state["session_context"]
    return {
        "session_id": context.session_id[:8],
        "turn_index": context.turn_index,
    }


def prepare_session_turn(
    state: AgentState,
    planner: Planner,
    resolver: FollowUpResolver,
    session_store: SessionMemoryStore,
    *,
    trace: TraceCollector,
    planning_context: Mapping[str, Any] | None = None,
) -> TurnPlanningRun:
    """Plan raw input and resolve/re-plan at most one confirmed follow-up."""

    initial_result = planner.plan(
        state,
        trace=trace,
        planning_context=planning_context,
    )
    if initial_result.intent is not PlannerIntent.FOLLOW_UP:
        return TurnPlanningRun(
            initial_result=initial_result,
            effective_result=initial_result,
        )

    state["was_follow_up"] = True
    trace.add_event(
        EventType.FOLLOW_UP_DETECTED,
        component="session_workflow",
        action="detect_follow_up",
        summary="Planner identified a follow-up request.",
        metadata=_session_metadata(state),
    )
    previous = session_store.get(state["session_context"].session_id)
    has_context = bool(previous and previous.has_business_context)
    trace.add_event(
        EventType.SESSION_CONTEXT_LOADED,
        component="session_workflow",
        action="load_context",
        summary="Session context lookup completed.",
        metadata={**_session_metadata(state), "has_business_context": has_context},
    )
    if previous is None or not previous.has_business_context:
        resolution = FollowUpResolution(
            can_resolve=False,
            missing_fields=["session_context"],
            reason_summary="Missing previous successful analysis context.",
        )
        state["follow_up_resolution"] = resolution
        trace.add_event(
            EventType.FOLLOW_UP_RESOLUTION_FAILED,
            component="session_workflow",
            action="load_context",
            summary="Follow-up cannot be resolved without prior context.",
            metadata=_session_metadata(state),
        )
        return TurnPlanningRun(
            initial_result=initial_result,
            effective_result=None,
            resolution=resolution,
            error=resolution.reason_summary,
        )

    resolution = resolver.resolve(
        state["original_query"],
        previous,
        trace=trace,
    )
    state["follow_up_resolution"] = resolution
    if not resolution.can_resolve or resolution.resolved_query is None:
        return TurnPlanningRun(
            initial_result=initial_result,
            effective_result=None,
            resolution=resolution,
            error=resolution.reason_summary,
        )

    state["resolved_query"] = resolution.resolved_query
    effective_result = planner.plan(
        state,
        trace=trace,
        planning_context=planning_context,
    )
    if effective_result.intent is PlannerIntent.FOLLOW_UP:
        failed_resolution = deepcopy(resolution)
        failed_resolution.can_resolve = False
        failed_resolution.missing_fields = ["standalone_query"]
        failed_resolution.reason_summary = (
            "Resolved query was still classified as a follow-up."
        )
        state["follow_up_resolution"] = failed_resolution
        state["resolved_query"] = None
        trace.add_event(
            EventType.FOLLOW_UP_RESOLUTION_FAILED,
            component="session_workflow",
            action="replan",
            summary="Follow-up re-plan did not produce a standalone request.",
            metadata=_session_metadata(state),
        )
        return TurnPlanningRun(
            initial_result=initial_result,
            effective_result=None,
            resolution=failed_resolution,
            error=failed_resolution.reason_summary,
        )
    return TurnPlanningRun(
        initial_result=initial_result,
        effective_result=effective_result,
        resolution=resolution,
    )


def _workflow_succeeded(state: AgentState, workflow: WorkflowRun) -> bool:
    return bool(
        workflow.final_answer_result
        and workflow.final_answer_result.success
        and state["final_answer"]
        and all(result.success for result in state["analysis_results"])
        and all(result.success for result in workflow.analysis_results)
        and not state["pending_tasks"]
        and all(task.status == "completed" for task in state["task_plan"])
    )


def commit_session_context(
    state: AgentState,
    planner_result: PlannerResult,
    workflow: WorkflowRun,
    session_store: SessionMemoryStore,
    extractor: SessionContextExtractor,
    *,
    trace: TraceCollector,
    resolution: FollowUpResolution | None = None,
) -> bool:
    """Commit compact semantic context only after full workflow success."""

    if not _workflow_succeeded(state, workflow):
        return False
    session_id = state["session_context"].session_id
    if state["was_follow_up"]:
        previous = session_store.get(session_id)
        if previous is None or resolution is None:
            return False
        context = merge_session_context(previous, resolution)
    else:
        update = extractor.extract(get_effective_query(state), state["task_plan"])
        context = SessionContext(session_id=session_id)
        context.metrics = list(update.metrics)
        context.dimensions = list(update.dimensions)
        context.time_range = deepcopy(update.time_range)
        context.filters = deepcopy(update.filters)
        context.entities = deepcopy(update.entities)
        context.analysis_goal = update.analysis_goal

    context.turn_index = state["session_context"].turn_index
    context.last_user_query = state["original_query"]
    context.last_resolved_query = get_effective_query(state)
    context.last_intent = planner_result.intent.value
    context.last_answer_summary = " ".join(state["final_answer"].split())[:300]
    context.last_source_task_ids = list(
        workflow.final_answer_result.source_task_ids
    )
    context.updated_at = datetime.now(UTC).isoformat()
    session_store.save(context)
    state["session_context"] = deepcopy(context)
    slot_values = (
        context.metrics,
        context.dimensions,
        context.time_range.labels,
        context.filters,
        context.entities,
        context.analysis_goal,
    )
    trace.add_event(
        EventType.SESSION_CONTEXT_UPDATED,
        component="session_workflow",
        action="commit_context",
        summary="Authoritative session context was updated.",
        metadata={
            **_session_metadata(state),
            "context_field_count": sum(bool(value) for value in slot_values),
            "was_follow_up": state["was_follow_up"],
        },
    )
    trace.add_event(
        EventType.SESSION_TURN_COMPLETED,
        component="session_workflow",
        action="complete_turn",
        summary="Session turn completed successfully.",
        metadata={
            **_session_metadata(state),
            "was_follow_up": state["was_follow_up"],
        },
    )
    return True


def run_session_turn(
    state: AgentState,
    planner: Planner,
    sql_agent: SQLAgent,
    reviewer: Reviewer,
    analyst: Analyst,
    resolver: FollowUpResolver,
    extractor: SessionContextExtractor,
    session_store: SessionMemoryStore,
    *,
    trace: TraceCollector,
    planning_context: Mapping[str, Any] | None = None,
    tool_router: ToolRouter | None = None,
) -> SessionTurnRun:
    """Run one bounded, session-aware DataPilot turn."""

    active_planning_context = planning_context
    if active_planning_context is None:
        active_planning_context = try_fetch_planning_context(
            sql_agent.wren_tools
        )
    planning = prepare_session_turn(
        state,
        planner,
        resolver,
        session_store,
        trace=trace,
        planning_context=active_planning_context,
    )
    if not planning.can_execute or planning.effective_result is None:
        return SessionTurnRun(planning=planning, workflow=None)
    workflow = execute_task_plan(
        state,
        sql_agent,
        reviewer,
        analyst,
        trace=trace,
        tool_router=tool_router,
    )
    updated = commit_session_context(
        state,
        planning.effective_result,
        workflow,
        session_store,
        extractor,
        trace=trace,
        resolution=planning.resolution,
    )
    return SessionTurnRun(
        planning=planning,
        workflow=workflow,
        memory_updated=updated,
    )


def get_next_ready_task(state: AgentState) -> TaskItem | None:
    """Return the first pending task with completed dependencies in plan order."""

    completed_ids = {
        item.task_id for item in state["completed_tasks"] if item.status == "completed"
    }
    pending_ids = {item.task_id for item in state["pending_tasks"]}
    return next(
        (
            item
            for item in state["task_plan"]
            if item.task_id in pending_ids
            and item.status == "pending"
            and set(item.depends_on) <= completed_ids
        ),
        None,
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


def _execute_initial_query(
    state: AgentState,
    task: TaskItem,
    sql_agent: SQLAgent,
    *,
    trace: TraceCollector,
    tool_router: ToolRouter | None,
) -> SQLResult:
    """Execute one routed tool call, or preserve the existing SQL path."""

    if tool_router is None:
        return sql_agent.execute_task(state, task, trace=trace)

    trace.add_event(
        EventType.TOOL_ROUTING_STARTED,
        component="tool_router",
        action="route_query_task",
        summary="Started bounded Query Task routing.",
        metadata={"task_id": task.task_id},
    )
    decision = tool_router.route(
        task.description,
        metric_binding=task.metric_binding,
        requested_dimensions=task.requested_dimensions,
    )
    state["tool_routes"].append(decision.as_dict())
    trace.add_event(
        EventType.TOOL_ROUTING_COMPLETED,
        component="tool_router",
        action="route_query_task",
        summary="Completed bounded Query Task routing.",
        metadata={
            "task_id": task.task_id,
            "route": decision.route,
            "tool": decision.tool_name,
            "deterministic": decision.deterministic,
            "model_attempts": decision.model_attempts,
            "duration": decision.latency_ms / 1000,
        },
    )
    if decision.route == "sql" or decision.tool_name is None:
        return sql_agent.execute_task(state, task, trace=trace)

    trace.add_event(
        EventType.TOOL_EXECUTION_STARTED,
        component="tool_registry",
        action="execute_read_only_tool",
        summary="Started a validated read-only domain tool.",
        metadata={"task_id": task.task_id, "tool": decision.tool_name},
    )
    tool_result = tool_router.registry.execute(
        decision.tool_name,
        decision.arguments,
    )
    state["tool_results"].append(serialized_tool_result(tool_result))
    if tool_result.success:
        result = tool_result_to_sql_result(task, tool_result)
        task.status = "executed"
        if result.sql:
            state["generated_sql"].append(result.sql)
        state["sql_results"].append(result)
        state["current_task"] = task
        trace.add_event(
            EventType.TOOL_EXECUTION_SUCCEEDED,
            component="tool_registry",
            action="execute_read_only_tool",
            summary="Read-only domain tool returned reviewable query evidence.",
            metadata={
                "task_id": task.task_id,
                "tool": decision.tool_name,
                "row_count": result.row_count,
                "duration": result.execution_time,
            },
        )
        return result

    trace.add_event(
        EventType.TOOL_EXECUTION_FAILED,
        component="tool_registry",
        action="execute_read_only_tool",
        summary="Read-only domain tool failed safely.",
        metadata={
            "task_id": task.task_id,
            "tool": decision.tool_name,
            "error_type": tool_result.metadata.get("error_type", "tool_error"),
            "duration": tool_result.execution_time_ms / 1000,
        },
    )
    state["tool_fallback_count"] += 1
    trace.add_event(
        EventType.TOOL_FALLBACK,
        component="tool_router",
        action="fallback_to_sql_agent",
        summary="Tool failure fell back once to the existing SQL Agent.",
        metadata={"task_id": task.task_id, "tool": decision.tool_name},
    )
    return sql_agent.execute_task(state, task, trace=trace)


def execute_query_task(
    state: AgentState,
    task: TaskItem,
    sql_agent: SQLAgent,
    reviewer: Reviewer,
    *,
    trace: TraceCollector,
    max_semantic_corrections: int = 1,
    tool_router: ToolRouter | None = None,
) -> ReviewedQueryRun:
    """Execute, review, and optionally correct one ready query task."""

    if max_semantic_corrections not in {0, 1}:
        raise ValueError("max_semantic_corrections must be 0 or 1")
    stop_workflow = False
    if not is_task_ready(state, task):
        raise ValueError("task must be a ready query task")
    sql_results = [
        _execute_initial_query(
            state,
            task,
            sql_agent,
            trace=trace,
            tool_router=tool_router,
        )
    ]
    reviews: list[ReviewerResult] = []
    current_result = sql_results[-1]
    previous_feedback: ReviewerResult | None = None
    review_retry_count = 0
    if not current_result.success:
        _mark_task_failed(state, task)
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
            mark_evidence_review_status(current_result, "approve")
            if current_result.execution_source == "sql":
                _store_verified_sql(
                    sql_agent,
                    task,
                    current_result,
                    trace=trace,
                    review_retry_count=review_retry_count,
                )
            break
        if review.decision == "fail":
            mark_evidence_review_status(current_result, "fail")
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
            mark_evidence_review_status(current_result, "fail")
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
        corrected_result = sql_agent.execute_correction(
            state,
            task,
            current_result,
            review,
            trace=trace,
            semantic_retry_count=review_retry_count,
        )
        current_result = merge_correction_evidence(
            sql_results[0],
            corrected_result,
        )
        sql_results.append(current_result)
        _add_workflow_event(
            trace,
            EventType.SEMANTIC_RETRY_COMPLETED,
            task_id=task.task_id,
            review_retry_count=review_retry_count,
            duration=perf_counter() - retry_started,
            error_type=None if current_result.success else "SQLCorrectionFailed",
        )
        if not current_result.success:
            mark_evidence_review_status(current_result, "fail")
            _mark_task_failed(state, task)
            stop_workflow = True
            break
        previous_feedback = review

    return ReviewedQueryRun(
        task_id=task.task_id,
        sql_results=tuple(sql_results),
        review_results=tuple(reviews),
    )


def execute_ready_query_tasks(
    state: AgentState,
    sql_agent: SQLAgent,
    reviewer: Reviewer,
    *,
    trace: TraceCollector,
    max_semantic_corrections: int = 1,
    tool_router: ToolRouter | None = None,
) -> tuple[ReviewedQueryRun, ...]:
    """Compatibility helper that executes all currently ready query tasks."""

    runs: list[ReviewedQueryRun] = []
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
        run = execute_query_task(
            state,
            task,
            sql_agent,
            reviewer,
            trace=trace,
            max_semantic_corrections=max_semantic_corrections,
            tool_router=tool_router,
        )
        runs.append(run)
        if not run.approved:
            break
    return tuple(runs)


def execute_task_plan(
    state: AgentState,
    sql_agent: SQLAgent,
    reviewer: Reviewer,
    analyst: Analyst,
    *,
    trace: TraceCollector,
    tool_router: ToolRouter | None = None,
) -> WorkflowRun:
    """Dispatch ready tasks serially in task-plan order until done or failed."""

    query_runs: list[ReviewedQueryRun] = []
    analysis_results: list[AnalysisResult] = []
    final_result: FinalAnswerResult | None = None
    while True:
        task = get_next_ready_task(state)
        if task is None:
            break
        state["current_task"] = task
        if task.task_type == "query":
            run = execute_query_task(
                state,
                task,
                sql_agent,
                reviewer,
                trace=trace,
                tool_router=tool_router,
            )
            query_runs.append(run)
            if not run.approved:
                break
        elif task.task_type == "analysis":
            analysis_results.append(
                analyst.execute_task(state, task, trace=trace)
            )
        elif task.task_type == "response":
            final_result = analyst.generate_final_answer(
                state,
                task,
                trace=trace,
            )
        else:
            raise ValueError(f"unsupported task type: {task.task_type}")
    return WorkflowRun(
        reviewed_queries=tuple(query_runs),
        analysis_results=tuple(analysis_results),
        final_answer_result=final_result,
    )


@dataclass(frozen=True, slots=True)
class GraphSkeleton:
    """Execute Phase 6 directly or a configured Phase 7 session turn."""

    planner: Planner
    sql_agent: SQLAgent
    reviewer: Reviewer
    analyst: Analyst
    follow_up_resolver: FollowUpResolver | None = None
    context_extractor: SessionContextExtractor | None = None
    session_store: SessionMemoryStore | None = None
    tool_router: ToolRouter | None = None
    nodes: tuple[str, ...] = GRAPH_NODES
    edges: tuple[tuple[str, str], ...] = GRAPH_EDGES

    def run_planner(
        self,
        state: AgentState,
        *,
        trace: TraceCollector,
        planning_context: Mapping[str, Any] | None = None,
    ) -> PlannerResult:
        """Run the real Planner node and return its structured result."""

        active_planning_context = planning_context
        if active_planning_context is None:
            active_planning_context = try_fetch_planning_context(
                self.sql_agent.wren_tools
            )
        return self.planner.plan(
            state,
            trace=trace,
            planning_context=active_planning_context,
        )

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
            tool_router=self.tool_router,
        )

    def run(self, state: AgentState, *, trace: TraceCollector) -> AgentState:
        """Run the configured workflow and return the updated turn state."""

        if (
            self.follow_up_resolver is not None
            and self.context_extractor is not None
            and self.session_store is not None
        ):
            run_session_turn(
                state,
                self.planner,
                self.sql_agent,
                self.reviewer,
                self.analyst,
                self.follow_up_resolver,
                self.context_extractor,
                self.session_store,
                trace=trace,
                tool_router=self.tool_router,
            )
            return state
        self.run_planner(state, trace=trace)
        execute_task_plan(
            state,
            self.sql_agent,
            self.reviewer,
            self.analyst,
            trace=trace,
            tool_router=self.tool_router,
        )
        return state


def build_graph(
    planner: Planner,
    sql_agent: SQLAgent,
    reviewer: Reviewer,
    analyst: Analyst | None = None,
    follow_up_resolver: FollowUpResolver | None = None,
    context_extractor: SessionContextExtractor | None = None,
    session_store: SessionMemoryStore | None = None,
    tool_router: ToolRouter | None = None,
) -> GraphSkeleton:
    """Inject Phase 6 nodes plus optional Phase 7 session components."""

    return GraphSkeleton(
        planner=planner,
        sql_agent=sql_agent,
        reviewer=reviewer,
        analyst=analyst or Analyst(model_client=planner.model_client),
        follow_up_resolver=follow_up_resolver,
        context_extractor=context_extractor,
        session_store=session_store,
        tool_router=tool_router,
    )
