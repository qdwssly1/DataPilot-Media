from __future__ import annotations

from datetime import UTC, datetime, timedelta

from datapilot.tracing.summary import summarize_trace
from datapilot.tracing.trace import EventType, TraceEvent


BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _event(
    event_type: EventType,
    offset_ms: int,
    **metadata: object,
) -> TraceEvent:
    return TraceEvent(
        trace_id="trace-1",
        timestamp=BASE + timedelta(milliseconds=offset_ms),
        event_type=event_type,
        component="test",
        action="test",
        summary="Synthetic trace event.",
        metadata=dict(metadata),
    )


def test_trace_summary_success() -> None:
    events = [
        _event(EventType.PLANNER_STARTED, 0),
        _event(EventType.PLANNER_RESULT, 100, duration=0.1, task_count=4),
        _event(EventType.SQL_EXECUTION_STARTED, 120, task_id="q1"),
        _event(EventType.SQL_AGENT_COMPLETED, 200, task_id="q1", duration=0.08),
        _event(EventType.REVIEW_STARTED, 210, task_id="q1"),
        _event(EventType.REVIEW_RESULT, 250, task_id="q1", duration=0.04),
        _event(EventType.REVIEW_APPROVED, 251, task_id="q1", duration=0.04),
        _event(EventType.SQL_EXECUTION_STARTED, 300, task_id="q2"),
        _event(EventType.SQL_AGENT_COMPLETED, 380, task_id="q2", duration=0.08),
        _event(EventType.REVIEW_STARTED, 390, task_id="q2"),
        _event(EventType.REVIEW_RESULT, 430, task_id="q2", duration=0.04),
        _event(EventType.REVIEW_APPROVED, 431, task_id="q2", duration=0.04),
        _event(EventType.ANALYST_COMPLETED, 500, task_id="analysis", duration=0.06),
        _event(
            EventType.FINAL_ANSWER_COMPLETED,
            700,
            task_id="answer",
            duration=0.2,
        ),
        _event(EventType.SESSION_TURN_COMPLETED, 800),
    ]

    summary = summarize_trace(events)

    assert summary.trace_id == "trace-1"
    assert summary.total_duration_ms == 800
    assert summary.planner_duration_ms == 100
    assert summary.sql_agent_duration_ms == 160
    assert summary.reviewer_duration_ms == 80
    assert summary.analyst_duration_ms == 60
    assert summary.final_answer_duration_ms == 200
    assert summary.sql_query_count == 2
    assert summary.review_count == 2
    assert summary.task_count == 4
    assert summary.completed_task_count == 4
    assert summary.failed_task_count == 0
    assert summary.success is True


def test_trace_summary_retry_counts() -> None:
    events = [
        _event(EventType.PLANNER_RESULT, 0, duration=0.1, task_count=1),
        _event(EventType.SQL_RETRY, 10, task_id="q1"),
        _event(EventType.SEMANTIC_RETRY_STARTED, 20, task_id="q1"),
        _event(EventType.REVIEW_APPROVED, 30, task_id="q1"),
    ]

    summary = summarize_trace(events)

    assert summary.technical_retry_count == 1
    assert summary.semantic_retry_count == 1


def test_trace_summary_failed_run() -> None:
    events = [
        _event(EventType.PLANNER_RESULT, 0, duration=0.1, task_count=2),
        _event(EventType.REVIEW_APPROVED, 10, task_id="q1"),
        _event(EventType.ANALYST_FAILED, 20, task_id="analysis", duration=0.01),
    ]

    summary = summarize_trace(events)

    assert summary.completed_task_count == 1
    assert summary.failed_task_count == 1
    assert summary.success is False


def test_trace_summary_does_not_invent_missing_metrics() -> None:
    summary = summarize_trace([_event(EventType.USER_QUERY, 0)])

    assert summary.planner_duration_ms is None
    assert summary.sql_agent_duration_ms is None
    assert summary.task_count is None
    assert summary.success is None
    assert summary.llm_call_count is None
    assert summary.token_usage is None


def test_trace_summary_follow_up_run() -> None:
    events = [
        _event(EventType.PLANNER_RESULT, 0, duration=0.1, task_count=1),
        _event(EventType.FOLLOW_UP_RESOLUTION_STARTED, 10),
        _event(EventType.FOLLOW_UP_RESOLVED, 20),
        _event(EventType.PLANNER_RESULT, 30, duration=0.1, task_count=2),
        _event(EventType.REVIEW_APPROVED, 40, task_id="query"),
        _event(
            EventType.FINAL_ANSWER_COMPLETED,
            50,
            task_id="answer",
            duration=0.01,
        ),
        _event(EventType.SESSION_TURN_COMPLETED, 60),
    ]

    summary = summarize_trace(events)

    assert summary.follow_up_resolution_count == 1
    assert summary.task_count == 2
    assert summary.completed_task_count == 2
    assert summary.success is True
