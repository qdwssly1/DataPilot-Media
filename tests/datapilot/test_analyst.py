from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import pytest

from datapilot.agent.analyst import (
    Analyst,
    AnalystError,
    AnalystOutputError,
    compare_grouped_metrics,
)
from datapilot.agent.state import (
    AnalysisResult,
    ReviewerResult,
    SQLResult,
    TaskItem,
    create_initial_state,
)
from datapilot.tracing.trace import EventType, TraceCollector


class FakeAnalystModel:
    def __init__(self, responses: list[str | Exception] | None = None) -> None:
        self.responses = list(responses or [])
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


def _trace(state: Any) -> TraceCollector:
    return TraceCollector(trace_id=state["trace_id"])


def _approve_query(
    state: Any,
    task: TaskItem,
    rows: list[dict[str, Any]],
    columns: list[str],
) -> None:
    task.status = "completed"
    state["completed_tasks"].append(task)
    state["sql_results"].append(
        SQLResult(
            task_id=task.task_id,
            sql=f"SELECT * FROM {task.task_id}",
            columns=columns,
            rows=rows,
            row_count=len(rows),
        )
    )
    state["review_results"].append(
        ReviewerResult(
            task_id=task.task_id,
            decision="approve",
            reason_summary="Verified.",
            confidence=1.0,
        )
    )


def _comparison_state() -> tuple[Any, TaskItem]:
    state = create_initial_state("Compare Q2 and Q3 category GMV")
    q2 = TaskItem("q2", "Q2 category GMV", "query")
    q3 = TaskItem("q3", "Q3 category GMV", "query")
    analysis = TaskItem(
        "compare",
        "Compare Q2 and Q3",
        "analysis",
        ["q2", "q3"],
    )
    state["task_plan"] = [q2, q3, analysis]
    state["pending_tasks"] = [analysis]
    state["current_task"] = analysis
    _approve_query(
        state,
        q2,
        [{"category": "A", "gmv": 100}, {"category": "B", "gmv": 200}],
        ["category", "gmv"],
    )
    _approve_query(
        state,
        q3,
        [{"category": "A", "gmv": 80}, {"category": "B", "gmv": 240}],
        ["category", "gmv"],
    )
    return state, analysis


def _single_query_state(*, reviewed: bool = True) -> tuple[Any, TaskItem]:
    state = create_initial_state("Summarize the verified metric")
    query = TaskItem("query", "Retrieve metric", "query", status="completed")
    analysis = TaskItem("analysis", "Summarize metric", "analysis", ["query"])
    state["task_plan"] = [query, analysis]
    state["completed_tasks"] = [query]
    state["pending_tasks"] = [analysis]
    state["sql_results"] = [
        SQLResult(
            task_id="query",
            sql="SELECT 42 AS value",
            columns=["value"],
            rows=[{"value": 42}],
            row_count=1,
        )
    ]
    if reviewed:
        state["review_results"] = [
            ReviewerResult(
                task_id="query",
                decision="approve",
                reason_summary="Verified.",
                confidence=1.0,
            )
        ]
    return state, analysis


def _valid_analysis_json() -> str:
    return json.dumps(
        {
            "summary": "The verified metric is 42.",
            "findings": ["value=42"],
            "source_task_ids": ["query"],
        }
    )


def _valid_answer_json(source_id: str = "query") -> str:
    return json.dumps(
        {
            "answer": "The verified value is 42.",
            "key_findings": ["value=42"],
            "source_task_ids": [source_id],
        }
    )


def test_analyst_requires_approved_dependencies() -> None:
    state = create_initial_state("compare")
    query = TaskItem("query", "Retrieve data", "query")
    analysis = TaskItem("analysis", "Analyze data", "analysis", ["query"])
    state["task_plan"] = [query, analysis]
    state["pending_tasks"] = [query, analysis]

    with pytest.raises(AnalystError, match="dependencies are not completed"):
        Analyst(model_client=FakeAnalystModel()).execute_task(
            state,
            analysis,
            trace=_trace(state),
        )

    assert analysis.status == "failed"


def test_analyst_rejects_unreviewed_sql_result() -> None:
    state, analysis = _single_query_state(reviewed=False)

    with pytest.raises(AnalystError, match="not Reviewer-approved"):
        Analyst(model_client=FakeAnalystModel()).execute_task(
            state,
            analysis,
            trace=_trace(state),
        )


def test_analyst_executes_analysis_task() -> None:
    state, task = _comparison_state()
    model = FakeAnalystModel()

    result = Analyst(model_client=model).execute_task(
        state,
        task,
        trace=_trace(state),
    )

    assert result.success is True
    assert result.source_task_ids == ["q2", "q3"]
    assert model.calls == []


def test_analyst_does_not_execute_query_task() -> None:
    state = create_initial_state("query")
    query = TaskItem("query", "Retrieve data", "query")
    state["task_plan"] = [query]
    state["pending_tasks"] = [query]

    with pytest.raises(AnalystError, match="not a pending analysis"):
        Analyst(model_client=FakeAnalystModel()).execute_task(
            state,
            query,
            trace=_trace(state),
        )


def test_grouped_metric_comparison() -> None:
    result = compare_grouped_metrics(
        [{"category": "A", "gmv": 100}, {"category": "B", "gmv": 200}],
        [{"category": "A", "gmv": 80}, {"category": "B", "gmv": 240}],
        dimension_column="category",
        metric_column="gmv",
        left_label="Q2",
        right_label="Q3",
    )

    assert result["comparison"][0] == {
        "key": "A",
        "Q2": 100.0,
        "Q3": 80.0,
        "difference": -20.0,
        "growth_rate": -0.2,
    }


def test_grouped_metric_comparison_accepts_period_aliases() -> None:
    result = compare_grouped_metrics(
        [{"category": "A", "q2_gmv": 100}],
        [{"category": "A", "q3_gmv": 80}],
        dimension_column="category",
        metric_column="q2_gmv",
        right_metric_column="q3_gmv",
        left_label="Q2",
        right_label="Q3",
    )

    assert result["metric_column"] == "gmv"
    assert result["comparison"][0]["difference"] == -20
    assert result["comparison"][0]["growth_rate"] == pytest.approx(-0.2)


def test_analyst_infers_different_metric_aliases() -> None:
    state = create_initial_state("Compare Q2 and Q3 category GMV")
    q2 = TaskItem("q2", "Q2 category GMV", "query")
    q3 = TaskItem("q3", "Q3 category GMV", "query")
    analysis = TaskItem("compare", "Compare periods", "analysis", ["q2", "q3"])
    state["task_plan"] = [q2, q3, analysis]
    state["pending_tasks"] = [analysis]
    _approve_query(
        state,
        q2,
        [{"category": "A", "q2_gmv": Decimal("100")}],
        ["category", "q2_gmv"],
    )
    _approve_query(
        state,
        q3,
        [{"category": "A", "q3_gmv": Decimal("80")}],
        ["category", "q3_gmv"],
    )

    result = Analyst(model_client=FakeAnalystModel()).execute_task(
        state,
        analysis,
        trace=_trace(state),
    )

    assert result.derived_values["comparison"][0]["difference"] == -20
    assert result.derived_values["largest_decline"]["key"] == "A"


def test_analyst_rejects_ambiguous_metric_columns() -> None:
    state = create_initial_state("Compare Q2 and Q3 category GMV")
    q2 = TaskItem("q2", "Q2 category GMV", "query")
    q3 = TaskItem("q3", "Q3 category GMV", "query")
    analysis = TaskItem("compare", "Compare periods", "analysis", ["q2", "q3"])
    state["task_plan"] = [q2, q3, analysis]
    state["pending_tasks"] = [analysis]
    _approve_query(
        state,
        q2,
        [{"category": "A", "gmv": 250}],
        ["category", "gmv"],
    )
    _approve_query(
        state,
        q3,
        [{"category": "A", "q2_gmv": 250, "q3_gmv": 200}],
        ["category", "q2_gmv", "q3_gmv"],
    )
    model = FakeAnalystModel()

    with pytest.raises(AnalystError, match="ambiguous metric columns"):
        Analyst(model_client=model).execute_task(
            state,
            analysis,
            trace=_trace(state),
        )

    assert model.calls == []
    assert state["analysis_results"][-1].success is False
    assert analysis.status == "failed"


def test_analyst_does_not_silently_choose_wrong_period() -> None:
    state = create_initial_state("Compare Q2 and Q3 category GMV")
    q2 = TaskItem("q2", "Q2 category GMV", "query")
    q3 = TaskItem("q3", "Q3 category GMV", "query")
    analysis = TaskItem("compare", "Compare periods", "analysis", ["q2", "q3"])
    state["task_plan"] = [q2, q3, analysis]
    state["pending_tasks"] = [analysis]
    _approve_query(
        state,
        q2,
        [{"category": "A", "gmv": 250}, {"category": "B", "gmv": 300}],
        ["category", "gmv"],
    )
    _approve_query(
        state,
        q3,
        [
            {"category": "A", "q2_gmv": 250, "q3_gmv": 200, "decline": 50},
            {"category": "B", "q2_gmv": 300, "q3_gmv": 370, "decline": -70},
        ],
        ["category", "q2_gmv", "q3_gmv", "decline"],
    )

    with pytest.raises(AnalystError, match="source result contract mismatch"):
        Analyst(model_client=FakeAnalystModel()).execute_task(
            state,
            analysis,
            trace=_trace(state),
        )

    assert state["final_answer"] is None
    assert state["analysis_results"][-1].derived_values == {}


def test_real_totals_deterministic_comparison() -> None:
    result = compare_grouped_metrics(
        [{"category": "A", "gmv": 250}, {"category": "B", "gmv": 300}],
        [{"category": "A", "gmv": 200}, {"category": "B", "gmv": 370}],
        dimension_column="category",
        metric_column="gmv",
        left_label="Q2",
        right_label="Q3",
    )

    by_category = {item["key"]: item for item in result["comparison"]}
    assert by_category["A"] == {
        "key": "A",
        "Q2": 250.0,
        "Q3": 200.0,
        "difference": -50.0,
        "growth_rate": -0.2,
    }
    assert by_category["B"]["difference"] == 70
    assert by_category["B"]["growth_rate"] == pytest.approx(70 / 300)
    assert result["largest_decline"]["key"] == "A"


def test_growth_rate_calculation() -> None:
    state, task = _comparison_state()

    result = Analyst(model_client=FakeAnalystModel()).execute_task(
        state,
        task,
        trace=_trace(state),
    )

    by_category = {
        item["key"]: item for item in result.derived_values["comparison"]
    }
    assert by_category["A"]["growth_rate"] == pytest.approx(-0.2)
    assert by_category["B"]["growth_rate"] == pytest.approx(0.2)


def test_largest_decline_detection() -> None:
    state, task = _comparison_state()

    result = Analyst(model_client=FakeAnalystModel()).execute_task(
        state,
        task,
        trace=_trace(state),
    )

    assert result.derived_values["largest_decline"]["key"] == "A"
    assert result.derived_values["largest_decline"]["difference"] == -20


def test_analysis_result_updates_state() -> None:
    state, task = _comparison_state()

    result = Analyst(model_client=FakeAnalystModel()).execute_task(
        state,
        task,
        trace=_trace(state),
    )

    assert state["analysis_results"] == [result]
    assert task in state["completed_tasks"]
    assert task not in state["pending_tasks"]


def test_analysis_failure_does_not_complete_task() -> None:
    state, task = _single_query_state()

    with pytest.raises(AnalystError, match="model_call"):
        Analyst(model_client=FakeAnalystModel([RuntimeError("offline")])).execute_task(
            state,
            task,
            trace=_trace(state),
        )

    assert task.status == "failed"
    assert task not in state["completed_tasks"]
    assert state["analysis_results"][-1].success is False


def test_analysis_structured_output_retry() -> None:
    state, task = _single_query_state()
    model = FakeAnalystModel(["not-json", _valid_analysis_json()])
    trace = _trace(state)

    result = Analyst(model_client=model).execute_task(state, task, trace=trace)

    assert result.retry_count == 1
    assert len(model.calls) == 2
    assert EventType.ANALYST_OUTPUT_RETRY in {
        event.event_type for event in trace.get_events()
    }


def test_analysis_retry_is_bounded() -> None:
    state, task = _single_query_state()
    model = FakeAnalystModel(["not-json", "still-not-json"])

    with pytest.raises(AnalystOutputError):
        Analyst(model_client=model).execute_task(
            state,
            task,
            trace=_trace(state),
        )

    assert len(model.calls) == 2
    assert task.status == "failed"


def test_final_answer_requires_completed_dependencies() -> None:
    state = create_initial_state("answer")
    analysis = TaskItem("analysis", "Analyze", "analysis")
    response = TaskItem("response", "Answer", "response", ["analysis"])
    state["task_plan"] = [analysis, response]
    state["pending_tasks"] = [analysis, response]

    with pytest.raises(AnalystError, match="dependencies are not completed"):
        Analyst(model_client=FakeAnalystModel()).generate_final_answer(
            state,
            response,
            trace=_trace(state),
        )

    assert state["final_answer"] is None


def test_final_answer_uses_verified_data() -> None:
    state, analysis = _single_query_state()
    response = TaskItem("response", "Answer user", "response", ["query"])
    state["task_plan"] = [state["task_plan"][0], response]
    state["pending_tasks"] = [response]
    model = FakeAnalystModel([_valid_answer_json()])

    Analyst(model_client=model).generate_final_answer(
        state,
        response,
        trace=_trace(state),
    )

    del analysis
    assert '"value":42' in model.calls[0]["user_prompt"]
    assert "Allowed source_task_ids" in model.calls[0]["user_prompt"]


def test_final_answer_updates_state() -> None:
    state, _ = _single_query_state()
    response = TaskItem("response", "Answer user", "response", ["query"])
    state["task_plan"] = [state["task_plan"][0], response]
    state["pending_tasks"] = [response]

    result = Analyst(
        model_client=FakeAnalystModel([_valid_answer_json()])
    ).generate_final_answer(state, response, trace=_trace(state))

    assert state["final_answer"] == "The verified value is 42."
    assert state["final_answer_result"] is result
    assert response.status == "completed"


def test_final_answer_does_not_invent_sources() -> None:
    state, _ = _single_query_state()
    response = TaskItem("response", "Answer user", "response", ["query"])
    state["task_plan"] = [state["task_plan"][0], response]
    state["pending_tasks"] = [response]
    invented = _valid_answer_json("invented")

    with pytest.raises(AnalystOutputError, match="unapproved sources"):
        analyst = Analyst(model_client=FakeAnalystModel([invented, invented]))
        analyst.generate_final_answer(
            state,
            response,
            trace=_trace(state),
        )

    assert state["final_answer"] is None
    assert response.status == "failed"


def test_analyst_trace_success() -> None:
    state, task = _comparison_state()
    trace = _trace(state)

    Analyst(model_client=FakeAnalystModel()).execute_task(state, task, trace=trace)

    event_types = [event.event_type for event in trace.get_events()]
    assert EventType.ANALYST_STARTED in event_types
    assert EventType.ANALYST_INPUT_PREPARED in event_types
    assert EventType.ANALYST_CALCULATION_COMPLETED in event_types
    assert EventType.ANALYST_RESULT in event_types
    assert EventType.ANALYST_COMPLETED in event_types


def test_analyst_trace_failure() -> None:
    state, task = _single_query_state()
    trace = _trace(state)

    with pytest.raises(AnalystError):
        Analyst(model_client=FakeAnalystModel([RuntimeError("offline")])).execute_task(
            state,
            task,
            trace=trace,
        )

    assert trace.get_events()[-1].event_type is EventType.ANALYST_FAILED


def test_final_answer_trace() -> None:
    state, _ = _single_query_state()
    response = TaskItem("response", "Answer user", "response", ["query"])
    state["task_plan"] = [state["task_plan"][0], response]
    state["pending_tasks"] = [response]
    trace = _trace(state)

    Analyst(
        model_client=FakeAnalystModel([_valid_answer_json()])
    ).generate_final_answer(state, response, trace=trace)

    assert [event.event_type for event in trace.get_events()] == [
        EventType.FINAL_ANSWER_STARTED,
        EventType.FINAL_ANSWER_COMPLETED,
    ]
