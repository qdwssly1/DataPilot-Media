from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from datapilot.agent.graph import build_graph
from datapilot.agent.planner import Planner, PlannerIntent
from datapilot.agent.sql_agent import SQLAgent
from datapilot.agent.state import create_initial_state
from datapilot.tools.wren_tools import WrenQueryResult
from datapilot.tracing.trace import EventType, TraceCollector


class StaticPlannerModel:
    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_schema: Mapping[str, Any],
    ) -> str:
        del system_prompt, user_prompt, response_schema
        return json.dumps(
            {
                "intent": "single_query",
                "reason_summary": "A database lookup is required.",
                "tasks": [
                    {
                        "task_id": "task_1",
                        "description": "Retrieve the requested metric.",
                        "task_type": "query",
                        "depends_on": [],
                        "status": "pending",
                    }
                ],
                "requires_database": True,
                "requires_context": False,
                "is_follow_up": False,
            }
        )


class StaticSQLModel:
    def __init__(self, sql: str = "SELECT value FROM metrics") -> None:
        self.sql = sql

    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_schema: Mapping[str, Any],
    ) -> str:
        del system_prompt, user_prompt, response_schema
        return json.dumps({"sql": self.sql, "summary": "Retrieve the metric."})


class FakeWrenTools:
    def fetch_context(self, question: str, *, limit: int = 5) -> dict[str, Any]:
        del question, limit
        return {"strategy": "full", "schema": "model metrics(value integer)"}

    def recall_queries(
        self,
        question: str,
        *,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        del question, limit
        return []

    def dry_plan(self, sql: str) -> str:
        return f"planned: {sql}"

    def query(self, sql: str, *, limit: int = 100) -> WrenQueryResult:
        del sql, limit
        return WrenQueryResult(
            columns=["value"],
            rows=[{"value": 7}],
            row_count=1,
        )


def _graph(trace: TraceCollector, *, sql: str = "SELECT value FROM metrics") -> Any:
    return build_graph(
        Planner(model_client=StaticPlannerModel()),
        SQLAgent(
            model_client=StaticSQLModel(sql),
            wren_tools=FakeWrenTools(),
            trace=trace,
            max_attempts=1,
        ),
    )


def test_graph_runs_real_planner_node() -> None:
    state = create_initial_state("show sales")
    trace = TraceCollector(trace_id=state["trace_id"])
    graph = _graph(trace)

    result = graph.run_planner(state, trace=trace)

    assert result.intent is PlannerIntent.SINGLE_QUERY
    assert state["current_task"] is state["task_plan"][0]
    assert [event.event_type for event in trace.get_events()] == [
        EventType.PLANNER_STARTED,
        EventType.PLANNER_RESULT,
    ]


def test_graph_runs_sql_agent_then_stops_before_reviewer() -> None:
    state = create_initial_state("show sales")
    trace = TraceCollector(trace_id=state["trace_id"])
    graph = _graph(trace)

    assert graph.nodes == (
        "start",
        "planner",
        "sql_agent",
        "reviewer",
        "analyst",
        "end",
    )
    with pytest.raises(
        NotImplementedError,
        match="Reviewer and Analyst are not implemented yet",
    ):
        graph.run(state, trace=trace)

    assert state["generated_sql"] == ["SELECT value FROM metrics"]
    assert state["sql_results"][0].success is True
    assert state["completed_tasks"][0].task_id == "task_1"


def test_graph_returns_failed_sql_state_without_entering_review() -> None:
    state = create_initial_state("show sales")
    trace = TraceCollector(trace_id=state["trace_id"])
    graph = _graph(trace, sql="DELETE FROM metrics")

    result = graph.run(state, trace=trace)

    assert result is state
    assert state["sql_results"][0].success is False
    assert state["completed_tasks"] == []
