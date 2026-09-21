from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from datapilot.agent.evidence import build_final_evidence_pack
from datapilot.agent.graph import execute_query_task
from datapilot.agent.reviewer import Reviewer
from datapilot.agent.sql_agent import SQLAgent
from datapilot.agent.state import TaskItem, create_initial_state
from datapilot.tools.contracts import (
    ReadOnlyTool,
    ToolArguments,
    ToolRegistry,
    ToolResult,
)
from datapilot.tools.router import ToolRouter
from datapilot.tools.wren_tools import WrenQueryResult
from datapilot.tracing.trace import EventType, TraceCollector
from domains.media.runtime import build_media_tool_registry
from domains.media.runtime.tools import build_alarm_evidence


class ToolAndSQLWren:
    def __init__(self) -> None:
        self.context_calls = 0
        self.store_calls = 0

    def fetch_context(self, question: str, *, limit: int = 5) -> dict[str, Any]:
        del question, limit
        self.context_calls += 1
        return {"strategy": "full", "schema": "model metrics(value integer)"}

    def recall_queries(
        self, question: str, *, limit: int = 3
    ) -> list[dict[str, Any]]:
        del question, limit
        return []

    def dry_plan(self, sql: str) -> str:
        return f"planned: {sql}"

    def query(self, sql: str, *, limit: int = 100) -> WrenQueryResult:
        del limit
        if "stream_sessions" in sql:
            rows = [
                {
                    "session_count": 60,
                    "successful_sessions": 43,
                    "failed_sessions": 17,
                    "playback_success_rate": 43 / 60,
                    "average_startup_time_ms": 800.0,
                    "total_buffer_duration_seconds": 100.0,
                    "rebuffer_ratio": 0.003,
                }
            ]
        else:
            rows = [{"value": 7}]
        return WrenQueryResult(
            columns=list(rows[0]),
            rows=rows,
            row_count=len(rows),
        )

    def store_query(
        self,
        nl: str,
        sql: str,
        *,
        tags: list[str] | None = None,
    ) -> None:
        del nl, sql, tags
        self.store_calls += 1


class ExplodingSQLModel:
    def complete(self, **kwargs: Any) -> str:
        del kwargs
        raise AssertionError("SQL model must not run for a successful tool route")


class StaticSQLModel:
    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_schema: Mapping[str, Any],
    ) -> str:
        del system_prompt, user_prompt, response_schema
        return json.dumps({"sql": "SELECT value FROM metrics", "summary": "value"})


class ReviewModel:
    def __init__(self, decision: str) -> None:
        self.decision = decision
        self.prompts: list[str] = []

    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_schema: Mapping[str, Any],
    ) -> str:
        del system_prompt, response_schema
        self.prompts.append(user_prompt)
        if self.decision == "approve":
            return json.dumps(
                {
                    "decision": "approve",
                    "reason_summary": "The evidence satisfies the task.",
                    "issues": [],
                    "retry_instruction": None,
                    "confidence": 0.95,
                }
            )
        return json.dumps(
            {
                "decision": "fail",
                "reason_summary": "The evidence is not relevant enough.",
                "issues": [
                    {
                        "issue_type": "result_mismatch",
                        "description": "Returned evidence does not satisfy the task.",
                    }
                ],
                "retry_instruction": None,
                "confidence": 0.9,
            }
        )


class RetryThenApproveReviewModel:
    def __init__(self) -> None:
        self.calls = 0

    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_schema: Mapping[str, Any],
    ) -> str:
        del system_prompt, user_prompt, response_schema
        self.calls += 1
        if self.calls == 1:
            return json.dumps(
                {
                    "decision": "retry",
                    "reason_summary": "Add the wider grouped alarm count.",
                    "issues": [
                        {
                            "issue_type": "missing_data",
                            "description": "The wider grouped count is missing.",
                        }
                    ],
                    "retry_instruction": (
                        "Add the grouped count without removing valid rows."
                    ),
                    "confidence": 0.9,
                }
            )
        return json.dumps(
            {
                "decision": "approve",
                "reason_summary": "The correction supplements the alarm evidence.",
                "issues": [],
                "retry_instruction": None,
                "confidence": 0.95,
            }
        )


def _state() -> tuple[Any, TaskItem, TraceCollector]:
    state = create_initial_state("查询华南当前窗口播放成功率和卡顿情况。")
    task = TaskItem(
        "qoe",
        "查询华南当前窗口播放成功率和卡顿情况。",
        "query",
    )
    state["task_plan"] = [task]
    state["pending_tasks"] = [task]
    state["current_task"] = task
    return state, task, TraceCollector(trace_id=state["trace_id"])


def test_tool_result_enters_existing_reviewer_and_completes_only_on_approval() -> None:
    state, task, trace = _state()
    wren = ToolAndSQLWren()
    review_model = ReviewModel("approve")
    run = execute_query_task(
        state,
        task,
        SQLAgent(
            model_client=ExplodingSQLModel(),
            wren_tools=wren,
            trace=trace,
            max_attempts=1,
        ),
        Reviewer(model_client=review_model, trace=trace),
        trace=trace,
        tool_router=ToolRouter(build_media_tool_registry(wren)),
    )

    assert run.approved is True
    assert run.sql_results[0].execution_source == "tool"
    assert run.sql_results[0].tool_name == "query_qoe_metrics"
    assert task.status == "completed"
    assert state["review_results"][0].decision == "approve"
    assert "source=tool" in review_model.prompts[0]
    assert wren.context_calls == 0
    assert wren.store_calls == 0
    event_types = [event.event_type for event in trace.get_events()]
    assert EventType.TOOL_EXECUTION_SUCCEEDED in event_types
    assert EventType.REVIEW_APPROVED in event_types


def test_tool_success_does_not_bypass_reviewer_failure() -> None:
    state, task, trace = _state()
    wren = ToolAndSQLWren()
    run = execute_query_task(
        state,
        task,
        SQLAgent(
            model_client=ExplodingSQLModel(),
            wren_tools=wren,
            trace=trace,
            max_attempts=1,
        ),
        Reviewer(model_client=ReviewModel("fail"), trace=trace),
        trace=trace,
        tool_router=ToolRouter(build_media_tool_registry(wren)),
    )

    assert state["tool_results"][0]["success"] is True
    assert run.approved is False
    assert task.status == "failed"
    assert task not in state["completed_tasks"]


class BrokenArguments(ToolArguments):
    pass


class AlarmArguments(ToolArguments):
    pass


class SyntheticAlarmTool(ReadOnlyTool):
    name = "synthetic_alarm_tool"
    description = "Returns deterministic mixed-status synthetic alarm rows."
    domain = "test"
    input_model = AlarmArguments

    def routing_score(self, task_description: str) -> int:
        return 100 if "alarm" in task_description.casefold() else 0

    def _execute(self, arguments: ToolArguments) -> ToolResult:
        del arguments
        rows = [
            {
                "alarm_id": f"alarm-{index}",
                "timestamp": f"2026-09-01 11:{index * 10:02d}:00",
                "window_name": "current_window",
                "region": "华南",
                "cdn": "CDN-B",
                "error_code": "E302",
                "severity": "high",
                "status": status,
                "message": f"Origin upstream timeout sample {index}.",
            }
            for index, status in enumerate(
                ("open", "investigating", "resolved"),
                start=1,
            )
        ]
        return ToolResult(
            tool_name=self.name,
            success=True,
            data=rows,
            summary="Three mixed-status synthetic alarms.",
            metadata={
                "executed_query": "synthetic fixed read",
                "arguments": {},
                "columns": list(rows[0]),
                "alarm_evidence": build_alarm_evidence(rows),
            },
            error=None,
            execution_time_ms=0.0,
        )


class BrokenRoutedTool(ReadOnlyTool):
    name = "broken_routed_tool"
    description = "Always fails to exercise SQL fallback."
    domain = "test"
    input_model = BrokenArguments

    def routing_score(self, task_description: str) -> int:
        del task_description
        return 100

    def _execute(self, arguments: ToolArguments) -> ToolResult:
        del arguments
        raise RuntimeError("synthetic tool outage")


def test_tool_failure_falls_back_once_to_existing_sql_agent() -> None:
    state, task, trace = _state()
    wren = ToolAndSQLWren()
    run = execute_query_task(
        state,
        task,
        SQLAgent(
            model_client=StaticSQLModel(),
            wren_tools=wren,
            trace=trace,
            max_attempts=1,
        ),
        Reviewer(model_client=ReviewModel("approve"), trace=trace),
        trace=trace,
        tool_router=ToolRouter(ToolRegistry([BrokenRoutedTool()])),
    )

    assert run.approved is True
    assert run.sql_results[0].execution_source == "sql"
    assert state["tool_fallback_count"] == 1
    assert wren.context_calls == 1
    assert EventType.TOOL_FALLBACK in {
        event.event_type for event in trace.get_events()
    }


def test_semantic_sql_correction_preserves_approved_tool_alarm_evidence() -> None:
    state = create_initial_state("Review alarm evidence and wider grouped counts.")
    task = TaskItem(
        "alarms",
        "Review alarm events and add wider grouped alarm counts.",
        "query",
    )
    state["task_plan"] = [task]
    state["pending_tasks"] = [task]
    state["current_task"] = task
    trace = TraceCollector(trace_id=state["trace_id"])
    review_model = RetryThenApproveReviewModel()
    wren = ToolAndSQLWren()

    run = execute_query_task(
        state,
        task,
        SQLAgent(
            model_client=StaticSQLModel(),
            wren_tools=wren,
            trace=trace,
            max_attempts=1,
        ),
        Reviewer(model_client=review_model, trace=trace),
        trace=trace,
        tool_router=ToolRouter(ToolRegistry([SyntheticAlarmTool()])),
    )

    corrected = run.sql_results[-1]
    pack = build_final_evidence_pack([corrected], [])
    alarm = next(
        item
        for item in pack["data_evidence"]
        if item["kind"] == "alarm_status_distribution"
    )
    lineage = pack["reviewed_evidence"][0]

    assert run.approved is True
    assert review_model.calls == 2
    assert corrected.execution_source == "sql"
    assert alarm["facts"]["mixed"] is True
    assert alarm["facts"]["distribution"] == [
        {"value": "investigating", "count": 1},
        {"value": "open", "count": 1},
        {"value": "resolved", "count": 1},
    ]
    assert {
        item["role"] for item in lineage["evidence_sources"]
    } == {"base_tool_evidence", "correction_evidence"}
    assert lineage["final_review_status"] == "approve"
