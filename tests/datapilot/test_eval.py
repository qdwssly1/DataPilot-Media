from __future__ import annotations

from evals.datapilot.runner import load_cases, render_markdown, run_eval


EXPECTED_METRICS = {
    "planner_intent_accuracy",
    "planner_plan_valid_rate",
    "sql_execution_success_rate",
    "sql_safety_rejection_rate",
    "reviewer_decision_accuracy",
    "semantic_correction_success_rate",
    "analysis_numeric_accuracy",
    "follow_up_resolution_accuracy",
    "end_to_end_task_success_rate",
    "average_technical_retries",
    "average_semantic_retries",
}


def test_eval_dataset_has_30_unique_synthetic_cases() -> None:
    cases = load_cases()

    assert len(cases) == 30
    assert len({case["id"] for case in cases}) == len(cases)
    assert {case["component"] for case in cases} == {
        "planner",
        "sql",
        "reviewer",
        "analyst",
        "follow_up",
        "end_to_end",
    }
    assert all(
        set(case).isdisjoint({"api_key", "base_url", "connection_string"})
        for case in cases
    )


def test_offline_eval_runs_without_failures() -> None:
    result = run_eval()

    assert result["case_count"] == 30
    assert result["failed_cases"] == []
    assert set(result["metrics"]) == EXPECTED_METRICS
    assert all(
        value == 1.0
        for name, value in result["metrics"].items()
        if not name.startswith("average_")
    )
    assert result["metrics"]["average_technical_retries"] == 0.2
    assert result["metrics"]["average_semantic_retries"] == 0.1
    assert result["environment"]["network"] == "disabled by design"


def test_eval_markdown_reports_measured_results() -> None:
    result = run_eval()

    markdown = render_markdown(result)

    assert "Cases: **30**" in markdown
    assert "`planner_intent_accuracy` | `1.000000`" in markdown
    assert "`average_semantic_retries` | `0.100000`" in markdown
    assert "Real LLM evaluation was not run" in markdown
