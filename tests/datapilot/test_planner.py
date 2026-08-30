from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

import pytest

from datapilot.agent.planner import (
    SYSTEM_PROMPT,
    Planner,
    PlannerError,
    PlannerIntent,
)
from datapilot.agent.state import SQLResult, TaskItem, create_initial_state
from datapilot.tracing.trace import EventType, TraceCollector


class FakePlannerModel:
    def __init__(self, responses: list[str]) -> None:
        self.responses = iter(responses)
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_schema: Mapping[str, Any],
    ) -> str:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "response_schema": response_schema,
            }
        )
        return next(self.responses)


def _task(
    task_id: str,
    description: str,
    *,
    task_type: str = "query",
    depends_on: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "description": description,
        "task_type": task_type,
        "depends_on": depends_on or [],
        "status": "pending",
    }


def _payload(
    *,
    intent: str,
    tasks: list[dict[str, Any]],
    requires_database: bool,
    requires_context: bool = False,
    is_follow_up: bool = False,
) -> dict[str, Any]:
    return {
        "intent": intent,
        "reason_summary": "The observable request determines this plan.",
        "tasks": tasks,
        "requires_database": requires_database,
        "requires_context": requires_context,
        "is_follow_up": is_follow_up,
    }


def _single_query_payload() -> dict[str, Any]:
    return _payload(
        intent="single_query",
        tasks=[_task("task_1", "Retrieve the requested metric.")],
        requires_database=True,
    )


def _multi_step_payload() -> dict[str, Any]:
    return _payload(
        intent="multi_step_analysis",
        tasks=[
            _task("task_1", "Retrieve the current-period metric."),
            _task(
                "task_2",
                "Compare the metric with the prior period.",
                task_type="analysis",
                depends_on=["task_1"],
            ),
        ],
        requires_database=True,
    )


def _run_plan(
    payloads: list[str],
    *,
    query: str = "Show July sales",
    max_retries: int = 1,
) -> tuple[Any, TraceCollector, FakePlannerModel, Any]:
    state = create_initial_state(query)
    trace = TraceCollector(trace_id=state["trace_id"])
    model = FakePlannerModel(payloads)
    result = Planner(model, max_retries=max_retries).plan(state, trace=trace)
    return result, trace, model, state


def test_planner_simple_question() -> None:
    payload = _payload(
        intent="simple_question",
        tasks=[
            _task(
                "task_1",
                "Prepare a direct response.",
                task_type="response",
            )
        ],
        requires_database=False,
    )

    result, _, _, _ = _run_plan([json.dumps(payload)])

    assert result.intent is PlannerIntent.SIMPLE_QUESTION
    assert result.requires_database is False
    assert result.tasks[0].task_type == "response"


def test_planner_single_query() -> None:
    result, _, model, _ = _run_plan([json.dumps(_single_query_payload())])

    assert result.intent is PlannerIntent.SINGLE_QUERY
    assert result.requires_database is True
    assert [task.task_id for task in result.tasks] == ["task_1"]
    assert len(model.calls) == 1


def test_planner_multi_step_analysis() -> None:
    result, _, _, _ = _run_plan([json.dumps(_multi_step_payload())])

    assert result.intent is PlannerIntent.MULTI_STEP_ANALYSIS
    assert [task.task_type for task in result.tasks] == ["query", "analysis"]
    assert result.tasks[1].depends_on == ["task_1"]


def test_planner_follow_up_detection() -> None:
    payload = _payload(
        intent="follow_up",
        tasks=[_task("task_1", "Retrieve the requested follow-up detail.")],
        requires_database=True,
        requires_context=True,
        is_follow_up=True,
    )

    result, _, _, _ = _run_plan(
        [json.dumps(payload)],
        query="What about the previous month?",
    )

    assert result.intent is PlannerIntent.FOLLOW_UP
    assert result.is_follow_up is True
    assert result.requires_context is True


def test_planner_updates_agent_state() -> None:
    result, _, _, state = _run_plan([json.dumps(_multi_step_payload())])

    assert state["task_plan"] == list(result.tasks)
    assert state["pending_tasks"] == list(result.tasks)
    assert state["pending_tasks"] is not state["task_plan"]
    assert state["current_task"] is state["task_plan"][0]


def test_planner_preserves_sql_fields() -> None:
    state = create_initial_state("Compare sales")
    completed = TaskItem("done_1", "Already completed", status="completed")
    sql_result = SQLResult(
        task_id="done_1",
        sql="existing statement",
        columns=["value"],
        rows=[{"value": 7}],
    )
    state["completed_tasks"] = [completed]
    state["generated_sql"] = ["existing statement"]
    state["sql_results"] = [sql_result]
    trace = TraceCollector(trace_id=state["trace_id"])

    Planner(FakePlannerModel([json.dumps(_single_query_payload())])).plan(
        state,
        trace=trace,
    )

    assert state["completed_tasks"] == [completed]
    assert state["completed_tasks"][0] is completed
    assert state["generated_sql"] == ["existing statement"]
    assert state["sql_results"] == [sql_result]
    assert state["sql_results"][0] is sql_result


def test_planner_task_dependencies() -> None:
    result, _, _, _ = _run_plan([json.dumps(_multi_step_payload())])

    first, second = result.tasks
    assert first.depends_on == []
    assert second.depends_on == [first.task_id]


def test_planner_invalid_json_retry() -> None:
    result, trace, model, _ = _run_plan(
        ["not-json", json.dumps(_single_query_payload())]
    )

    assert result.intent is PlannerIntent.SINGLE_QUERY
    assert len(model.calls) == 2
    assert [event.event_type for event in trace.get_events()] == [
        EventType.PLANNER_STARTED,
        EventType.PLANNER_RETRY,
        EventType.PLANNER_RESULT,
    ]


def test_planner_invalid_json_fails_after_retry() -> None:
    state = create_initial_state("Show sales")
    trace = TraceCollector(trace_id=state["trace_id"])
    model = FakePlannerModel(["not-json", "still-not-json"])

    with pytest.raises(
        PlannerError,
        match="invalid structured output after 2 attempts",
    ):
        Planner(model).plan(state, trace=trace)

    assert len(model.calls) == 2
    assert state["task_plan"] == []
    assert state["pending_tasks"] == []
    assert state["current_task"] is None
    assert [event.event_type for event in trace.get_events()] == [
        EventType.PLANNER_STARTED,
        EventType.PLANNER_RETRY,
        EventType.PLANNER_FAILED,
    ]


def test_planner_allows_at_most_one_retry() -> None:
    with pytest.raises(ValueError, match="max_retries must be 0 or 1"):
        Planner(FakePlannerModel([]), max_retries=2)


def test_planner_trace_events() -> None:
    result, trace, _, _ = _run_plan([json.dumps(_single_query_payload())])
    events = trace.get_events()

    assert [event.event_type for event in events] == [
        EventType.PLANNER_STARTED,
        EventType.PLANNER_RESULT,
    ]
    allowed_metadata = {"intent", "task_count", "retry_count", "duration"}
    assert all(set(event.metadata) <= allowed_metadata for event in events)
    assert events[-1].metadata["intent"] == result.intent.value
    assert events[-1].metadata["task_count"] == 1
    assert events[-1].metadata["retry_count"] == 0
    assert events[-1].metadata["duration"] >= 0


def test_planner_does_not_generate_sql() -> None:
    result, _, model, state = _run_plan([json.dumps(_single_query_payload())])

    assert state["generated_sql"] == []
    assert state["sql_results"] == []
    assert all("sql" not in asdict(task) for task in result.tasks)
    assert "Do not generate SQL" in SYSTEM_PROMPT
    assert "Do not generate SQL" in model.calls[0]["system_prompt"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.pop("reason_summary"),
        lambda payload: payload.update(intent="unsupported"),
        lambda payload: payload.update(tasks=[]),
        lambda payload: payload["tasks"].append(
            _task("task_1", "Duplicate identifier")
        ),
        lambda payload: payload["tasks"][0].update(depends_on=["missing"]),
        lambda payload: payload["tasks"][0].update(task_type=[]),
        lambda payload: payload["tasks"][0].update(sql="SELECT forbidden"),
    ],
    ids=[
        "missing-field",
        "illegal-intent",
        "empty-tasks",
        "duplicate-task-id",
        "invalid-dependency",
        "invalid-task-type",
        "unexpected-sql-field",
    ],
)
def test_planner_rejects_invalid_structured_output(mutate: Any) -> None:
    payload = _single_query_payload()
    mutate(payload)
    state = create_initial_state("Show sales")
    trace = TraceCollector(trace_id=state["trace_id"])

    with pytest.raises(PlannerError, match="invalid structured output"):
        Planner(
            FakePlannerModel([json.dumps(payload)]),
            max_retries=0,
        ).plan(state, trace=trace)

    assert state["task_plan"] == []
    assert state["pending_tasks"] == []
    assert state["current_task"] is None
