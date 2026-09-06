"""Grounded analysis and final-answer generation for DataPilot.

The Analyst consumes only Reviewer-approved query results and completed
analysis results. Common grouped comparisons are calculated in Python so the
model is never responsible for arithmetic.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from decimal import Decimal
from time import perf_counter
from typing import Any, Protocol

from datapilot.agent.state import (
    AgentState,
    AnalysisResult,
    FinalAnswerResult,
    SQLResult,
    TaskItem,
    get_effective_query,
)
from datapilot.tracing.trace import EventType, TraceCollector


class AnalystModel(Protocol):
    """Dependency-injection boundary for analysis language generation."""

    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_schema: Mapping[str, Any],
    ) -> str:
        """Return one complete JSON object as text."""


class AnalystError(RuntimeError):
    """Analyst failure with a safe stage and task identifier."""

    def __init__(self, *, stage: str, task_id: str, summary: str) -> None:
        self.stage = stage
        self.task_id = task_id
        self.summary = summary
        super().__init__(f"{stage} failed for task {task_id}: {summary}")


class AnalystOutputError(AnalystError):
    """Raised when a model response violates the strict output contract."""


ANALYST_SYSTEM_PROMPT = """You are DataPilot's grounded Analyst.
Use only the supplied verified query results and completed analysis results.
Do not write or execute SQL. Do not invent data, sources, calculations, or facts.
Python has already performed recognized arithmetic; preserve those values.
Return exactly one JSON object matching the supplied schema.
source_task_ids must contain only identifiers explicitly listed as allowed.
Keep the summary and findings concise. Do not reveal hidden reasoning.
"""

FINAL_ANSWER_SYSTEM_PROMPT = """You are DataPilot's final response writer.
Answer only from the supplied Reviewer-approved query results and completed
analysis results. Do not invent data, sources, calculations, or business context.
Preserve supplied calculated values exactly and make the answer concise.
Return exactly one JSON object matching the supplied schema.
source_task_ids must contain only identifiers explicitly listed as allowed.
Do not write SQL or reveal hidden reasoning.
"""

ANALYSIS_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "findings", "source_task_ids"],
    "properties": {
        "summary": {"type": "string", "minLength": 1, "maxLength": 600},
        "findings": {
            "type": "array",
            "maxItems": 20,
            "items": {"type": "string", "minLength": 1, "maxLength": 400},
        },
        "source_task_ids": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "minLength": 1},
        },
    },
}

FINAL_ANSWER_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer", "key_findings", "source_task_ids"],
    "properties": {
        "answer": {"type": "string", "minLength": 1, "maxLength": 2000},
        "key_findings": {
            "type": "array",
            "maxItems": 20,
            "items": {"type": "string", "minLength": 1, "maxLength": 400},
        },
        "source_task_ids": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "minLength": 1},
        },
    },
}

_ANALYSIS_FIELDS = frozenset({"summary", "findings", "source_task_ids"})
_FINAL_FIELDS = frozenset({"answer", "key_findings", "source_task_ids"})
_DIMENSION_HINTS = ("category", "product", "name", "type", "region", "id")
_METRIC_HINTS = ("gmv", "revenue", "sales", "amount", "value", "total")


def _safe_error_summary(error: BaseException) -> str:
    text = " ".join(str(error).split()) or type(error).__name__
    text = re.sub(
        r"(?i)(api[_ -]?key|password|token|authorization)\s*[:=]\s*\S+",
        r"\1=[REDACTED]",
        text,
    )
    text = re.sub(r"[a-z][a-z0-9+.-]*://\S+", "[REDACTED_URL]", text)
    return text[:300]


def _compact_json(value: Any, *, max_chars: int = 12000) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 3]}..."


def _is_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float, Decimal))
        and math.isfinite(float(value))
    )


def _ordered_columns(results: Sequence[SQLResult]) -> list[str]:
    if not results:
        return []
    common = set(results[0].columns)
    for result in results[1:]:
        common &= set(result.columns)
    return [column for column in results[0].columns if column in common]


def _prefer_column(columns: list[str], hints: tuple[str, ...]) -> str | None:
    lowered = {column: column.lower() for column in columns}
    for hint in hints:
        match = next(
            (column for column in columns if hint in lowered[column]),
            None,
        )
        if match is not None:
            return match
    return columns[0] if columns else None


def infer_grouped_comparison_columns(
    results: Sequence[SQLResult],
) -> tuple[str, str] | None:
    """Infer one shared dimension and numeric metric conservatively."""

    common = _ordered_columns(results)
    if len(common) < 2 or any(not result.rows for result in results):
        return None
    dimension_candidates = [
        column
        for column in common
        if any(
            not _is_number(row.get(column))
            for result in results
            for row in result.rows
            if row.get(column) is not None
        )
    ]
    numeric_candidates = [
        column
        for column in common
        if all(
            _is_number(row.get(column))
            for result in results
            for row in result.rows
        )
    ]
    dimension = _prefer_column(dimension_candidates, _DIMENSION_HINTS)
    metric = _prefer_column(numeric_candidates, _METRIC_HINTS)
    if dimension is None or metric is None or dimension == metric:
        return None
    return dimension, metric


def _infer_grouped_comparison_spec(
    results: Sequence[SQLResult],
    *,
    analysis_task_id: str,
) -> tuple[str, str, str] | None:
    """Infer a shared dimension and exactly one numeric metric per result."""

    if len(results) != 2 or any(not result.rows for result in results):
        return None
    common = _ordered_columns(results)
    dimension_candidates = [
        column
        for column in common
        if any(
            not _is_number(row.get(column))
            for result in results
            for row in result.rows
            if row.get(column) is not None
        )
    ]
    dimension = _prefer_column(dimension_candidates, _DIMENSION_HINTS)
    if dimension is None:
        return None
    metrics: list[str] = []
    for result in results:
        numeric_candidates = [
            column
            for column in result.columns
            if column != dimension
            and all(_is_number(row.get(column)) for row in result.rows)
        ]
        if not numeric_candidates:
            return None
        if len(numeric_candidates) > 1:
            raise AnalystError(
                stage="source_contract",
                task_id=analysis_task_id,
                summary=(
                    "ambiguous metric columns / source result contract mismatch "
                    f"for source {result.task_id}: {numeric_candidates}"
                ),
            )
        metrics.append(numeric_candidates[0])
    return dimension, metrics[0], metrics[1]


def _canonical_metric_name(left_metric: str, right_metric: str) -> str:
    if left_metric == right_metric:
        return left_metric
    left_lower = left_metric.lower()
    right_lower = right_metric.lower()
    return next(
        (
            hint
            for hint in _METRIC_HINTS
            if hint in left_lower and hint in right_lower
        ),
        "metric",
    )


def compare_grouped_metrics(
    left_rows: Sequence[Mapping[str, Any]],
    right_rows: Sequence[Mapping[str, Any]],
    *,
    dimension_column: str,
    metric_column: str,
    right_metric_column: str | None = None,
    left_label: str,
    right_label: str,
) -> dict[str, Any]:
    """Compare grouped metrics with deterministic Python arithmetic."""

    def aggregate(
        rows: Sequence[Mapping[str, Any]],
        value_column: str,
    ) -> dict[str, float]:
        grouped: dict[str, float] = {}
        for row in rows:
            key = row.get(dimension_column)
            value = row.get(value_column)
            if key is None or not _is_number(value):
                raise ValueError(
                    "comparison rows require a dimension and finite numeric metric"
                )
            key_text = str(key)
            grouped[key_text] = grouped.get(key_text, 0.0) + float(value)
        return grouped

    right_metric = right_metric_column or metric_column
    left = aggregate(left_rows, metric_column)
    right = aggregate(right_rows, right_metric)
    keys = list(left)
    keys.extend(key for key in right if key not in left)
    comparison: list[dict[str, Any]] = []
    for key in keys:
        left_value = left.get(key, 0.0)
        right_value = right.get(key, 0.0)
        difference = right_value - left_value
        growth_rate = difference / left_value if left_value != 0 else None
        comparison.append(
            {
                "key": key,
                left_label: left_value,
                right_label: right_value,
                "difference": difference,
                "growth_rate": growth_rate,
            }
        )
    declining = [item for item in comparison if item["difference"] < 0]
    largest_decline = min(declining, key=lambda item: item["difference"], default=None)
    return {
        "dimension_column": dimension_column,
        "metric_column": _canonical_metric_name(metric_column, right_metric),
        "metric_columns": {
            left_label: metric_column,
            right_label: right_metric,
        },
        "source_labels": [left_label, right_label],
        "comparison": comparison,
        "largest_decline": largest_decline,
    }


def _format_number(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:.6g}"


def _comparison_findings(values: Mapping[str, Any]) -> list[str]:
    findings: list[str] = []
    left_label, right_label = values["source_labels"]
    metric = values["metric_column"]
    for item in values["comparison"]:
        rate = item["growth_rate"]
        rate_text = "undefined" if rate is None else f"{rate * 100:.2f}%"
        findings.append(
            f"{item['key']}: {metric} {left_label}="
            f"{_format_number(item[left_label])}, {right_label}="
            f"{_format_number(item[right_label])}, difference="
            f"{_format_number(item['difference'])}, growth={rate_text}."
        )
    largest = values["largest_decline"]
    if largest is not None:
        findings.append(
            f"Largest decline: {largest['key']} "
            f"({_format_number(largest['difference'])}, "
            f"{largest['growth_rate'] * 100:.2f}%)."
        )
    return findings


def _approved_query_result(state: AgentState, task_id: str) -> SQLResult | None:
    reviews = [item for item in state["review_results"] if item.task_id == task_id]
    if not reviews or reviews[-1].decision != "approve":
        return None
    return next(
        (
            result
            for result in reversed(state["sql_results"])
            if result.task_id == task_id and result.success
        ),
        None,
    )


def _completed_analysis_result(
    state: AgentState,
    task_id: str,
) -> AnalysisResult | None:
    return next(
        (
            result
            for result in reversed(state["analysis_results"])
            if result.task_id == task_id and result.success
        ),
        None,
    )


def _prepare_inputs(
    state: AgentState,
    task: TaskItem,
) -> tuple[list[SQLResult], list[AnalysisResult]]:
    if not task.depends_on:
        raise AnalystError(
            stage="input",
            task_id=task.task_id,
            summary="task has no grounded dependencies",
        )
    task_by_id = {item.task_id: item for item in state["task_plan"]}
    completed_ids = {
        item.task_id for item in state["completed_tasks"] if item.status == "completed"
    }
    if not set(task.depends_on) <= completed_ids:
        raise AnalystError(
            stage="input",
            task_id=task.task_id,
            summary="task dependencies are not completed",
        )
    query_results: list[SQLResult] = []
    analysis_results: list[AnalysisResult] = []
    for source_id in task.depends_on:
        source_task = task_by_id.get(source_id)
        if source_task is None:
            raise AnalystError(
                stage="input",
                task_id=task.task_id,
                summary=f"unknown source task: {source_id}",
            )
        if source_task.task_type == "query":
            result = _approved_query_result(state, source_id)
            if result is None:
                raise AnalystError(
                    stage="input",
                    task_id=task.task_id,
                    summary=f"query source is not Reviewer-approved: {source_id}",
                )
            query_results.append(result)
        elif source_task.task_type == "analysis":
            result = _completed_analysis_result(state, source_id)
            if result is None:
                raise AnalystError(
                    stage="input",
                    task_id=task.task_id,
                    summary=f"analysis source is not completed: {source_id}",
                )
            analysis_results.append(result)
        else:
            raise AnalystError(
                stage="input",
                task_id=task.task_id,
                summary=f"response task cannot be an input: {source_id}",
            )
    return query_results, analysis_results


def _validate_string_list(
    value: Any,
    *,
    task_id: str,
    field_name: str,
    max_items: int,
) -> list[str]:
    if not isinstance(value, list) or len(value) > max_items:
        raise AnalystOutputError(
            stage="structured_output",
            task_id=task_id,
            summary=f"{field_name} must be a bounded string list",
        )
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise AnalystOutputError(
            stage="structured_output",
            task_id=task_id,
            summary=f"{field_name} contains an invalid item",
        )
    cleaned = [item.strip() for item in value]
    if len(cleaned) != len(set(cleaned)):
        raise AnalystOutputError(
            stage="structured_output",
            task_id=task_id,
            summary=f"{field_name} contains duplicates",
        )
    return cleaned


def _parse_model_object(
    response_text: str,
    *,
    task_id: str,
    expected_fields: frozenset[str],
    text_field: str,
    list_field: str,
    allowed_source_ids: set[str],
) -> tuple[str, list[str], list[str]]:
    try:
        raw = json.loads(response_text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AnalystOutputError(
            stage="structured_output",
            task_id=task_id,
            summary="response is not valid JSON",
        ) from exc
    if not isinstance(raw, dict) or frozenset(raw) != expected_fields:
        raise AnalystOutputError(
            stage="structured_output",
            task_id=task_id,
            summary="response fields do not match the schema",
        )
    text = raw[text_field]
    if not isinstance(text, str) or not text.strip():
        raise AnalystOutputError(
            stage="structured_output",
            task_id=task_id,
            summary=f"{text_field} must not be empty",
        )
    items = _validate_string_list(
        raw[list_field],
        task_id=task_id,
        field_name=list_field,
        max_items=20,
    )
    source_ids = _validate_string_list(
        raw["source_task_ids"],
        task_id=task_id,
        field_name="source_task_ids",
        max_items=len(allowed_source_ids),
    )
    unknown = set(source_ids) - allowed_source_ids
    if unknown:
        raise AnalystOutputError(
            stage="structured_output",
            task_id=task_id,
            summary=f"response cites unapproved sources: {sorted(unknown)}",
        )
    if allowed_source_ids and not source_ids:
        raise AnalystOutputError(
            stage="structured_output",
            task_id=task_id,
            summary="response must cite at least one approved source",
        )
    return text.strip(), items, source_ids


def _next_ready_task(state: AgentState) -> TaskItem | None:
    completed_ids = {
        item.task_id for item in state["completed_tasks"] if item.status == "completed"
    }
    pending_ids = {item.task_id for item in state["pending_tasks"]}
    return next(
        (
            item
            for item in state["task_plan"]
            if item.task_id in pending_ids
            and item.status == "pending"
            and set(item.depends_on) <= completed_ids
        ),
        None,
    )


def _mark_completed(state: AgentState, task: TaskItem) -> None:
    task.status = "completed"
    if all(item.task_id != task.task_id for item in state["completed_tasks"]):
        state["completed_tasks"].append(task)
    state["pending_tasks"] = [
        item for item in state["pending_tasks"] if item.task_id != task.task_id
    ]
    state["current_task"] = _next_ready_task(state)


def _mark_failed(state: AgentState, task: TaskItem) -> None:
    task.status = "failed"
    state["pending_tasks"] = [
        item for item in state["pending_tasks"] if item.task_id != task.task_id
    ]
    state["current_task"] = task


class Analyst:
    """Run grounded analysis tasks and produce the final response."""

    def __init__(
        self,
        *,
        model_client: AnalystModel,
        trace: TraceCollector | None = None,
        max_output_retries: int = 1,
    ) -> None:
        if max_output_retries not in {0, 1}:
            raise ValueError("max_output_retries must be 0 or 1")
        self.model_client = model_client
        self.trace = trace
        self.max_output_retries = max_output_retries

    def execute_task(
        self,
        state: AgentState,
        task: TaskItem,
        *,
        trace: TraceCollector | None = None,
    ) -> AnalysisResult:
        """Execute one ready analysis task from grounded dependencies."""

        active_trace = self._active_trace(state, trace)
        started_at = perf_counter()
        self._event(active_trace, EventType.ANALYST_STARTED, task, started_at)
        if task.task_type != "analysis" or task.status != "pending":
            error = AnalystError(
                stage="task_validation",
                task_id=task.task_id,
                summary="task is not a pending analysis task",
            )
            self._fail(state, task, active_trace, started_at, error)
            raise error
        task.status = "in_progress"
        try:
            query_results, analysis_results = _prepare_inputs(state, task)
            self._event(
                active_trace,
                EventType.ANALYST_INPUT_PREPARED,
                task,
                started_at,
                source_task_ids=list(task.depends_on),
            )
            deterministic = self._deterministic_result(
                task,
                query_results,
                analysis_results,
            )
            if deterministic is not None:
                self._event(
                    active_trace,
                    EventType.ANALYST_CALCULATION_COMPLETED,
                    task,
                    started_at,
                    source_task_ids=list(task.depends_on),
                )
                result = deterministic
            else:
                result = self._generate_analysis(
                    state,
                    task,
                    query_results,
                    analysis_results,
                    trace=active_trace,
                    started_at=started_at,
                )
        except AnalystError as exc:
            self._fail(state, task, active_trace, started_at, exc)
            raise
        except Exception as exc:
            error = AnalystError(
                stage="analysis",
                task_id=task.task_id,
                summary=_safe_error_summary(exc),
            )
            self._fail(state, task, active_trace, started_at, error)
            raise error from exc

        state["analysis_results"].append(result)
        _mark_completed(state, task)
        self._event(
            active_trace,
            EventType.ANALYST_RESULT,
            task,
            started_at,
            source_task_ids=result.source_task_ids,
        )
        self._event(
            active_trace,
            EventType.ANALYST_COMPLETED,
            task,
            started_at,
            source_task_ids=result.source_task_ids,
        )
        return result

    def generate_final_answer(
        self,
        state: AgentState,
        task: TaskItem,
        *,
        trace: TraceCollector | None = None,
    ) -> FinalAnswerResult:
        """Generate one grounded final answer for a ready response task."""

        active_trace = self._active_trace(state, trace)
        started_at = perf_counter()
        self._event(active_trace, EventType.FINAL_ANSWER_STARTED, task, started_at)
        if task.task_type != "response" or task.status != "pending":
            error = AnalystError(
                stage="task_validation",
                task_id=task.task_id,
                summary="task is not a pending response task",
            )
            self._fail_final(state, task, active_trace, started_at, error)
            raise error
        task.status = "in_progress"
        try:
            query_results, analysis_results = _prepare_inputs(state, task)
            allowed = set(task.depends_on)
            prompt = (
                f"Effective user query:\n{get_effective_query(state)}\n\n"
                f"Response task:\n{task.description}\n\n"
                f"Allowed source_task_ids:\n{_compact_json(task.depends_on)}\n\n"
                "Verified query results:\n"
                f"{_compact_json([asdict(item) for item in query_results])}\n\n"
                "Completed analysis results:\n"
                f"{_compact_json([asdict(item) for item in analysis_results])}"
            )
            text, findings, source_ids, retry_count = self._call_model(
                task,
                prompt=prompt,
                system_prompt=FINAL_ANSWER_SYSTEM_PROMPT,
                response_schema=FINAL_ANSWER_RESPONSE_SCHEMA,
                expected_fields=_FINAL_FIELDS,
                text_field="answer",
                list_field="key_findings",
                allowed_source_ids=allowed,
                retry_event=EventType.ANALYST_OUTPUT_RETRY,
                trace=active_trace,
                started_at=started_at,
            )
            result = FinalAnswerResult(
                task_id=task.task_id,
                answer=text,
                key_findings=findings,
                source_task_ids=source_ids,
                retry_count=retry_count,
            )
        except AnalystError as exc:
            self._fail_final(state, task, active_trace, started_at, exc)
            raise
        except Exception as exc:
            error = AnalystError(
                stage="final_answer",
                task_id=task.task_id,
                summary=_safe_error_summary(exc),
            )
            self._fail_final(state, task, active_trace, started_at, error)
            raise error from exc

        state["final_answer"] = result.answer
        state["final_answer_result"] = result
        _mark_completed(state, task)
        self._event(
            active_trace,
            EventType.FINAL_ANSWER_COMPLETED,
            task,
            started_at,
            source_task_ids=result.source_task_ids,
        )
        return result

    @staticmethod
    def _deterministic_result(
        task: TaskItem,
        query_results: list[SQLResult],
        analysis_results: list[AnalysisResult],
    ) -> AnalysisResult | None:
        if len(query_results) == 2 and not analysis_results:
            inferred = _infer_grouped_comparison_spec(
                query_results,
                analysis_task_id=task.task_id,
            )
            if inferred is not None:
                dimension, left_metric, right_metric = inferred
                values = compare_grouped_metrics(
                    query_results[0].rows,
                    query_results[1].rows,
                    dimension_column=dimension,
                    metric_column=left_metric,
                    right_metric_column=right_metric,
                    left_label=query_results[0].task_id,
                    right_label=query_results[1].task_id,
                )
                return AnalysisResult(
                    task_id=task.task_id,
                    summary=(
                        f"Compared {values['metric_column']} by {dimension} across "
                        f"{query_results[0].task_id} and {query_results[1].task_id}."
                    ),
                    findings=_comparison_findings(values),
                    derived_values=values,
                    source_task_ids=[item.task_id for item in query_results],
                )
        if not query_results and analysis_results:
            largest = next(
                (
                    result.derived_values["largest_decline"]
                    for result in analysis_results
                    if result.derived_values.get("largest_decline") is not None
                ),
                None,
            )
            if largest is not None:
                return AnalysisResult(
                    task_id=task.task_id,
                    summary=f"Largest decline is {largest['key']}.",
                    findings=[
                        f"Largest decline: {largest['key']} "
                        f"({_format_number(float(largest['difference']))})."
                    ],
                    derived_values={"largest_decline": largest},
                    source_task_ids=[item.task_id for item in analysis_results],
                )
        return None

    def _generate_analysis(
        self,
        state: AgentState,
        task: TaskItem,
        query_results: list[SQLResult],
        analysis_results: list[AnalysisResult],
        *,
        trace: TraceCollector,
        started_at: float,
    ) -> AnalysisResult:
        prompt = (
            f"Effective user query:\n{get_effective_query(state)}\n\n"
            f"Analysis task:\n{task.description}\n\n"
            f"Allowed source_task_ids:\n{_compact_json(task.depends_on)}\n\n"
            "Verified query results:\n"
            f"{_compact_json([asdict(item) for item in query_results])}\n\n"
            "Completed analysis results:\n"
            f"{_compact_json([asdict(item) for item in analysis_results])}"
        )
        summary, findings, source_ids, retry_count = self._call_model(
            task,
            prompt=prompt,
            system_prompt=ANALYST_SYSTEM_PROMPT,
            response_schema=ANALYSIS_RESPONSE_SCHEMA,
            expected_fields=_ANALYSIS_FIELDS,
            text_field="summary",
            list_field="findings",
            allowed_source_ids=set(task.depends_on),
            retry_event=EventType.ANALYST_OUTPUT_RETRY,
            trace=trace,
            started_at=started_at,
        )
        return AnalysisResult(
            task_id=task.task_id,
            summary=summary,
            findings=findings,
            source_task_ids=source_ids,
            retry_count=retry_count,
        )

    def _call_model(
        self,
        task: TaskItem,
        *,
        prompt: str,
        system_prompt: str,
        response_schema: Mapping[str, Any],
        expected_fields: frozenset[str],
        text_field: str,
        list_field: str,
        allowed_source_ids: set[str],
        retry_event: EventType,
        trace: TraceCollector,
        started_at: float,
    ) -> tuple[str, list[str], list[str], int]:
        last_error: AnalystOutputError | None = None
        for attempt in range(self.max_output_retries + 1):
            suffix = ""
            if last_error is not None:
                suffix = (
                    "\n\nStructured output correction:\n"
                    f"Error: {last_error.summary}\n"
                    "Return a corrected JSON object only."
                )
            try:
                response = self.model_client.complete(
                    system_prompt=system_prompt,
                    user_prompt=f"{prompt}{suffix}",
                    response_schema=response_schema,
                )
            except Exception as exc:
                raise AnalystError(
                    stage="model_call",
                    task_id=task.task_id,
                    summary=_safe_error_summary(exc),
                ) from exc
            try:
                text, items, source_ids = _parse_model_object(
                    response,
                    task_id=task.task_id,
                    expected_fields=expected_fields,
                    text_field=text_field,
                    list_field=list_field,
                    allowed_source_ids=allowed_source_ids,
                )
            except AnalystOutputError as exc:
                last_error = exc
                if attempt < self.max_output_retries:
                    self._event(trace, retry_event, task, started_at)
                    continue
                raise
            return text, items, source_ids, attempt
        raise AssertionError("Analyst output retry loop exited unexpectedly")

    def _active_trace(
        self,
        state: AgentState,
        trace: TraceCollector | None,
    ) -> TraceCollector:
        active = trace if trace is not None else self.trace
        if active is None:
            raise ValueError("a TraceCollector is required")
        if active.trace_id != state["trace_id"]:
            raise ValueError("trace and state must use the same trace_id")
        return active

    @staticmethod
    def _event(
        trace: TraceCollector,
        event_type: EventType,
        task: TaskItem,
        started_at: float,
        *,
        source_task_ids: list[str] | None = None,
        error_type: str | None = None,
    ) -> None:
        metadata: dict[str, Any] = {
            "task_id": task.task_id,
            "duration": perf_counter() - started_at,
        }
        if source_task_ids is not None:
            metadata["source_task_ids"] = source_task_ids
        if error_type is not None:
            metadata["error_type"] = error_type
        trace.add_event(
            event_type,
            component="analyst",
            action=event_type.value.lower(),
            summary=f"Analyst event: {event_type.value}.",
            metadata=metadata,
        )

    def _fail(
        self,
        state: AgentState,
        task: TaskItem,
        trace: TraceCollector,
        started_at: float,
        error: AnalystError,
    ) -> None:
        _mark_failed(state, task)
        state["analysis_results"].append(
            AnalysisResult(
                task_id=task.task_id,
                summary="Analysis failed.",
                success=False,
                error=error.summary,
                source_task_ids=list(task.depends_on),
            )
        )
        self._event(
            trace,
            EventType.ANALYST_FAILED,
            task,
            started_at,
            error_type=type(error).__name__,
        )

    def _fail_final(
        self,
        state: AgentState,
        task: TaskItem,
        trace: TraceCollector,
        started_at: float,
        error: AnalystError,
    ) -> None:
        _mark_failed(state, task)
        state["final_answer_result"] = FinalAnswerResult(
            task_id=task.task_id,
            answer="",
            success=False,
            error=error.summary,
            source_task_ids=list(task.depends_on),
        )
        self._event(
            trace,
            EventType.FINAL_ANSWER_FAILED,
            task,
            started_at,
            error_type=type(error).__name__,
        )
