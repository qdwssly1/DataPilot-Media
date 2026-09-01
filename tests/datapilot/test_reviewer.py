from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from datapilot.agent.graph import execute_ready_query_tasks
from datapilot.agent.reviewer import Reviewer, ReviewerOutputError
from datapilot.agent.sql_agent import SQLAgent, is_task_ready
from datapilot.agent.state import SQLResult, TaskItem, create_initial_state
from datapilot.tools.wren_tools import WrenQueryResult
from datapilot.tracing.trace import EventType, TraceCollector


class FakeModel:
    def __init__(self, responses: list[str | Exception]) -> None:
        self.responses = list(responses)
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
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeWrenTools:
    def __init__(self, *, store_error: Exception | None = None) -> None:
        self.store_error = store_error
        self.store_calls: list[tuple[str, str, list[str] | None]] = []
        self.calls: list[str] = []

    def fetch_context(self, question: str, *, limit: int = 5) -> dict[str, Any]:
        del question, limit
        self.calls.append("fetch_context")
        return {
            "strategy": "full",
            "schema": "orders(order_date date, category varchar, gmv decimal)",
        }

    def recall_queries(
        self,
        question: str,
        *,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        del question, limit
        self.calls.append("recall_queries")
        return []

    def dry_plan(self, sql: str) -> str:
        self.calls.append("dry_plan")
        return f"planned: {sql}"

    def query(self, sql: str, *, limit: int = 100) -> WrenQueryResult:
        del limit
        self.calls.append("query")
        if "category" in sql.lower():
            return WrenQueryResult(
                columns=["category", "gmv"],
                rows=[{"category": "A", "gmv": 42}],
                row_count=1,
            )
        return WrenQueryResult(
            columns=["gmv"],
            rows=[{"gmv": 42}],
            row_count=1,
        )

    def store_query(
        self,
        nl: str,
        sql: str,
        *,
        tags: list[str] | None = None,
    ) -> None:
        self.store_calls.append((nl, sql, tags))
        if self.store_error is not None:
            raise self.store_error


def _review_response(
    decision: str,
    *,
    issue_type: str | None = None,
    description: str = "The query does not match the task.",
    instruction: str | None = None,
    confidence: float = 0.95,
) -> str:
    issues = (
        [{"issue_type": issue_type, "description": description}]
        if issue_type
        else []
    )
    return json.dumps(
        {
            "decision": decision,
            "reason_summary": (
                "The SQL result supports the task."
                if decision == "approve"
                else description
            ),
            "issues": issues,
            "retry_instruction": instruction,
            "confidence": confidence,
        }
    )


def _sql_response(sql: str) -> str:
    return json.dumps({"sql": sql, "summary": "Execute the requested query."})


def _query_task(
    task_id: str = "task_1",
    *,
    description: str = "Calculate July 2026 GMV.",
    depends_on: list[str] | None = None,
) -> TaskItem:
    return TaskItem(
        task_id=task_id,
        description=description,
        task_type="query",
        depends_on=depends_on or [],
    )


def _state_for(*tasks: TaskItem) -> tuple[Any, TraceCollector]:
    state = create_initial_state("Calculate July 2026 GMV by category.")
    state["task_plan"] = list(tasks)
    state["pending_tasks"] = list(tasks)
    state["current_task"] = tasks[0] if tasks else None
    trace = TraceCollector(trace_id=state["trace_id"])
    return state, trace


def _executed_state() -> tuple[Any, TaskItem, SQLResult, TraceCollector]:
    task = _query_task()
    state, trace = _state_for(task)
    task.status = "executed"
    result = SQLResult(
        task_id=task.task_id,
        sql=(
            "SELECT SUM(gmv) AS gmv FROM orders "
            "WHERE order_date >= DATE '2026-07-01' "
            "AND order_date < DATE '2026-08-01'"
        ),
        columns=["gmv"],
        rows=[{"gmv": 42}],
        row_count=1,
        context_summary="orders(order_date, category, gmv)",
    )
    state["generated_sql"] = [result.sql]
    state["sql_results"] = [result]
    state["business_context"].definitions[f"wren_context:{task.task_id}"] = (
        result.context_summary
    )
    return state, task, result, trace


def test_reviewer_approves_semantically_correct_query() -> None:
    state, task, result, trace = _executed_state()

    review = Reviewer(
        model_client=FakeModel([_review_response("approve")])
    ).review(state, task, result, trace=trace)

    assert review.decision == "approve"
    assert review.issues == []
    assert review.retry_instruction is None


def test_reviewer_detects_metric_mismatch() -> None:
    state, task, result, trace = _executed_state()
    response = _review_response(
        "retry",
        issue_type="metric_mismatch",
        description="COUNT(*) does not calculate GMV.",
        instruction="Use SUM(gmv) for the requested date range.",
    )

    review = Reviewer(model_client=FakeModel([response])).review(
        state,
        task,
        result,
        trace=trace,
    )

    assert review.issues[0].issue_type == "metric_mismatch"
    assert review.retry_instruction == "Use SUM(gmv) for the requested date range."


def test_reviewer_detects_dimension_mismatch() -> None:
    state, task, result, trace = _executed_state()
    response = _review_response(
        "retry",
        issue_type="dimension_mismatch",
        description="The result omits product category.",
        instruction="Select category and group the GMV aggregation by category.",
    )

    review = Reviewer(model_client=FakeModel([response])).review(
        state,
        task,
        result,
        trace=trace,
    )

    assert review.issues[0].issue_type == "dimension_mismatch"


def test_reviewer_limits_sample_rows_in_prompt() -> None:
    state, task, result, trace = _executed_state()
    result.rows = [{"row_number": index} for index in range(12)]
    result.row_count = 12
    model = FakeModel([_review_response("approve")])

    Reviewer(model_client=model, sample_row_limit=5).review(
        state,
        task,
        result,
        trace=trace,
    )

    prompt = model.calls[0]["user_prompt"]
    assert '"row_number":4' in prompt
    assert '"row_number":5' not in prompt


def test_reviewer_structured_output_retry() -> None:
    state, task, result, trace = _executed_state()
    model = FakeModel(["not-json", _review_response("approve")])

    review = Reviewer(model_client=model).review(
        state,
        task,
        result,
        trace=trace,
    )

    assert review.decision == "approve"
    assert len(model.calls) == 2
    assert EventType.REVIEW_OUTPUT_RETRY in {
        event.event_type for event in trace.get_events()
    }


def test_reviewer_fails_after_invalid_output_retry() -> None:
    state, task, result, trace = _executed_state()

    with pytest.raises(ReviewerOutputError, match="review fields invalid"):
        Reviewer(model_client=FakeModel(["not-json", '{"decision":"approve"}'])).review(
            state,
            task,
            result,
            trace=trace,
        )

    assert task.status == "failed"
    assert state["completed_tasks"] == []
    assert trace.get_events()[-1].event_type is EventType.REVIEW_FAILED


@pytest.mark.parametrize(
    "payload",
    [
        {
            "decision": "approve",
            "reason_summary": "Contradictory approval.",
            "issues": [
                {"issue_type": "other", "description": "Unexpected issue."}
            ],
            "retry_instruction": None,
            "confidence": 0.8,
        },
        {
            "decision": "retry",
            "reason_summary": "Retry needs an instruction.",
            "issues": [
                {"issue_type": "other", "description": "Needs correction."}
            ],
            "retry_instruction": None,
            "confidence": 0.8,
        },
        {
            "decision": "fail",
            "reason_summary": "Fail cannot request retry.",
            "issues": [
                {"issue_type": "missing_data", "description": "Missing data."}
            ],
            "retry_instruction": "Try again.",
            "confidence": 0.8,
        },
        {
            "decision": "approve",
            "reason_summary": "Invalid confidence.",
            "issues": [],
            "retry_instruction": None,
            "confidence": 1.5,
        },
    ],
)
def test_reviewer_rejects_contradictory_structured_decisions(
    payload: dict[str, Any],
) -> None:
    state, task, result, trace = _executed_state()

    with pytest.raises(ReviewerOutputError):
        Reviewer(
            model_client=FakeModel([json.dumps(payload)]),
            max_output_retries=0,
        ).review(state, task, result, trace=trace)


def test_reviewer_updates_state_on_approve() -> None:
    state, task, result, trace = _executed_state()

    review = Reviewer(
        model_client=FakeModel([_review_response("approve")])
    ).review(state, task, result, trace=trace)

    assert task.status == "completed"
    assert state["completed_tasks"] == [task]
    assert state["pending_tasks"] == []
    assert state["review_result"] is review
    assert state["review_results"] == [review]


def test_reviewer_does_not_complete_task_on_retry() -> None:
    state, task, result, trace = _executed_state()
    response = _review_response(
        "retry",
        issue_type="metric_mismatch",
        instruction="Use SUM(gmv).",
    )

    Reviewer(model_client=FakeModel([response])).review(
        state,
        task,
        result,
        trace=trace,
    )

    assert task.status == "pending"
    assert state["completed_tasks"] == []
    assert state["pending_tasks"] == [task]


def test_reviewer_does_not_complete_task_on_fail() -> None:
    state, task, result, trace = _executed_state()
    response = _review_response(
        "fail",
        issue_type="missing_data",
        description="The required metric is unavailable in the context.",
    )

    Reviewer(model_client=FakeModel([response])).review(
        state,
        task,
        result,
        trace=trace,
    )

    assert task.status == "failed"
    assert state["completed_tasks"] == []
    assert state["pending_tasks"] == []


def test_dependency_requires_review_approval() -> None:
    first = _query_task("task_1")
    second = _query_task("task_2", depends_on=["task_1"])
    state, trace = _state_for(first, second)
    first.status = "executed"
    result = SQLResult("task_1", "SELECT SUM(gmv) FROM orders")

    assert is_task_ready(state, second) is False

    Reviewer(model_client=FakeModel([_review_response("approve")])).review(
        state,
        first,
        result,
        trace=trace,
    )

    assert is_task_ready(state, second) is True


def test_reviewer_feedback_reaches_sql_agent() -> None:
    task = _query_task()
    state, trace = _state_for(task)
    sql_model = FakeModel(
        [
            _sql_response("SELECT COUNT(*) FROM orders"),
            _sql_response("SELECT SUM(gmv) AS gmv FROM orders"),
        ]
    )
    reviewer_model = FakeModel(
        [
            _review_response(
                "retry",
                issue_type="metric_mismatch",
                description="COUNT(*) is not GMV.",
                instruction="Replace COUNT(*) with SUM(gmv).",
            ),
            _review_response("approve"),
        ]
    )

    execute_ready_query_tasks(
        state,
        SQLAgent(model_client=sql_model, wren_tools=FakeWrenTools()),
        Reviewer(model_client=reviewer_model),
        trace=trace,
    )

    correction_prompt = sql_model.calls[1]["user_prompt"]
    assert "COUNT(*) is not GMV" in correction_prompt
    assert "Replace COUNT(*) with SUM(gmv)" in correction_prompt
    assert "SELECT COUNT(*) FROM orders" in correction_prompt
    assert "metric_mismatch" in reviewer_model.calls[1]["user_prompt"]


def test_semantic_retry_generates_new_sql() -> None:
    task = _query_task()
    state, trace = _state_for(task)
    wrong_sql = "SELECT COUNT(*) FROM orders"
    corrected_sql = "SELECT SUM(gmv) AS gmv FROM orders"
    reviewer_model = FakeModel(
        [
            _review_response(
                "retry",
                issue_type="metric_mismatch",
                instruction="Use SUM(gmv).",
            ),
            _review_response("approve"),
        ]
    )

    runs = execute_ready_query_tasks(
        state,
        SQLAgent(
            model_client=FakeModel(
                [_sql_response(wrong_sql), _sql_response(corrected_sql)]
            ),
            wren_tools=FakeWrenTools(),
        ),
        Reviewer(model_client=reviewer_model),
        trace=trace,
    )

    assert [item.sql for item in runs[0].sql_results] == [wrong_sql, corrected_sql]
    assert runs[0].sql_results[0].semantic_retry_count == 0
    assert runs[0].sql_results[1].semantic_retry_count == 1
    assert runs[0].sql_results[1].retry_count == 0


def test_semantic_retry_is_bounded() -> None:
    task = _query_task()
    state, trace = _state_for(task)
    sql_model = FakeModel(
        [_sql_response("SELECT COUNT(*) FROM orders"), _sql_response("SELECT 1")]
    )
    retry = _review_response(
        "retry",
        issue_type="metric_mismatch",
        instruction="Use SUM(gmv).",
    )

    runs = execute_ready_query_tasks(
        state,
        SQLAgent(model_client=sql_model, wren_tools=FakeWrenTools()),
        Reviewer(model_client=FakeModel([retry, retry])),
        trace=trace,
    )

    assert len(runs[0].sql_results) == 2
    assert len(sql_model.calls) == 2
    assert task.status == "failed"
    assert trace.get_events()[-1].metadata["error_type"] == (
        "ReviewerRetryLimitError"
    )


def test_second_review_failure_stops_workflow() -> None:
    first = _query_task("task_1")
    second = _query_task("task_2")
    state, trace = _state_for(first, second)
    retry = _review_response(
        "retry",
        issue_type="metric_mismatch",
        instruction="Use SUM(gmv).",
    )
    fail = _review_response(
        "fail",
        issue_type="missing_data",
        description="Required data remains unavailable.",
    )

    runs = execute_ready_query_tasks(
        state,
        SQLAgent(
            model_client=FakeModel(
                [_sql_response("SELECT 1"), _sql_response("SELECT 2")]
            ),
            wren_tools=FakeWrenTools(),
        ),
        Reviewer(model_client=FakeModel([retry, fail])),
        trace=trace,
    )

    assert len(runs) == 1
    assert first.status == "failed"
    assert second.status == "pending"
    assert state["completed_tasks"] == []


def test_review_approve_allows_memory_store() -> None:
    task = _query_task()
    state, trace = _state_for(task)
    tools = FakeWrenTools()

    execute_ready_query_tasks(
        state,
        SQLAgent(
            model_client=FakeModel([_sql_response("SELECT SUM(gmv) FROM orders")]),
            wren_tools=tools,
        ),
        Reviewer(model_client=FakeModel([_review_response("approve")])),
        trace=trace,
    )

    assert tools.store_calls == [
        (
            task.description,
            "SELECT SUM(gmv) FROM orders",
            ["datapilot-reviewed"],
        )
    ]


def test_review_reject_does_not_store_memory() -> None:
    task = _query_task()
    state, trace = _state_for(task)
    tools = FakeWrenTools()
    fail = _review_response(
        "fail",
        issue_type="result_mismatch",
        description="The result cannot support the task.",
    )

    execute_ready_query_tasks(
        state,
        SQLAgent(
            model_client=FakeModel([_sql_response("SELECT 1")]),
            wren_tools=tools,
        ),
        Reviewer(model_client=FakeModel([fail])),
        trace=trace,
    )

    assert tools.store_calls == []


def test_memory_store_failure_is_nonfatal() -> None:
    task = _query_task()
    state, trace = _state_for(task)
    tools = FakeWrenTools(store_error=RuntimeError("memory is disabled"))

    runs = execute_ready_query_tasks(
        state,
        SQLAgent(
            model_client=FakeModel([_sql_response("SELECT SUM(gmv) FROM orders")]),
            wren_tools=tools,
        ),
        Reviewer(model_client=FakeModel([_review_response("approve")])),
        trace=trace,
    )

    assert runs[0].approved is True
    assert task.status == "completed"
    assert EventType.SQL_MEMORY_STORE_FAILED in {
        event.event_type for event in trace.get_events()
    }


def test_multi_query_each_query_is_reviewed() -> None:
    first = _query_task("task_1", description="Calculate Q2 GMV.")
    second = _query_task("task_2", description="Calculate Q3 GMV.")
    analysis = TaskItem(
        "task_3",
        "Compare Q2 and Q3.",
        task_type="analysis",
        depends_on=["task_1", "task_2"],
    )
    state, trace = _state_for(first, second, analysis)
    reviewer_model = FakeModel(
        [_review_response("approve"), _review_response("approve")]
    )

    runs = execute_ready_query_tasks(
        state,
        SQLAgent(
            model_client=FakeModel(
                [_sql_response("SELECT 2"), _sql_response("SELECT 3")]
            ),
            wren_tools=FakeWrenTools(),
        ),
        Reviewer(model_client=reviewer_model),
        trace=trace,
    )

    assert [run.task_id for run in runs] == ["task_1", "task_2"]
    assert len(reviewer_model.calls) == 2
    assert state["completed_tasks"] == [first, second]
    assert state["current_task"] is analysis


def test_analysis_task_is_not_reviewed() -> None:
    analysis = TaskItem("task_1", "Compare results.", task_type="analysis")
    state, trace = _state_for(analysis)
    reviewer_model = FakeModel([])

    runs = execute_ready_query_tasks(
        state,
        SQLAgent(model_client=FakeModel([]), wren_tools=FakeWrenTools()),
        Reviewer(model_client=reviewer_model),
        trace=trace,
    )

    assert runs == ()
    assert reviewer_model.calls == []


def test_reviewer_trace_approve() -> None:
    state, task, result, trace = _executed_state()

    Reviewer(model_client=FakeModel([_review_response("approve")])).review(
        state,
        task,
        result,
        trace=trace,
    )

    assert [event.event_type for event in trace.get_events()] == [
        EventType.REVIEW_STARTED,
        EventType.REVIEW_RESULT,
        EventType.REVIEW_APPROVED,
    ]


def test_reviewer_trace_retry() -> None:
    state, task, result, trace = _executed_state()
    response = _review_response(
        "retry",
        issue_type="metric_mismatch",
        instruction="Use SUM(gmv).",
    )

    Reviewer(model_client=FakeModel([response])).review(
        state,
        task,
        result,
        trace=trace,
    )

    assert trace.get_events()[-1].event_type is EventType.REVIEW_RETRY_REQUESTED
    assert trace.get_events()[-1].metadata["issue_types"] == ["metric_mismatch"]


def test_reviewer_trace_fail() -> None:
    state, task, result, trace = _executed_state()
    response = _review_response(
        "fail",
        issue_type="missing_data",
        description="The required source is unavailable.",
    )

    Reviewer(model_client=FakeModel([response])).review(
        state,
        task,
        result,
        trace=trace,
    )

    assert trace.get_events()[-1].event_type is EventType.REVIEW_FAILED
    allowed = {
        "task_id",
        "decision",
        "issue_types",
        "review_retry_count",
        "duration",
        "confidence",
        "error_type",
    }
    assert all(set(event.metadata) <= allowed for event in trace.get_events())
