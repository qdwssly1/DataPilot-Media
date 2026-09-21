from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from datapilot.agent.analyst import Analyst
from datapilot.agent.graph import build_graph, execute_task_plan, get_next_ready_task
from datapilot.agent.planner import Planner, PlannerIntent
from datapilot.agent.reviewer import Reviewer
from datapilot.agent.sql_agent import SQLAgent
from datapilot.agent.state import TaskItem, create_initial_state
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


class SequenceSQLModel:
    def __init__(self, sql_statements: list[str]) -> None:
        self.sql_statements = list(sql_statements)

    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_schema: Mapping[str, Any],
    ) -> str:
        del system_prompt, user_prompt, response_schema
        sql = self.sql_statements.pop(0)
        return json.dumps({"sql": sql, "summary": "Retrieve grouped GMV."})


class StaticReviewerModel:
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
                "decision": "approve",
                "reason_summary": "The result supports the task.",
                "issues": [],
                "retry_instruction": None,
                "confidence": 0.95,
            }
        )


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

    def store_query(
        self,
        nl: str,
        sql: str,
        *,
        tags: list[str] | None = None,
    ) -> None:
        del nl, sql, tags


class ComparisonWrenTools(FakeWrenTools):
    def query(self, sql: str, *, limit: int = 100) -> WrenQueryResult:
        del limit
        rows = (
            [{"category": "A", "gmv": 100}, {"category": "B", "gmv": 200}]
            if "Q2" in sql
            else [{"category": "A", "gmv": 80}, {"category": "B", "gmv": 240}]
        )
        return WrenQueryResult(
            columns=["category", "gmv"],
            rows=rows,
            row_count=2,
        )


class FinalAnswerModel:
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
                "data_evidence_ids": [
                    "data:analysis-comparison:1:group:1",
                    "data:analysis-comparison:1:group:2",
                ],
                "knowledge_evidence_ids": [],
                "inferences": [
                    {
                        "bundle_id": "bundle:multi_group:ALL:category",
                        "claim_type": "observation",
                        "predicate": "general",
                        "polarity": "neutral",
                        "subject_evidence_ids": [],
                        "supporting_evidence_ids": [
                            "data:analysis-comparison:1:group:1",
                            "data:analysis-comparison:1:group:2",
                        ],
                    }
                ],
                "limitation_ids": [
                    "limitation:bounded_evidence",
                    "limitation:limited_time_windows",
                ],
                "source_task_ids": ["decline"],
            }
        )


def _multi_step_state() -> tuple[Any, list[TaskItem]]:
    state = create_initial_state("Compare Q2 and Q3 category GMV")
    tasks = [
        TaskItem("q2", "Retrieve Q2 category GMV", "query"),
        TaskItem("q3", "Retrieve Q3 category GMV", "query"),
        TaskItem("compare", "Compare Q2 and Q3", "analysis", ["q2", "q3"]),
        TaskItem("decline", "Find largest decline", "analysis", ["compare"]),
        TaskItem("response", "Answer the user", "response", ["decline"]),
    ]
    state["task_plan"] = tasks
    state["pending_tasks"] = list(tasks)
    state["current_task"] = tasks[0]
    return state, tasks


def _multi_step_components(trace: TraceCollector) -> tuple[Any, Any, Any]:
    tools = ComparisonWrenTools()
    sql_agent = SQLAgent(
        model_client=SequenceSQLModel(
            [
                "SELECT category, gmv FROM orders WHERE quarter = 'Q2'",
                "SELECT category, gmv FROM orders WHERE quarter = 'Q3'",
            ]
        ),
        wren_tools=tools,
        trace=trace,
        max_attempts=1,
    )
    reviewer = Reviewer(model_client=StaticReviewerModel(), trace=trace)
    analyst = Analyst(model_client=FinalAnswerModel(), trace=trace)
    return sql_agent, reviewer, analyst


def _graph(trace: TraceCollector, *, sql: str = "SELECT value FROM metrics") -> Any:
    return build_graph(
        Planner(model_client=StaticPlannerModel()),
        SQLAgent(
            model_client=StaticSQLModel(sql),
            wren_tools=FakeWrenTools(),
            trace=trace,
            max_attempts=1,
        ),
        Reviewer(model_client=StaticReviewerModel(), trace=trace),
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


def test_graph_runs_reviewer_and_finishes_available_tasks() -> None:
    state = create_initial_state("show sales")
    trace = TraceCollector(trace_id=state["trace_id"])
    graph = _graph(trace)

    assert graph.nodes == (
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
    result = graph.run(state, trace=trace)

    assert result is state
    assert state["generated_sql"] == ["SELECT value FROM metrics"]
    assert state["sql_results"][0].success is True
    assert state["completed_tasks"][0].task_id == "task_1"
    assert state["review_result"].decision == "approve"


def test_graph_returns_failed_sql_state_without_entering_review() -> None:
    state = create_initial_state("show sales")
    trace = TraceCollector(trace_id=state["trace_id"])
    graph = _graph(trace, sql="DELETE FROM metrics")

    result = graph.run(state, trace=trace)

    assert result is state
    assert state["sql_results"][0].success is False
    assert state["completed_tasks"] == []


def test_get_next_ready_task_uses_plan_order() -> None:
    state, tasks = _multi_step_state()

    assert get_next_ready_task(state) is tasks[0]
    tasks[0].status = "completed"
    state["completed_tasks"].append(tasks[0])
    state["pending_tasks"].remove(tasks[0])

    assert get_next_ready_task(state) is tasks[1]


def test_multi_query_multi_analysis_workflow() -> None:
    state, tasks = _multi_step_state()
    trace = TraceCollector(trace_id=state["trace_id"])
    sql_agent, reviewer, analyst = _multi_step_components(trace)

    run = execute_task_plan(
        state,
        sql_agent,
        reviewer,
        analyst,
        trace=trace,
    )

    assert len(run.reviewed_queries) == 2
    assert len(run.analysis_results) == 2
    assert run.final_answer_result is not None
    assert all(task.status == "completed" for task in tasks)
    assert state["analysis_results"][0].derived_values["largest_decline"]["key"] == "A"


def test_graph_dispatches_query_analysis_response() -> None:
    state, _ = _multi_step_state()
    trace = TraceCollector(trace_id=state["trace_id"])
    sql_agent, reviewer, analyst = _multi_step_components(trace)

    execute_task_plan(state, sql_agent, reviewer, analyst, trace=trace)

    event_types = [event.event_type for event in trace.get_events()]
    assert event_types.count(EventType.SQL_AGENT_STARTED) == 2
    assert event_types.count(EventType.REVIEW_APPROVED) == 2
    assert event_types.count(EventType.ANALYST_COMPLETED) == 2
    assert event_types.count(EventType.FINAL_ANSWER_COMPLETED) == 1


def test_graph_reaches_end_after_response() -> None:
    state, tasks = _multi_step_state()
    trace = TraceCollector(trace_id=state["trace_id"])
    sql_agent, reviewer, analyst = _multi_step_components(trace)

    execute_task_plan(state, sql_agent, reviewer, analyst, trace=trace)

    assert state["pending_tasks"] == []
    assert state["current_task"] is None
    assert state["final_answer"] is not None
    assert tasks[-1].status == "completed"


def test_graph_stops_on_failed_dependency() -> None:
    state, tasks = _multi_step_state()
    trace = TraceCollector(trace_id=state["trace_id"])
    tools = ComparisonWrenTools()
    sql_agent = SQLAgent(
        model_client=SequenceSQLModel(["DELETE FROM orders"]),
        wren_tools=tools,
        trace=trace,
        max_attempts=1,
    )

    run = execute_task_plan(
        state,
        sql_agent,
        Reviewer(model_client=StaticReviewerModel(), trace=trace),
        Analyst(model_client=FinalAnswerModel(), trace=trace),
        trace=trace,
    )

    assert run.reviewed_queries[0].approved is False
    assert tasks[0].status == "failed"
    assert all(task.status == "pending" for task in tasks[1:])
    assert state["final_answer"] is None
