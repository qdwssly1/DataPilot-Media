"""Grounded analysis and final-answer generation for DataPilot.

The Analyst consumes only Reviewer-approved query results and completed
analysis results. Common grouped comparisons are calculated in Python so the
model is never responsible for arithmetic.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal
from time import perf_counter
from typing import Any, Protocol

from datapilot.agent.evidence import (
    EvidenceProjectionError,
    build_evidence_contract,
    build_final_evidence_pack,
    build_final_evidence_projection,
    evidence_accuracy_violations,
    evidence_bundle_claim_violations,
    evidence_projection_claim_violations,
    final_claim_violations,
    normalize_evidence_language,
    render_final_answer,
)
from datapilot.agent.state import (
    AgentState,
    AnalysisResult,
    FinalAnswerResult,
    SQLResult,
    TaskItem,
    get_effective_query,
)
from datapilot.retrieval.integration import compact_knowledge_evidence
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
Use only the supplied verified query results, completed analysis results, and
retrieved knowledge evidence. Keep them structurally distinct: DATA EVIDENCE is
Reviewer-approved query output; KNOWLEDGE EVIDENCE is documented definition/SOP;
INFERENCE is a cautious interpretation supported by those inputs.
Do not write or execute SQL. Do not invent data, sources, calculations, or facts.
Knowledge text must never be presented as an observed alarm or measured result.
Temporal overlap or an error-code description is correlation, not causal proof.
Planner task wording is an instruction, not evidence: do not preserve a label such
as "healthy" when approved rows show that the group declined. Preserve every
observed categorical value and count; never treat one aggregate value as the state
of every record. Follow the supplied structured evidence contract exactly.
For every group listed in declining_groups, explicitly retain the "declined"
classification. Only largest_decline_group may be called the main degradation;
other declining groups must be described as smaller declines, never as controls.
Python has already performed recognized arithmetic; preserve those values.
Return exactly one JSON object matching the supplied schema.
source_task_ids must contain only identifiers explicitly listed as allowed.
Keep the summary and findings concise. Do not reveal hidden reasoning.
"""

FINAL_ANSWER_SYSTEM_PROMPT = """You are DataPilot's final response writer and
typed claim selector, but not a prose writer. Return exactly one JSON object matching the supplied schema. For
each claim, first choose one visible bundle_id and cite only IDs allowed by that
deterministic bundle. Select every projected data item and limitation marked
required, and only relevant knowledge.

General claims always use predicate=general, polarity=neutral, and no subjects.
Only observation may use predicate=stable_control; it requires positive or
negative polarity and non-empty group-comparison subjects. Declining subjects
require negative polarity. observation is Data-backed; knowledge is
Knowledge-backed; correlation needs at least two sources and at least one Data
item; hypothesis needs Data plus Knowledge or Limitation; recommendation needs
Knowledge plus Data or Limitation; causal_claim needs explicit approved causal
evidence. Knowledge is documentation, not observed data.

Claims contain no factual statement. Facts, scope, numbers, and causality come
only from typed fields and selected IDs. Respect region, window, CDN, severity,
level, and error-code scopes. Use entity bundles for entity incidents, regional
bundles for shared regional factors, multi_group bundles for cross-group
comparisons, and global_knowledge for knowledge-only claims. Split regional and
entity recommendations. Never cite across bundles or encode causal meaning as a
noncausal type. source_task_ids must be allowed IDs. Do not output SQL, prose,
debug text, reasoning, aliases, or extra fields.
"""

FINAL_PROMPT_CHAR_LIMIT = 20_000
FINAL_PROMPT_SAFETY_MARGIN = 1_200

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

_CLAIM_TYPES = [
    "observation",
    "knowledge",
    "correlation",
    "hypothesis",
    "recommendation",
    "causal_claim",
]
_CLAIM_SUPPORT_SCHEMA: dict[str, Any] = {
    "type": "array",
    "minItems": 1,
    "maxItems": 20,
    "uniqueItems": True,
    "items": {"type": "string", "minLength": 1},
}
_GENERAL_CLAIM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "bundle_id",
        "claim_type",
        "predicate",
        "polarity",
        "subject_evidence_ids",
        "supporting_evidence_ids",
    ],
    "properties": {
        "bundle_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "claim_type": {"type": "string", "enum": _CLAIM_TYPES},
        "predicate": {"type": "string", "const": "general"},
        "polarity": {"type": "string", "const": "neutral"},
        "subject_evidence_ids": {
            "type": "array",
            "maxItems": 0,
            "items": {"type": "string", "minLength": 1},
        },
        "supporting_evidence_ids": deepcopy(_CLAIM_SUPPORT_SCHEMA),
    },
}
_STABLE_CONTROL_CLAIM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "bundle_id",
        "claim_type",
        "predicate",
        "polarity",
        "subject_evidence_ids",
        "supporting_evidence_ids",
    ],
    "properties": {
        "bundle_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "claim_type": {"type": "string", "const": "observation"},
        "predicate": {"type": "string", "const": "stable_control"},
        "polarity": {
            "type": "string",
            "enum": ["positive", "negative"],
        },
        "subject_evidence_ids": {
            "type": "array",
            "minItems": 1,
            "maxItems": 20,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1},
        },
        "supporting_evidence_ids": deepcopy(_CLAIM_SUPPORT_SCHEMA),
    },
}


FINAL_ANSWER_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "data_evidence_ids",
        "knowledge_evidence_ids",
        "inferences",
        "limitation_ids",
        "source_task_ids",
    ],
    "properties": {
        "data_evidence_ids": {
            "type": "array",
            "maxItems": 50,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 400},
        },
        "knowledge_evidence_ids": {
            "type": "array",
            "maxItems": 5,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 400},
        },
        "inferences": {
            "type": "array",
            "maxItems": 10,
            "items": {
                "oneOf": [
                    _GENERAL_CLAIM_SCHEMA,
                    _STABLE_CONTROL_CLAIM_SCHEMA,
                ],
            },
        },
        "limitation_ids": {
            "type": "array",
            "maxItems": 20,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 400},
        },
        "source_task_ids": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1},
        },
    },
}

_ANALYSIS_FIELDS = frozenset({"summary", "findings", "source_task_ids"})
_FINAL_FIELDS = frozenset(
    {
        "data_evidence_ids",
        "knowledge_evidence_ids",
        "inferences",
        "limitation_ids",
        "source_task_ids",
    }
)
_DIMENSION_HINTS = ("category", "product", "name", "type", "region", "id")
_METRIC_HINTS = ("gmv", "revenue", "sales", "amount", "value", "total")
_KNOWLEDGE_CITATION_RE = re.compile(r"\[knowledge:([^\]]+)\]")


def _final_answer_schema(*, allow_empty_sources: bool) -> dict[str, Any]:
    if not allow_empty_sources:
        return FINAL_ANSWER_RESPONSE_SCHEMA
    schema = deepcopy(FINAL_ANSWER_RESPONSE_SCHEMA)
    schema["properties"]["source_task_ids"]["minItems"] = 0
    return schema


def _final_contract_prompt() -> str:
    exact_fields = json.dumps(sorted(_FINAL_FIELDS), ensure_ascii=False)
    return (
        "\n\nStructured output keys:\n"
        f"Return exactly these top-level fields: {exact_fields}. "
        "Include every field and do not add aliases or extra fields."
    )


def _bounded_analyst_guidance(
    analysis_results: Sequence[AnalysisResult],
) -> dict[str, Any]:
    """Keep strategy continuity without duplicating Analyst facts."""

    return {
        "completed_analysis_task_ids": [
            item.task_id for item in analysis_results if item.success
        ],
        "guidance": (
            "Use the projected evidence as the only fact source. Preserve "
            "correlation/causation boundaries and recommend checks for explicit "
            "evidence gaps."
        ),
    }


def _prompt_chars(
    *,
    user_prompt: str,
    response_schema: Mapping[str, Any],
    correction_suffix: str = "",
) -> int:
    schema_json = json.dumps(
        response_schema,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return sum(
        map(
            len,
            (
                FINAL_ANSWER_SYSTEM_PROMPT,
                user_prompt,
                _final_contract_prompt(),
                correction_suffix,
                schema_json,
            ),
        )
    )


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


def _compact_verified_query_results(
    results: Sequence[SQLResult],
    *,
    max_chars: int = 12000,
) -> str:
    """Bound verified rows without truncating later dependency sources.

    A single prefix truncation can hide an approved result that happens to be
    last (notably a SQL-corrected log task after large QoE results). Build a
    compact record for every result first, then add rows round-robin while the
    unchanged total budget permits. The evidence contract separately carries
    complete deterministic comparison and distribution facts.
    """

    records: list[dict[str, Any]] = []
    source_rows: list[list[dict[str, Any]]] = []
    for result in results:
        continuity = result.tool_metadata.get("evidence_continuity")
        continuity = (
            dict(continuity) if isinstance(continuity, Mapping) else None
        )
        rows = [dict(row) for row in result.rows]
        source_rows.append(rows)
        records.append(
            {
                "task_id": result.task_id,
                "success": result.success,
                "columns": list(result.columns),
                "row_count": result.row_count,
                "rows": [],
                "rows_included": 0,
                "rows_omitted": len(rows),
                "execution_source": result.execution_source,
                "tool_name": result.tool_name,
                "tool_input": dict(result.tool_input),
                "semantic_retry_count": result.semantic_retry_count,
                "evidence_continuity": continuity,
                "sample_semantics": "bounded_verified_rows",
            }
        )

    base_serialized = json.dumps(
        records,
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    )
    if len(base_serialized) > max_chars:
        return _compact_json(records, max_chars=max_chars)

    positions = [0] * len(records)
    blocked: set[int] = set()
    while len(blocked) < len(records):
        progressed = False
        for index, record in enumerate(records):
            if index in blocked:
                continue
            position = positions[index]
            if position >= len(source_rows[index]):
                blocked.add(index)
                continue
            record["rows"].append(source_rows[index][position])
            record["rows_included"] = position + 1
            record["rows_omitted"] = len(source_rows[index]) - position - 1
            serialized = json.dumps(
                records,
                ensure_ascii=False,
                default=str,
                separators=(",", ":"),
            )
            if len(serialized) <= max_chars:
                positions[index] += 1
                progressed = True
                continue
            record["rows"].pop()
            record["rows_included"] = position
            record["rows_omitted"] = len(source_rows[index]) - position
            blocked.add(index)
        if not progressed:
            break
    return json.dumps(
        records,
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    )


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
    explicit_bindings: list[dict[str, Any] | None] = []
    for result in results:
        raw_binding = result.tool_metadata.get("metric_binding")
        if not isinstance(raw_binding, Mapping):
            preserved = result.tool_metadata.get("preserved_tool_evidence")
            if isinstance(preserved, Mapping):
                raw_binding = preserved.get("metric_binding")
        binding = dict(raw_binding) if isinstance(raw_binding, Mapping) else None
        explicit_bindings.append(binding)
        if binding is not None:
            primary = binding.get("primary_metric")
            if not isinstance(primary, str) or not primary.strip():
                raise AnalystError(
                    stage="source_contract",
                    task_id=analysis_task_id,
                    summary=(
                        "invalid primary metric binding for source "
                        f"{result.task_id}"
                    ),
                )
            primary = primary.strip()
            if primary not in result.columns or not all(
                _is_number(row.get(primary)) for row in result.rows
            ):
                raise AnalystError(
                    stage="source_contract",
                    task_id=analysis_task_id,
                    summary=(
                        f"bound primary metric {primary!r} is absent or non-numeric "
                        f"for source {result.task_id}"
                    ),
                )
            metrics.append(primary)
            continue
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
    if any(binding is not None for binding in explicit_bindings):
        if any(binding is None for binding in explicit_bindings):
            raise AnalystError(
                stage="source_contract",
                task_id=analysis_task_id,
                summary="metric binding is missing from one comparison source",
            )
        semantics = {
            (
                str(binding.get("primary_metric")),
                str(binding.get("unit")),
                str(binding.get("aggregation_semantics")),
            )
            for binding in explicit_bindings
            if binding is not None
        }
        if len(semantics) != 1:
            raise AnalystError(
                stage="source_contract",
                task_id=analysis_task_id,
                summary="metric bindings are incompatible across comparison sources",
            )
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
    *,
    allow_knowledge_only: bool = False,
) -> tuple[list[SQLResult], list[AnalysisResult]]:
    if not task.depends_on:
        if allow_knowledge_only and state["knowledge_evidence"]:
            return [], []
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


def _transitive_approved_query_results(
    state: AgentState,
    task: TaskItem,
) -> list[SQLResult]:
    """Collect approved SQL evidence behind direct analysis dependencies."""

    task_by_id = {item.task_id: item for item in state["task_plan"]}
    seen_tasks: set[str] = set()
    seen_results: set[str] = set()
    output: list[SQLResult] = []

    def visit(task_id: str) -> None:
        if task_id in seen_tasks:
            return
        seen_tasks.add(task_id)
        source = task_by_id.get(task_id)
        if source is None:
            return
        if source.task_type == "query":
            result = _approved_query_result(state, task_id)
            if result is not None and task_id not in seen_results:
                seen_results.add(task_id)
                output.append(result)
            return
        if source.task_type == "analysis":
            for dependency in source.depends_on:
                visit(dependency)

    for dependency in task.depends_on:
        visit(dependency)
    return output


def _transitive_completed_analysis_results(
    state: AgentState,
    task: TaskItem,
) -> list[AnalysisResult]:
    """Collect completed analysis evidence behind a response dependency."""

    task_by_id = {item.task_id: item for item in state["task_plan"]}
    seen_tasks: set[str] = set()
    output: list[AnalysisResult] = []

    def visit(task_id: str) -> None:
        if task_id in seen_tasks:
            return
        seen_tasks.add(task_id)
        source = task_by_id.get(task_id)
        if source is None or source.task_type != "analysis":
            return
        result = _completed_analysis_result(state, task_id)
        if result is not None:
            output.append(result)
        for dependency in source.depends_on:
            visit(dependency)

    for dependency in task.depends_on:
        visit(dependency)
    return output


def _validate_evidence_output(
    text: str,
    items: Sequence[str],
    *,
    task_id: str,
    contract: Mapping[str, Any],
    require_sections: bool,
) -> None:
    violations = evidence_accuracy_violations(
        text,
        supporting_items=items,
        contract=contract,
        require_sections=require_sections,
    )
    if violations:
        raise AnalystOutputError(
            stage="evidence_contract",
            task_id=task_id,
            summary=violations[0][:300],
        )


def _validate_knowledge_citations(
    answer: str,
    state: AgentState,
    *,
    task_id: str,
    required: bool,
) -> None:
    citations = set(_KNOWLEDGE_CITATION_RE.findall(answer))
    allowed = {str(item["chunk_id"]) for item in state["knowledge_evidence"]}
    unknown = citations - allowed
    if unknown:
        raise AnalystOutputError(
            stage="structured_output",
            task_id=task_id,
            summary=f"response cites unknown knowledge chunks: {sorted(unknown)}",
        )
    if required and not citations:
        raise AnalystOutputError(
            stage="structured_output",
            task_id=task_id,
            summary="knowledge-only response must cite retrieved knowledge",
        )


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


def _parse_final_selection(
    response_text: str,
    *,
    task_id: str,
    allowed_source_ids: set[str],
) -> dict[str, Any]:
    try:
        raw = json.loads(response_text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AnalystOutputError(
            stage="structured_output",
            task_id=task_id,
            summary="response is not valid JSON",
        ) from exc
    if not isinstance(raw, dict) or frozenset(raw) != _FINAL_FIELDS:
        raise AnalystOutputError(
            stage="structured_output",
            task_id=task_id,
            summary="response fields do not match the schema",
        )
    data_ids = _validate_string_list(
        raw["data_evidence_ids"],
        task_id=task_id,
        field_name="data_evidence_ids",
        max_items=50,
    )
    knowledge_ids = _validate_string_list(
        raw["knowledge_evidence_ids"],
        task_id=task_id,
        field_name="knowledge_evidence_ids",
        max_items=5,
    )
    limitation_ids = _validate_string_list(
        raw["limitation_ids"],
        task_id=task_id,
        field_name="limitation_ids",
        max_items=20,
    )
    source_ids = _validate_string_list(
        raw["source_task_ids"],
        task_id=task_id,
        field_name="source_task_ids",
        max_items=len(allowed_source_ids),
    )
    unknown_sources = set(source_ids) - allowed_source_ids
    if unknown_sources:
        raise AnalystOutputError(
            stage="structured_output",
            task_id=task_id,
            summary=f"response cites unapproved sources: {sorted(unknown_sources)}",
        )
    if allowed_source_ids and not source_ids:
        raise AnalystOutputError(
            stage="structured_output",
            task_id=task_id,
            summary="response must cite at least one approved source",
        )
    raw_inferences = raw["inferences"]
    if not isinstance(raw_inferences, list) or len(raw_inferences) > 10:
        raise AnalystOutputError(
            stage="structured_output",
            task_id=task_id,
            summary="inferences must be a bounded object list",
        )
    inferences: list[dict[str, Any]] = []
    expected = {
        "bundle_id",
        "claim_type",
        "predicate",
        "polarity",
        "subject_evidence_ids",
        "supporting_evidence_ids",
    }
    for index, item in enumerate(raw_inferences, start=1):
        if not isinstance(item, dict) or set(item) != expected:
            raise AnalystOutputError(
                stage="structured_output",
                task_id=task_id,
                summary=f"inference {index} fields do not match the schema",
            )
        bundle_id = item["bundle_id"]
        claim_type = item["claim_type"]
        predicate = item["predicate"]
        polarity = item["polarity"]
        if not isinstance(bundle_id, str) or not bundle_id.strip():
            raise AnalystOutputError(
                stage="structured_output",
                task_id=task_id,
                summary=f"inference {index} requires a bundle_id",
            )
        if claim_type not in set(_CLAIM_TYPES):
            raise AnalystOutputError(
                stage="structured_output",
                task_id=task_id,
                summary=f"inference {index} has an invalid claim_type",
            )
        if predicate not in {"general", "stable_control"}:
            raise AnalystOutputError(
                stage="structured_output",
                task_id=task_id,
                summary=f"inference {index} has an invalid predicate",
            )
        if polarity not in {"neutral", "positive", "negative"}:
            raise AnalystOutputError(
                stage="structured_output",
                task_id=task_id,
                summary=f"inference {index} has an invalid polarity",
            )
        support = _validate_string_list(
            item["supporting_evidence_ids"],
            task_id=task_id,
            field_name=f"inferences[{index}].supporting_evidence_ids",
            max_items=20,
        )
        if not support:
            raise AnalystOutputError(
                stage="structured_output",
                task_id=task_id,
                summary=f"inference {index} must cite supporting evidence",
            )
        subjects = _validate_string_list(
            item["subject_evidence_ids"],
            task_id=task_id,
            field_name=f"inferences[{index}].subject_evidence_ids",
            max_items=20,
        )
        if predicate == "general":
            if polarity != "neutral":
                raise AnalystOutputError(
                    stage="structured_output",
                    task_id=task_id,
                    summary=(
                        f"inference {index} general predicate requires "
                        "neutral polarity"
                    ),
                )
            if subjects:
                raise AnalystOutputError(
                    stage="structured_output",
                    task_id=task_id,
                    summary=(
                        f"inference {index} general predicate cannot identify "
                        "subjects"
                    ),
                )
        else:
            if claim_type != "observation":
                raise AnalystOutputError(
                    stage="structured_output",
                    task_id=task_id,
                    summary=(
                        f"inference {index} stable_control must be an observation"
                    ),
                )
            if polarity not in {"positive", "negative"}:
                raise AnalystOutputError(
                    stage="structured_output",
                    task_id=task_id,
                    summary=(
                        f"inference {index} stable_control requires positive or "
                        "negative polarity"
                    ),
                )
            if not subjects:
                raise AnalystOutputError(
                    stage="structured_output",
                    task_id=task_id,
                    summary=(
                        f"inference {index} stable_control requires subject evidence"
                    ),
                )
        inferences.append(
            {
                "bundle_id": bundle_id.strip(),
                "claim_type": claim_type,
                "predicate": predicate,
                "polarity": polarity,
                "subject_evidence_ids": subjects,
                "supporting_evidence_ids": support,
            }
        )
    return {
        "data_evidence_ids": data_ids,
        "knowledge_evidence_ids": knowledge_ids,
        "inferences": inferences,
        "limitation_ids": limitation_ids,
        "source_task_ids": source_ids,
    }


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
            knowledge_only = not task.depends_on and bool(state["knowledge_evidence"])
            query_results, analysis_results = _prepare_inputs(
                state,
                task,
                allow_knowledge_only=True,
            )
            allowed = set(task.depends_on)
            knowledge = compact_knowledge_evidence(state)
            grounding_queries = _transitive_approved_query_results(state, task)
            grounding_analyses = _transitive_completed_analysis_results(
                state,
                task,
            )
            evidence_pack = build_final_evidence_pack(
                grounding_queries,
                knowledge,
                grounding_analyses,
            )
            state["final_evidence_pack"] = evidence_pack
            evidence_pack_json = json.dumps(
                evidence_pack,
                ensure_ascii=False,
                default=str,
                separators=(",", ":"),
            )
            response_schema = _final_answer_schema(
                allow_empty_sources=not allowed
            )
            analyst_guidance_json = json.dumps(
                _bounded_analyst_guidance(analysis_results),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            prompt_prefix = (
                f"Effective user query:\n{get_effective_query(state)}\n\n"
                f"Response task:\n{task.description}\n\n"
                "Allowed source_task_ids:\n"
                f"{_compact_json(task.depends_on)}\n\n"
                "Bounded Analyst guidance (strategy only; not a fact source):\n"
                f"{analyst_guidance_json}\n\n"
                "Final Evidence Projection (model-facing IDs and facts; each "
                "ID resolves to the authoritative Full Evidence Pack):\n"
            )
            prompt_suffix = (
                "\n\nSelection contract: select all required projected data "
                "evidence and limitations. Select only relevant projected "
                "knowledge. Do not cite an ID omitted from this projection. "
                "Every inference must cite selected evidence IDs and use the "
                "correct bundle_id, claim_type, predicate, polarity, and subject "
                "evidence. First choose one deterministic bundle, then use only "
                "IDs listed by that bundle. Split regional and entity-specific "
                "recommendations instead of combining their scopes. "
                "Ordinary claims use general/neutral with no subjects. Only "
                "observation claims may use stable_control, with positive or "
                "negative polarity and non-empty group-comparison subjects. "
                "Use limitation IDs as supporting_evidence_ids when they "
                "directly bound a hypothesis or recommendation. Classify "
                "definitions as knowledge and SOP-backed next checks as "
                "recommendation. Use stable_control plus negative polarity for declining "
                "groups that cannot serve as stable unaffected controls. Do not "
                "add factual prose or fields outside the typed schema. For code-specific "
                "alarm claims, cite the exact matching scoped distribution "
                "rather than an ALL-alarms distribution."
            )
            fixed_prompt_chars = _prompt_chars(
                user_prompt=f"{prompt_prefix}{prompt_suffix}",
                response_schema=response_schema,
            )
            available_evidence_budget = (
                FINAL_PROMPT_CHAR_LIMIT
                - fixed_prompt_chars
                - FINAL_PROMPT_SAFETY_MARGIN
            )
            if available_evidence_budget <= 0:
                raise AnalystError(
                    stage="evidence_projection",
                    task_id=task.task_id,
                    summary="fixed final prompt context exceeds the bounded size",
                )
            try:
                evidence_projection, projection_stats = (
                    build_final_evidence_projection(
                        evidence_pack,
                        max_chars=available_evidence_budget,
                    )
                )
            except EvidenceProjectionError as exc:
                state["final_evidence_projection_stats"] = {
                    "full_evidence_pack_chars": len(evidence_pack_json),
                    "projected_evidence_chars": None,
                    "fixed_prompt_chars": fixed_prompt_chars,
                    "analyst_context_chars": len(analyst_guidance_json),
                    "final_prompt_chars": None,
                    "budget_limit": FINAL_PROMPT_CHAR_LIMIT,
                    "budget_remaining": None,
                    "available_evidence_budget": available_evidence_budget,
                    "safety_margin_chars": FINAL_PROMPT_SAFETY_MARGIN,
                }
                raise AnalystError(
                    stage="evidence_projection",
                    task_id=task.task_id,
                    summary=str(exc)[:300],
                ) from exc
            projection_json = json.dumps(
                evidence_projection,
                ensure_ascii=False,
                default=str,
                separators=(",", ":"),
                sort_keys=True,
            )
            prompt = f"{prompt_prefix}{projection_json}{prompt_suffix}"
            final_prompt_chars = _prompt_chars(
                user_prompt=prompt,
                response_schema=response_schema,
            )
            projection_stats.update(
                {
                    "fixed_prompt_chars": fixed_prompt_chars,
                    "analyst_context_chars": len(analyst_guidance_json),
                    "final_prompt_chars": final_prompt_chars,
                    "budget_limit": FINAL_PROMPT_CHAR_LIMIT,
                    "budget_remaining": (
                        FINAL_PROMPT_CHAR_LIMIT - final_prompt_chars
                    ),
                    "available_evidence_budget": available_evidence_budget,
                    "safety_margin_chars": FINAL_PROMPT_SAFETY_MARGIN,
                    "prompt_attempt_chars": [final_prompt_chars],
                }
            )
            state["final_evidence_projection"] = evidence_projection
            state["final_evidence_projection_stats"] = projection_stats
            if (
                final_prompt_chars + FINAL_PROMPT_SAFETY_MARGIN
                > FINAL_PROMPT_CHAR_LIMIT
            ):
                raise AnalystError(
                    stage="evidence_projection",
                    task_id=task.task_id,
                    summary="bounded final prompt exceeds the context budget",
                )
            del query_results, knowledge_only
            selection, retry_count = self._call_final_model(
                state,
                task,
                prompt=prompt,
                response_schema=response_schema,
                allowed_source_ids=allowed,
                trace=active_trace,
                started_at=started_at,
            )
            text = render_final_answer(
                evidence_pack,
                selection,
                state["final_evidence_projection"],
            )
            findings = [
                line[2:]
                for line in text.splitlines()
                if line.startswith("- [")
            ]
            if not findings:
                selected_data = set(selection["data_evidence_ids"])
                findings = [
                    str(item["statement"])
                    for item in evidence_pack["data_evidence"]
                    if item["evidence_id"] in selected_data
                ][:5]
            result = FinalAnswerResult(
                task_id=task.task_id,
                answer=text,
                key_findings=findings,
                source_task_ids=selection["source_task_ids"],
                retry_count=retry_count,
                evidence_pack=evidence_pack,
                evidence_projection=dict(state["final_evidence_projection"]),
                evidence_projection_stats=dict(
                    state["final_evidence_projection_stats"]
                ),
                claims=list(selection["inferences"]),
                validator_result=dict(state["final_validator_result"]),
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
            comparisons = build_evidence_contract(query_results)["comparisons"]
            if comparisons:
                primary = [
                    item
                    for item in comparisons
                    if item.get("is_primary_comparison") is True
                ]
                findings: list[str] = []
                for comparison in comparisons:
                    baseline = str(comparison["baseline_window"])
                    current = str(comparison["current_window"])
                    largest = comparison.get("largest_decline_group")
                    if largest is None:
                        findings.append(
                            f"{baseline} -> {current}: no declining paired group."
                        )
                    else:
                        group = next(
                            row
                            for row in comparison["groups"]
                            if row["group"] == largest
                        )
                        findings.append(
                            f"{baseline} -> {current}: largest decline is "
                            f"{largest} ({_format_number(float(group['delta']))})."
                        )
                derived: dict[str, Any] = {
                    "normalized_comparisons": comparisons,
                    "comparison_count": len(comparisons),
                    "primary_comparison_id": (
                        primary[0].get("comparison_id")
                        if len(primary) == 1
                        else None
                    ),
                }
                if len(primary) == 1:
                    largest = primary[0].get("largest_decline_group")
                    if largest is not None:
                        group = next(
                            row
                            for row in primary[0]["groups"]
                            if row["group"] == largest
                        )
                        derived["largest_decline"] = {
                            "key": largest,
                            "difference": group["delta"],
                            "baseline_window": primary[0]["baseline_window"],
                            "current_window": primary[0]["current_window"],
                        }
                summary = (
                    f"Normalized {len(comparisons)} explicit window comparison(s)."
                )
                if len(comparisons) > 1 and not primary:
                    summary += " No unique primary baseline was selected."
                return AnalysisResult(
                    task_id=task.task_id,
                    summary=summary,
                    findings=findings,
                    derived_values=derived,
                    source_task_ids=[item.task_id for item in query_results],
                )
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
        evidence_contract = build_evidence_contract(query_results)
        prompt = (
            f"Effective user query:\n{get_effective_query(state)}\n\n"
            f"Analysis task:\n{task.description}\n\n"
            f"Allowed source_task_ids:\n{_compact_json(task.depends_on)}\n\n"
            "Verified query results:\n"
            f"{_compact_verified_query_results(query_results)}\n\n"
            "Completed analysis results:\n"
            f"{_compact_json([asdict(item) for item in analysis_results])}\n\n"
            "Structured evidence contract (deterministic facts and language "
            "constraints):\n"
            f"{_compact_json(evidence_contract)}\n\n"
            "Retrieved knowledge evidence (documentation/SOP, not observed data):\n"
            f"{_compact_json(compact_knowledge_evidence(state))}\n\n"
            "Keep DATA EVIDENCE, KNOWLEDGE EVIDENCE, and INFERENCE distinct. "
            "Do not claim that correlation proves causation."
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
            output_validator=lambda text, items: _validate_evidence_output(
                text,
                items,
                task_id=task.task_id,
                contract=evidence_contract,
                require_sections=False,
            ),
            output_normalizer=lambda text, items: (
                normalize_evidence_language(text, contract=evidence_contract),
                [
                    normalize_evidence_language(
                        item,
                        contract=evidence_contract,
                    )
                    for item in items
                ],
            ),
        )
        return AnalysisResult(
            task_id=task.task_id,
            summary=summary,
            findings=findings,
            source_task_ids=source_ids,
            retry_count=retry_count,
        )

    def _call_final_model(
        self,
        state: AgentState,
        task: TaskItem,
        *,
        prompt: str,
        response_schema: Mapping[str, Any],
        allowed_source_ids: set[str],
        trace: TraceCollector,
        started_at: float,
    ) -> tuple[dict[str, Any], int]:
        last_error: AnalystOutputError | None = None
        exact_fields = json.dumps(sorted(_FINAL_FIELDS), ensure_ascii=False)
        contract_prompt = _final_contract_prompt()
        for attempt in range(self.max_output_retries + 1):
            suffix = ""
            if last_error is not None:
                suffix = (
                    "\n\nStructured output correction:\n"
                    f"Error: {last_error.summary}\n"
                    f"Required top-level fields: {exact_fields}.\n"
                    "Return a corrected JSON object only. Select only Evidence "
                    "Projection IDs, preserve required limitations, and do not "
                    "encode unsupported causality. General claims must use "
                    "neutral polarity and no subjects. Stable-control claims must "
                    "be observations with positive/negative polarity and "
                    "traceable group-comparison subjects. Every claim must select "
                    "an existing bundle_id and cite only IDs allowed by that "
                    "bundle; split incompatible regional/entity scopes. Do not return factual "
                    "prose or fields outside the typed schema."
                )
            request_chars = _prompt_chars(
                user_prompt=prompt,
                response_schema=response_schema,
                correction_suffix=suffix,
            )
            prompt_stats = state["final_evidence_projection_stats"]
            attempt_chars = prompt_stats.setdefault("prompt_attempt_chars", [])
            if not attempt_chars or attempt_chars[-1] != request_chars:
                attempt_chars.append(request_chars)
            prompt_stats["final_prompt_chars"] = max(
                int(prompt_stats.get("final_prompt_chars") or 0),
                request_chars,
            )
            prompt_stats["budget_remaining"] = (
                FINAL_PROMPT_CHAR_LIMIT - prompt_stats["final_prompt_chars"]
            )
            if request_chars > FINAL_PROMPT_CHAR_LIMIT:
                prompt_stats["retry_prompt_chars"] = request_chars
                raise AnalystError(
                    stage="evidence_projection",
                    task_id=task.task_id,
                    summary="bounded final prompt exceeds the context budget",
                )
            try:
                response = self.model_client.complete(
                    system_prompt=FINAL_ANSWER_SYSTEM_PROMPT,
                    user_prompt=f"{prompt}{contract_prompt}{suffix}",
                    response_schema=response_schema,
                )
            except Exception as exc:
                raise AnalystError(
                    stage="model_call",
                    task_id=task.task_id,
                    summary=_safe_error_summary(exc),
                ) from exc
            try:
                selection = _parse_final_selection(
                    response,
                    task_id=task.task_id,
                    allowed_source_ids=allowed_source_ids,
                )
                state["final_claims"] = list(selection["inferences"])
                projection_violations = evidence_projection_claim_violations(
                    selection,
                    state["final_evidence_projection"],
                )
                bundle_violations = evidence_bundle_claim_violations(
                    selection,
                    state["final_evidence_projection"],
                )
                full_pack_violations = final_claim_violations(
                    selection,
                    state["final_evidence_pack"],
                )
                violations = [
                    *projection_violations,
                    *bundle_violations,
                    *full_pack_violations,
                ]
                state["final_validator_result"] = {
                    "valid": not violations,
                    "violations": violations,
                    "retry_count": attempt,
                    "projection_guard_valid": not projection_violations,
                    "bundle_guard_valid": not bundle_violations,
                    "bundle_violations": bundle_violations,
                    "validated_against": "full_evidence_pack",
                }
                if violations:
                    raise AnalystOutputError(
                        stage="evidence_contract",
                        task_id=task.task_id,
                        summary=violations[0][:300],
                    )
            except AnalystOutputError as exc:
                last_error = exc
                recorded_attempt = state["final_validator_result"].get(
                    "retry_count"
                )
                if recorded_attempt != attempt:
                    state["final_validator_result"] = {
                        "valid": False,
                        "violations": [exc.summary],
                        "retry_count": attempt,
                    }
                if attempt < self.max_output_retries:
                    self._event(
                        trace,
                        EventType.ANALYST_OUTPUT_RETRY,
                        task,
                        started_at,
                    )
                    continue
                raise
            return selection, attempt
        raise AssertionError("Final Answer output retry loop exited unexpectedly")

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
        output_validator: Callable[[str, list[str]], None] | None = None,
        output_normalizer: (
            Callable[[str, list[str]], tuple[str, list[str]]] | None
        ) = None,
    ) -> tuple[str, list[str], list[str], int]:
        last_error: AnalystOutputError | None = None
        exact_fields = json.dumps(sorted(expected_fields), ensure_ascii=False)
        contract_prompt = (
            "\n\nStructured output keys:\n"
            f"Return exactly these top-level fields: {exact_fields}. "
            "Include every field and do not add aliases or extra fields."
        )
        for attempt in range(self.max_output_retries + 1):
            suffix = ""
            if last_error is not None:
                suffix = (
                    "\n\nStructured output correction:\n"
                    f"Error: {last_error.summary}\n"
                    f"Required top-level fields: {exact_fields}.\n"
                    "Return a corrected JSON object only, with no additional fields."
                )
            try:
                response = self.model_client.complete(
                    system_prompt=system_prompt,
                    user_prompt=f"{prompt}{contract_prompt}{suffix}",
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
                if output_normalizer is not None:
                    text, items = output_normalizer(text, items)
                if output_validator is not None:
                    output_validator(text, items)
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
            retry_count=int(
                state["final_validator_result"].get("retry_count", 0)
            ),
            evidence_pack=dict(state["final_evidence_pack"]),
            evidence_projection=dict(state["final_evidence_projection"]),
            evidence_projection_stats=dict(
                state["final_evidence_projection_stats"]
            ),
            claims=list(state["final_claims"]),
            validator_result=dict(state["final_validator_result"]),
        )
        self._event(
            trace,
            EventType.FINAL_ANSWER_FAILED,
            task,
            started_at,
            error_type=type(error).__name__,
        )
