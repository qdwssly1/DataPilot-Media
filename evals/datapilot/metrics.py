"""Metric aggregation for DataPilot's offline synthetic evaluation."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class EvalObservation:
    """One case outcome plus only the metric facts that case can measure."""

    case_id: str
    passed: bool
    detail: str = ""
    planner_intent_correct: bool | None = None
    planner_plan_valid: bool | None = None
    sql_execution_success: bool | None = None
    sql_safety_rejected: bool | None = None
    reviewer_decision_correct: bool | None = None
    semantic_correction_success: bool | None = None
    analysis_numeric_correct: bool | None = None
    follow_up_resolution_correct: bool | None = None
    end_to_end_task_success: bool | None = None
    technical_retries: int | None = None
    semantic_retries: int | None = None


_RATE_METRICS = {
    "planner_intent_accuracy": "planner_intent_correct",
    "planner_plan_valid_rate": "planner_plan_valid",
    "sql_execution_success_rate": "sql_execution_success",
    "sql_safety_rejection_rate": "sql_safety_rejected",
    "reviewer_decision_accuracy": "reviewer_decision_correct",
    "semantic_correction_success_rate": "semantic_correction_success",
    "analysis_numeric_accuracy": "analysis_numeric_correct",
    "follow_up_resolution_accuracy": "follow_up_resolution_correct",
    "end_to_end_task_success_rate": "end_to_end_task_success",
}


def _rate(observations: list[EvalObservation], field_name: str) -> float:
    values = [
        getattr(observation, field_name)
        for observation in observations
        if getattr(observation, field_name) is not None
    ]
    if not values:
        raise ValueError(f"no eligible cases for {field_name}")
    return round(sum(bool(value) for value in values) / len(values), 6)


def _average(observations: list[EvalObservation], field_name: str) -> float:
    values = [
        getattr(observation, field_name)
        for observation in observations
        if getattr(observation, field_name) is not None
    ]
    if not values:
        raise ValueError(f"no eligible cases for {field_name}")
    return round(sum(values) / len(values), 6)


def calculate_metrics(observations: list[EvalObservation]) -> dict[str, float]:
    """Calculate only metrics backed by eligible case observations."""

    metrics = {
        name: _rate(observations, field_name)
        for name, field_name in _RATE_METRICS.items()
    }
    metrics["average_technical_retries"] = _average(
        observations,
        "technical_retries",
    )
    metrics["average_semantic_retries"] = _average(
        observations,
        "semantic_retries",
    )
    return metrics
