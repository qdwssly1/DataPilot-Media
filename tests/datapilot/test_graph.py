from __future__ import annotations

import json
from typing import Any

import pytest

from datapilot.agent.graph import build_graph
from datapilot.agent.planner import Planner, PlannerIntent
from datapilot.agent.state import create_initial_state
from datapilot.tracing.trace import EventType, TraceCollector


class StaticPlannerModel:
    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_schema: dict[str, Any],
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


def test_graph_runs_real_planner_node() -> None:
    graph = build_graph(Planner(model_client=StaticPlannerModel()))
    state = create_initial_state("show sales")
    trace = TraceCollector(trace_id=state["trace_id"])

    result = graph.run_planner(state, trace=trace)

    assert result.intent is PlannerIntent.SINGLE_QUERY
    assert state["current_task"] is state["task_plan"][0]
    assert [event.event_type for event in trace.get_events()] == [
        EventType.PLANNER_STARTED,
        EventType.PLANNER_RESULT,
    ]


def test_graph_stops_before_unimplemented_sql_agent() -> None:
    graph = build_graph(Planner(model_client=StaticPlannerModel()))
    state = create_initial_state("show sales")
    trace = TraceCollector(trace_id=state["trace_id"])

    assert graph.nodes == (
        "start",
        "planner",
        "sql_agent",
        "reviewer",
        "analyst",
        "end",
    )
    with pytest.raises(NotImplementedError, match="SQL Agent is not implemented yet"):
        graph.run(state, trace=trace)

    assert [task.task_id for task in state["task_plan"]] == ["task_1"]
