"""Derived, dependency-free summaries for one DataPilot trace."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from datapilot.tracing.trace import EventType, TraceEvent


@dataclass(frozen=True, slots=True)
class TraceSummary:
    """Metrics that can be reconstructed from currently emitted trace events."""

    trace_id: str | None
    total_duration_ms: float | None
    planner_duration_ms: float | None
    sql_agent_duration_ms: float | None
    reviewer_duration_ms: float | None
    analyst_duration_ms: float | None
    final_answer_duration_ms: float | None
    sql_query_count: int
    review_count: int
    technical_retry_count: int
    semantic_retry_count: int
    follow_up_resolution_count: int
    task_count: int | None
    completed_task_count: int
    failed_task_count: int
    success: bool | None
    llm_call_count: int | None = None
    token_usage: int | None = None
    tool_router_duration_ms: float | None = None
    tool_execution_duration_ms: float | None = None
    tool_call_count: int = 0
    tool_fallback_count: int = 0


def _duration_ms(events: list[TraceEvent], types: set[EventType]) -> float | None:
    durations = [
        event.metadata.get("duration")
        for event in events
        if event.event_type in types
    ]
    numeric = [
        float(value)
        for value in durations
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    return sum(numeric) * 1000 if numeric else None


def _reviewer_duration_ms(events: list[TraceEvent]) -> float | None:
    """Count each review once; REVIEW_FAILED may duplicate REVIEW_RESULT."""

    terminal: dict[tuple[Any, Any], float] = {}
    for event in events:
        if event.event_type not in {EventType.REVIEW_RESULT, EventType.REVIEW_FAILED}:
            continue
        duration = event.metadata.get("duration")
        if not isinstance(duration, (int, float)) or isinstance(duration, bool):
            continue
        key = (
            event.metadata.get("task_id"),
            event.metadata.get("review_retry_count", 0),
        )
        terminal[key] = float(duration)
    return sum(terminal.values()) * 1000 if terminal else None


def _task_outcomes(events: list[TraceEvent]) -> dict[str, bool]:
    success_types = {
        EventType.REVIEW_APPROVED,
        EventType.ANALYST_COMPLETED,
        EventType.FINAL_ANSWER_COMPLETED,
    }
    failure_types = {
        EventType.SQL_AGENT_FAILED,
        EventType.REVIEW_FAILED,
        EventType.ANALYST_FAILED,
        EventType.FINAL_ANSWER_FAILED,
    }
    outcomes: dict[str, bool] = {}
    for event in events:
        task_id = event.metadata.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            continue
        if event.event_type in success_types:
            outcomes[task_id] = True
        elif event.event_type in failure_types:
            outcomes[task_id] = False
    return outcomes


def summarize_trace(events: list[TraceEvent]) -> TraceSummary:
    """Summarize one trace without estimating unavailable LLM/token metrics."""

    trace_ids = {event.trace_id for event in events}
    if len(trace_ids) > 1:
        raise ValueError("events must belong to one trace_id")
    trace_id = next(iter(trace_ids), None)
    total_duration_ms = None
    if events:
        timestamps = [event.timestamp for event in events]
        total_duration_ms = max(
            0.0,
            (max(timestamps) - min(timestamps)).total_seconds() * 1000,
        )

    planner_task_counts = [
        event.metadata.get("task_count")
        for event in events
        if event.event_type is EventType.PLANNER_RESULT
        and isinstance(event.metadata.get("task_count"), int)
    ]
    task_count = max(planner_task_counts) if planner_task_counts else None
    outcomes = _task_outcomes(events)
    completed = sum(outcomes.values())
    failed = len(outcomes) - completed
    failure_types = {
        EventType.PLANNER_FAILED,
        EventType.FOLLOW_UP_RESOLUTION_FAILED,
        EventType.SQL_AGENT_FAILED,
        EventType.REVIEW_FAILED,
        EventType.ANALYST_FAILED,
        EventType.FINAL_ANSWER_FAILED,
    }
    has_explicit_failure = any(
        event.event_type in failure_types for event in events
    )
    if any(
        event.event_type is EventType.SESSION_TURN_COMPLETED for event in events
    ):
        success: bool | None = True
    elif has_explicit_failure or failed:
        success = False
    elif task_count is not None:
        success = completed == task_count
    else:
        success = None

    return TraceSummary(
        trace_id=trace_id,
        total_duration_ms=total_duration_ms,
        planner_duration_ms=_duration_ms(
            events,
            {EventType.PLANNER_RESULT, EventType.PLANNER_FAILED},
        ),
        sql_agent_duration_ms=_duration_ms(
            events,
            {EventType.SQL_AGENT_COMPLETED, EventType.SQL_AGENT_FAILED},
        ),
        reviewer_duration_ms=_reviewer_duration_ms(events),
        analyst_duration_ms=_duration_ms(
            events,
            {EventType.ANALYST_COMPLETED, EventType.ANALYST_FAILED},
        ),
        final_answer_duration_ms=_duration_ms(
            events,
            {EventType.FINAL_ANSWER_COMPLETED, EventType.FINAL_ANSWER_FAILED},
        ),
        sql_query_count=sum(
            event.event_type is EventType.SQL_EXECUTION_STARTED for event in events
        ),
        review_count=sum(
            event.event_type is EventType.REVIEW_STARTED for event in events
        ),
        technical_retry_count=sum(
            event.event_type is EventType.SQL_RETRY for event in events
        ),
        semantic_retry_count=sum(
            event.event_type is EventType.SEMANTIC_RETRY_STARTED for event in events
        ),
        follow_up_resolution_count=sum(
            event.event_type is EventType.FOLLOW_UP_RESOLUTION_STARTED
            for event in events
        ),
        task_count=task_count,
        completed_task_count=completed,
        failed_task_count=failed,
        success=success,
        tool_router_duration_ms=_duration_ms(
            events,
            {EventType.TOOL_ROUTING_COMPLETED},
        ),
        tool_execution_duration_ms=_duration_ms(
            events,
            {
                EventType.TOOL_EXECUTION_SUCCEEDED,
                EventType.TOOL_EXECUTION_FAILED,
            },
        ),
        tool_call_count=sum(
            event.event_type is EventType.TOOL_EXECUTION_STARTED
            for event in events
        ),
        tool_fallback_count=sum(
            event.event_type is EventType.TOOL_FALLBACK for event in events
        ),
    )


def format_trace_summary(summary: TraceSummary) -> str:
    """Render the compact CLI view of a trace summary."""

    total = "unknown" if summary.task_count is None else str(summary.task_count)
    duration = (
        "unknown"
        if summary.total_duration_ms is None
        else f"{summary.total_duration_ms:.1f} ms"
    )
    return "\n".join(
        [
            "[Run Summary]",
            f"Tasks: {summary.completed_task_count}/{total} completed",
            f"SQL Queries: {summary.sql_query_count}",
            f"Tool Calls: {summary.tool_call_count}",
            f"Tool Fallbacks: {summary.tool_fallback_count}",
            f"Reviews: {summary.review_count}",
            f"Technical Retries: {summary.technical_retry_count}",
            f"Semantic Retries: {summary.semantic_retry_count}",
            f"Duration: {duration}",
        ]
    )
